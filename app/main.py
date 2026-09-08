"""Transparent always-on-top lyrics overlay.

The window is a frameless, translucent Qt shell whose entire visible surface is
a QWebEngineView. Qt owns the window (transparency, stacking, dragging); the web
layer owns everything you can see. That split is what lets the card be animated
with Motion while still behaving like a native desktop overlay.

Bridge design
-------------
JS talks to Python by writing a tagged line to the console, which Python reads by
overriding `javaScriptConsoleMessage`. This avoids QWebChannel, which would
otherwise require either serving the page over HTTP (so `qrc:///` scripts are
reachable without tripping CORS) or shipping a second transport. Traffic is only
a handful of messages per interaction, so the console path is more than fast
enough -- dragging in particular sends two messages total, not one per frame.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QPoint, QRect, QTimer, QUrl, pyqtSignal, Qt
from PyQt6.QtGui import QAction, QCursor, QGuiApplication
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

from . import startup
from .config import CACHE_DIR, ConfigStore
from .icon import app_icon
from .lyrics import LyricsProvider, LyricsResult
from .palette import NEUTRAL_THEME, theme_from_image
from .settings_dialog import SettingsDialog
from .smtc import Playback, SmtcWatcher
from .spotify import SpotifyClient

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR.parent / "web"

BRIDGE_PREFIX = "LYRICBRIDGE"

# Set LYRIC_OVERLAY_DEBUG=1 to trace bridge traffic on stderr.
DEBUG = bool(os.environ.get("LYRIC_OVERLAY_DEBUG"))

DEFAULT_WIDTH = 660
DEFAULT_HEIGHT = 230

# Resize bounds. The lower bounds are where the card stops being able to show a
# lyric line plus its neighbours; the upper bounds stop a stray drag from
# swallowing the screen.
MIN_WIDTH = 340
MIN_HEIGHT = 130
MAX_WIDTH = 1800
MAX_HEIGHT = 900


# ---------------------------------------------------------------------------
# Web page with a console-based inbound bridge
# ---------------------------------------------------------------------------


class BridgePage(QWebEnginePage):
    """A page that forwards tagged console lines to Python."""

    message = pyqtSignal(dict)

    def javaScriptConsoleMessage(self, level, msg, line, source):  # noqa: N802 (Qt API)
        if msg.startswith(BRIDGE_PREFIX):
            if DEBUG:
                print(f"[bridge] {msg[len(BRIDGE_PREFIX):]}", file=sys.stderr)
            try:
                self.message.emit(json.loads(msg[len(BRIDGE_PREFIX):]))
            except json.JSONDecodeError:
                pass
            return
        # Let genuine page errors reach the terminal; silence routine chatter.
        if level != QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel:
            print(f"[web] {source}:{line} {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Lyrics fetching off the UI thread
# ---------------------------------------------------------------------------


class LyricsWorker(QObject):
    """Fetches lyrics on a background thread and reports back via signal.

    Each request carries the track key it was issued for, so a slow response for
    a track the user already skipped past can be discarded rather than
    overwriting the current one.
    """

    ready = pyqtSignal(str, object)

    def __init__(self, provider: LyricsProvider):
        super().__init__()
        self._provider = provider

    def request(self, key: str, artist: str, title: str, album: str, duration_ms: int) -> None:
        def run() -> None:
            try:
                result = self._provider.fetch(artist, title, album, duration_ms)
            except Exception:
                result = LyricsResult(lines=[], synced=False, source="error")
            self.ready.emit(key, result)

        threading.Thread(target=run, name="lyrics-fetch", daemon=True).start()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class Overlay(QMainWindow):
    playback_changed = pyqtSignal(object)
    spotify_auth_finished = pyqtSignal(bool, str)
    theme_ready = pyqtSignal(object)

    def __init__(self, store: ConfigStore) -> None:
        super().__init__()

        self.store = store
        self._settings_dialog: Optional[SettingsDialog] = None
        self._theme_key: str = ""  # Track whose art the current theme came from.
        self._preloaded: set[str] = set()

        self._drag_offset: Optional[QPoint] = None
        self._resize_edge: str = ""
        self._resize_origin: QPoint = QPoint()
        self._resize_geometry: Optional[QRect] = None
        self._current_key: str = ""
        self._pending_key: str = ""
        self._ready = False
        self._queued_calls: list[str] = []

        self._configure_window()
        self._build_web_view()
        self._restore_geometry()

        # Drag is driven by a timer following the OS cursor rather than by mouse
        # events, because the web view consumes mouse events before Qt sees them.
        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(8)
        self._drag_timer.timeout.connect(self._follow_cursor)

        # Resizing is driven the same way, and for the same reason: the web view
        # consumes the mouse events, so Qt follows the OS cursor instead.
        self._resize_timer = QTimer(self)
        self._resize_timer.setInterval(8)
        self._resize_timer.timeout.connect(self._follow_resize)

        self._provider = LyricsProvider(CACHE_DIR)
        self._worker = LyricsWorker(self._provider)
        self._worker.ready.connect(self._on_lyrics_ready)

        settings = self.store.settings
        self._spotify = SpotifyClient(settings.spotify_client_id, settings.spotify_refresh_token)

        # Queue preloading polls far more slowly than playback: the queue only
        # changes when the user edits it or a track ends, and this is a
        # best-effort optimisation, not something the UI waits on.
        # Some tracks never publish album art. If none has arrived shortly
        # after a track change, drop back to the neutral theme rather than
        # leaving the previous song's colour on screen.
        self._art_timeout = QTimer(self)
        self._art_timeout.setSingleShot(True)
        self._art_timeout.setInterval(3000)
        self._art_timeout.timeout.connect(self._no_art)

        self._preload_timer = QTimer(self)
        self._preload_timer.setInterval(15000)
        self._preload_timer.timeout.connect(self._preload_queue)
        if self._spotify.connected and settings.preload_from_queue:
            self._preload_timer.start()

        self._build_tray()

        self.spotify_auth_finished.connect(self._spotify_connected)
        self.theme_ready.connect(self._on_theme_ready)
        self.playback_changed.connect(self._on_playback)
        # 120ms rather than 250ms: this interval is the floor on how fast a
        # track change is noticed, and it sits directly in front of the lyric
        # fetch. The SMTC calls are local COM and cost microseconds, so the
        # faster poll is not measurable against the fetch it precedes.
        self._watcher = SmtcWatcher(self.playback_changed.emit, interval=0.12)
        self._watcher.start()

    # -- window setup -------------------------------------------------------

    def _configure_window(self) -> None:
        self.setWindowTitle("Lyric Overlay")
        self.setWindowIcon(app_icon())
        # Tool keeps the overlay out of the taskbar and Alt-Tab, which is what
        # makes it read as an overlay rather than an application you switch to.
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.store.settings.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(DEFAULT_WIDTH, DEFAULT_HEIGHT)

    def _apply_always_on_top(self, enabled: bool) -> None:
        """Toggle the stay-on-top hint, preserving position and visibility.

        Changing window flags re-creates the native window, so Qt hides it --
        it has to be shown again explicitly, and the geometry restored.
        """
        was_visible = self.isVisible()
        geometry = self.geometry()

        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

        self.setGeometry(geometry)
        if was_visible:
            self.show()

    def _build_web_view(self) -> None:
        self.view = QWebEngineView(self)
        self.page = BridgePage(self.view)
        self.page.message.connect(self._on_bridge_message)
        self.view.setPage(self.page)

        # Without a transparent page background the view paints opaque white
        # over the translucent window.
        self.page.setBackgroundColor(Qt.GlobalColor.transparent)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.view.setStyleSheet("background: transparent;")

        settings = self.page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

        self.view.loadFinished.connect(self._on_load_finished)
        self.view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))
        self.setCentralWidget(self.view)

    # -- persistence --------------------------------------------------------

    def _restore_geometry(self) -> None:
        settings = self.store.settings
        x, y = settings.x, settings.y
        width, height = settings.w, settings.h

        if x is None or y is None or not self._is_on_screen(x, y, width, height):
            self._center_near_bottom()
            return
        self.setGeometry(x, y, width, height)
        self._clamp_to_screen()

    @staticmethod
    def _is_on_screen(x: int, y: int, w: int, h: int) -> bool:
        """Reject saved positions that no longer land on a connected display."""
        window = QRect(x, y, w, h)
        return any(
            screen.availableGeometry().intersects(window)
            for screen in QGuiApplication.screens()
        )

    def _center_near_bottom(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.setGeometry(
            area.center().x() - DEFAULT_WIDTH // 2,
            area.bottom() - DEFAULT_HEIGHT - 72,
            DEFAULT_WIDTH,
            DEFAULT_HEIGHT,
        )
        self._clamp_to_screen()

    def _clamp_to_screen(self) -> None:
        """Pull the window fully inside its screen's work area.

        Needed because the initial placement is computed before Qt has applied
        the window frame, and because a restored position can land off-screen
        after a resolution or scaling change.
        """
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()

        x = min(max(self.x(), area.left()), max(area.left(), area.right() - self.width()))
        y = min(max(self.y(), area.top()), max(area.top(), area.bottom() - self.height()))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _save_geometry(self) -> None:
        self.store.update(x=self.x(), y=self.y(), w=self.width(), h=self.height())

    # -- bridge -------------------------------------------------------------

    def _call_js(self, function: str, payload: dict) -> None:
        script = f"window.{function} && window.{function}({json.dumps(payload)})"
        if not self._ready:
            self._queued_calls.append(script)
            return
        self.page.runJavaScript(script)

    def _on_load_finished(self, ok: bool) -> None:
        self._ready = bool(ok)
        if not ok:
            print("[overlay] web layer failed to load", file=sys.stderr)
            return
        for script in self._queued_calls:
            self.page.runJavaScript(script)
        self._queued_calls.clear()
        # Push the saved appearance now that the page can receive it.
        self._apply_appearance()

    def _on_bridge_message(self, message: dict) -> None:
        kind = message.get("t")

        if kind == "dragstart":
            # Record the grab point so the window keeps its offset under the
            # cursor instead of snapping its corner to it.
            self._drag_offset = QCursor.pos() - self.pos()
            self._drag_timer.start()
        elif kind == "dragend":
            self._drag_timer.stop()
            self._drag_offset = None
            self._save_geometry()
        elif kind == "close":
            self.hide_to_tray()
        elif kind == "resizestart":
            self._resize_edge = str(message.get("edge", "se"))
            self._resize_origin = QCursor.pos()
            self._resize_geometry = QRect(self.geometry())
            self._resize_timer.start()
        elif kind == "resizeend":
            self._resize_timer.stop()
            self._resize_edge = ""
            self._save_geometry()

    def _follow_cursor(self) -> None:
        if self._drag_offset is not None:
            self.move(QCursor.pos() - self._drag_offset)

    def _follow_resize(self) -> None:
        """Resize the window to track the cursor, from the grabbed edge."""
        if not self._resize_edge or self._resize_geometry is None:
            return

        delta = QCursor.pos() - self._resize_origin
        start = self._resize_geometry
        edge = self._resize_edge

        x, y = start.x(), start.y()
        width, height = start.width(), start.height()

        if "e" in edge:
            width = start.width() + delta.x()
        elif "w" in edge:
            width = start.width() - delta.x()
        if "s" in edge:
            height = start.height() + delta.y()
        elif "n" in edge:
            height = start.height() - delta.y()

        width = max(MIN_WIDTH, min(MAX_WIDTH, width))
        height = max(MIN_HEIGHT, min(MAX_HEIGHT, height))

        # Dragging a top or left edge moves the origin as well as the size.
        # Deriving it from the clamped size keeps the opposite edge pinned once
        # the minimum is reached, instead of letting the window creep.
        if "w" in edge:
            x = start.x() + start.width() - width
        if "n" in edge:
            y = start.y() + start.height() - height

        self.setGeometry(x, y, width, height)

    # -- playback -----------------------------------------------------------

    def _on_playback(self, playback: Optional[Playback]) -> None:
        if playback is None or not playback.title:
            self._call_js("overlaySetIdle", {"reason": "no-session"})
            self._set_tray_state(False, "Lyric Overlay — nothing playing")
            return

        self._set_tray_state(playback.playing, f"{playback.title} — {playback.artist}")

        key = playback.track_key
        if key != self._current_key:
            self._current_key = key
            self._pending_key = key
            self._call_js(
                "overlaySetTrack",
                {
                    "title": playback.title,
                    "artist": playback.artist,
                    "durationMs": playback.duration_ms,
                },
            )
            self._worker.request(
                key, playback.artist, playback.title, playback.album, playback.duration_ms
            )
            # The previous track's colour is left in place for now: album art
            # arrives a poll or two after the metadata, and blanking to neutral
            # first would flash grey between every track. If no art turns up,
            # the timer below reverts.
            self._theme_key = ""
            self._art_timeout.start()

        if playback.thumbnail and self._theme_key != key:
            self._theme_key = key
            self._art_timeout.stop()
            self._extract_theme(playback.thumbnail)

        self._call_js(
            "overlaySync",
            {"positionMs": playback.position_now_ms(), "playing": playback.playing},
        )

    # -- album-art theming ---------------------------------------------------

    def _extract_theme(self, thumbnail: Optional[bytes]) -> None:
        """Derive the card's colours from the album art, off the UI thread.

        Decoding and quantising costs a few milliseconds -- small, but it lands
        exactly when a track change is already doing the most work, so it stays
        off the thread that is animating the card.
        """
        if not self.store.settings.theme_from_album:
            self.theme_ready.emit(NEUTRAL_THEME)
            return
        if not thumbnail:
            # No art (podcast, local file, some players). Fall back rather than
            # leaving the previous track's colours on screen.
            self.theme_ready.emit(NEUTRAL_THEME)
            return

        def run() -> None:
            try:
                theme = theme_from_image(thumbnail)
            except Exception:
                theme = NEUTRAL_THEME
            self.theme_ready.emit(theme)

        threading.Thread(target=run, name="palette", daemon=True).start()

    def _no_art(self) -> None:
        """No artwork arrived for this track; fall back to the neutral theme."""
        if not self._theme_key:
            self._call_js("overlaySetTheme", NEUTRAL_THEME.as_payload())

    def _on_theme_ready(self, theme) -> None:
        self._call_js("overlaySetTheme", theme.as_payload())

    def _on_lyrics_ready(self, key: str, result: LyricsResult) -> None:
        # Discard responses for tracks that are no longer playing.
        if key != self._current_key:
            return

        self._call_js(
            "overlaySetLyrics",
            {
                "synced": result.synced,
                "source": result.source,
                "lines": [
                    {
                        "start": line.start_ms,
                        "end": line.end_ms,
                        "instrumental": line.instrumental,
                        "words": [
                            {"t": w.text, "s": w.start_ms, "e": w.end_ms} for w in line.words
                        ],
                        "text": line.text,
                    }
                    for line in result.lines
                ],
            },
        )

    # -- lifecycle ----------------------------------------------------------

    # -- tray ---------------------------------------------------------------

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(muted=True), self)
        self.tray.setToolTip("Lyric Overlay")

        menu = QMenu()
        self._toggle_action = QAction("Hide overlay", self)
        self._toggle_action.triggered.connect(self.toggle_visible)
        menu.addAction(self._toggle_action)

        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        # Left click or double click toggles; right click opens the menu, which
        # Qt handles itself.
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_visible()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self._clamp_to_screen()
        self._sync_tray_labels()

    def _sync_tray_labels(self) -> None:
        self._toggle_action.setText("Hide overlay" if self.isVisible() else "Show overlay")

    def _set_tray_state(self, playing: bool, tooltip: str) -> None:
        self.tray.setIcon(app_icon(muted=not playing))
        self.tray.setToolTip(tooltip or "Lyric Overlay")

    # -- settings -----------------------------------------------------------

    def open_settings(self) -> None:
        if self._settings_dialog is None:
            dialog = SettingsDialog(self.store, self._connect_spotify, None)
            dialog.appearance_changed.connect(self._apply_appearance)
            dialog.finished.connect(self._settings_closed)
            self._settings_dialog = dialog

        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _settings_closed(self) -> None:
        dialog = self._settings_dialog
        if dialog is None:
            return

        chosen = dialog.collect()
        previous_top = self.store.settings.always_on_top
        self.store.update(**chosen)

        if chosen["always_on_top"] != previous_top:
            self._apply_always_on_top(chosen["always_on_top"])

        startup.set_enabled(chosen["start_with_windows"])

        self._spotify.client_id = chosen["spotify_client_id"]
        if self._spotify.connected and chosen["preload_from_queue"]:
            self._preload_timer.start()
            self._preload_queue()
        else:
            self._preload_timer.stop()

        self._settings_dialog = None
        dialog.deleteLater()

    def _apply_appearance(self) -> None:
        settings = self.store.settings
        if not settings.theme_from_album:
            self._call_js("overlaySetTheme", NEUTRAL_THEME.as_payload())
        self._call_js(
            "overlaySetAppearance",
            {
                "plateCore": settings.plate_core,
                "plateEdge": settings.plate_edge,
                "textScale": settings.text_scale,
            },
        )

    def _connect_spotify(self, client_id: str) -> None:
        self.store.update(spotify_client_id=client_id)
        self._spotify.client_id = client_id
        # The callback fires on the auth thread; the signal hops it back to the
        # UI thread, which is the only place Qt widgets may be touched.
        self._spotify.authorize(
            lambda ok, message: self.spotify_auth_finished.emit(ok, message)
        )

    def _spotify_connected(self, ok: bool, message: str) -> None:
        if ok:
            self.store.update(spotify_refresh_token=self._spotify.refresh_token)
            if self.store.settings.preload_from_queue:
                self._preload_timer.start()
                self._preload_queue()
        if self._settings_dialog is not None:
            self._settings_dialog.set_status(message)

    # -- queue preloading ---------------------------------------------------

    def _preload_queue(self) -> None:
        """Warm the cache for tracks Spotify says are coming up.

        Runs entirely in the background and never touches the UI: if it works,
        the next track's lyrics are already cached when it starts; if it fails,
        nothing changes.
        """
        if not self._spotify.connected:
            return

        def run() -> None:
            for track in self._spotify.upcoming(limit=2):
                key = f"{track['artist']}␟{track['title']}"
                if not key.strip("␟") or key in self._preloaded:
                    continue
                self._preloaded.add(key)
                try:
                    self._provider.fetch(
                        track["artist"], track["title"], track["album"], track["duration_ms"]
                    )
                except Exception:
                    self._preloaded.discard(key)

            # Bound the set so a long session cannot grow it without limit.
            if len(self._preloaded) > 200:
                self._preloaded.clear()

        threading.Thread(target=run, name="queue-preload", daemon=True).start()

    def quit(self) -> None:
        self._save_geometry()
        self.tray.hide()
        QApplication.instance().quit()

    def showEvent(self, event):  # noqa: N802 (Qt API)
        super().showEvent(event)
        # Re-clamp once the window actually exists. Before show(), Qt has not
        # resolved the frame or the per-monitor DPI scaling, so a placement
        # computed in __init__ can still land partly off-screen.
        self._clamp_to_screen()

    def hide_to_tray(self) -> None:
        """Hide the overlay, leaving it running in the notification area."""
        self._save_geometry()
        self.hide()
        self._sync_tray_labels()

        # Say where it went, once. Windows hides new tray icons in the overflow
        # by default, so without this the app looks like it simply quit.
        if not self.store.settings.tray_hint_shown:
            self.tray.showMessage(
                "Still running",
                "Lyric Overlay is in the notification area. "
                "Click the tray icon to bring it back.",
                app_icon(),
                6000,
            )
            self.store.update(tray_hint_shown=True)

    def closeEvent(self, event):  # noqa: N802 (Qt API)
        # The window close is a hide; quitting is done from the tray menu, so
        # the overlay does not disappear for good on a stray click.
        event.ignore()
        self.hide_to_tray()


def _enable_dpi_awareness() -> None:
    """Declare per-monitor DPI awareness before Qt initialises.

    Without this, Windows treats the process as legacy and bitmap-stretches the
    whole window on a scaled display: text renders soft, and Qt's geometry
    maths runs in virtualised coordinates that do not match physical pixels, so
    a window placed inside the work area can still land off-screen.

    Must run before QApplication is constructed -- the awareness mode is latched
    on first use.
    """
    if sys.platform != "win32":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        pass


def main() -> int:
    _enable_dpi_awareness()

    app = QApplication(sys.argv)
    app.setApplicationName("Lyric Overlay")
    app.setWindowIcon(app_icon())
    # The overlay hides to the tray rather than exiting, and the settings window
    # opening and closing must not end the process either. Quit is explicit,
    # from the tray menu.
    app.setQuitOnLastWindowClosed(False)

    store = ConfigStore()
    # Keep the checkbox honest: the user can remove the entry from Windows
    # Settings, and the config should reflect that rather than silently disagree.
    store.settings.start_with_windows = startup.is_enabled()

    overlay = Overlay(store)
    overlay.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
