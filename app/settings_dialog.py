"""Settings window, opened from the tray menu.

Deliberately honest about credentials: the app needs none, and the dialog says
so, so nobody goes hunting for a key that does not exist. The Spotify section is
presented as an optional speed-up, not a requirement.
"""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .config import ConfigStore
from .icon import app_icon

HELP_NO_KEY = (
    "No API key is required. Lyrics come from LRCLIB (free, keyless) and "
    "playback is read from Windows itself, so the app works as soon as it is "
    "installed."
)

HELP_SPOTIFY = (
    "Optional. Windows only reports the track playing right now, never what is "
    "queued next — so the first play of a track waits for its lyrics to "
    "download. Connecting Spotify lets the overlay read your queue and fetch "
    "the next track's lyrics in advance, so they are ready the moment it "
    "starts.\n\n"
    "Create an app at developer.spotify.com/dashboard, add "
    "http://127.0.0.1:8888/callback as a Redirect URI, and paste the Client ID "
    "below. No client secret is needed."
)


class SettingsDialog(QDialog):
    """Live-editing settings panel. Appearance changes apply as you drag."""

    appearance_changed = pyqtSignal()

    def __init__(
        self,
        store: ConfigStore,
        on_connect_spotify: Callable[[str], None],
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.store = store
        self._on_connect_spotify = on_connect_spotify

        self.setWindowTitle("Lyric Overlay — Settings")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(470)
        # A normal window, not a tool window: it must be reachable from the
        # taskbar, because the overlay itself deliberately is not.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.addWidget(self._build_appearance())
        layout.addWidget(self._build_behaviour())
        layout.addWidget(self._build_spotify())
        layout.addStretch(1)
        layout.addLayout(self._build_buttons())

    # -- sections -----------------------------------------------------------

    def _build_appearance(self) -> QGroupBox:
        box = QGroupBox("Appearance")
        form = QFormLayout(box)
        settings = self.store.settings

        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(10, 100)
        self.opacity.setValue(int(settings.plate_core * 100))
        self.opacity.valueChanged.connect(self._apply_appearance)
        self.opacity_label = QLabel()
        form.addRow("Background opacity", self._with_value(self.opacity, self.opacity_label))

        self.text_scale = QSlider(Qt.Orientation.Horizontal)
        self.text_scale.setRange(60, 200)
        self.text_scale.setValue(int(settings.text_scale * 100))
        self.text_scale.valueChanged.connect(self._apply_appearance)
        self.text_scale_label = QLabel()
        form.addRow("Text size", self._with_value(self.text_scale, self.text_scale_label))

        self.theme_from_album = QCheckBox("Tint the card to match the album cover")
        self.theme_from_album.setChecked(settings.theme_from_album)
        self.theme_from_album.toggled.connect(self._apply_appearance)
        form.addRow(self.theme_from_album)

        hint = QLabel(
            "Only the background is transparent — the lyrics stay solid. "
            "Album tinting changes the card's colour, never how dark it is."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(placeholderText); font-size: 11px;")
        form.addRow(hint)

        self._sync_labels()
        return box

    def _build_behaviour(self) -> QGroupBox:
        box = QGroupBox("Behaviour")
        layout = QVBoxLayout(box)
        settings = self.store.settings

        self.always_on_top = QCheckBox("Keep above other windows")
        self.always_on_top.setChecked(settings.always_on_top)
        layout.addWidget(self.always_on_top)

        self.start_with_windows = QCheckBox("Start automatically when I sign in")
        self.start_with_windows.setChecked(settings.start_with_windows)
        layout.addWidget(self.start_with_windows)

        note = QLabel("Closing the overlay hides it to the tray. Quit from the tray menu.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholderText); font-size: 11px;")
        layout.addWidget(note)
        return box

    def _build_spotify(self) -> QGroupBox:
        box = QGroupBox("Spotify connection (optional)")
        layout = QVBoxLayout(box)
        settings = self.store.settings

        no_key = QLabel(HELP_NO_KEY)
        no_key.setWordWrap(True)
        no_key.setStyleSheet("font-size: 11px;")
        layout.addWidget(no_key)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet("color: palette(mid);")
        layout.addWidget(rule)

        explain = QLabel(HELP_SPOTIFY)
        explain.setWordWrap(True)
        explain.setStyleSheet("color: palette(placeholderText); font-size: 11px;")
        layout.addWidget(explain)

        row = QHBoxLayout()
        self.client_id = QLineEdit(settings.spotify_client_id)
        self.client_id.setPlaceholderText("Spotify Client ID (leave blank to skip)")
        row.addWidget(self.client_id, 1)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self._connect_clicked)
        row.addWidget(self.connect_button)
        layout.addLayout(row)

        self.preload = QCheckBox("Preload the next track's lyrics from my queue")
        self.preload.setChecked(settings.preload_from_queue)
        layout.addWidget(self.preload)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size: 11px;")
        layout.addWidget(self.status)
        self.set_status(
            "Connected." if settings.spotify_refresh_token else "Not connected — using Windows only."
        )
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)

        close = QPushButton("Done")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        row.addWidget(close)
        return row

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _with_value(slider: QSlider, label: QLabel) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, 1)
        label.setMinimumWidth(42)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(label)
        return holder

    def _sync_labels(self) -> None:
        self.opacity_label.setText(f"{self.opacity.value()}%")
        self.text_scale_label.setText(f"{self.text_scale.value()}%")

    def _apply_appearance(self) -> None:
        self._sync_labels()
        core = self.opacity.value() / 100.0
        self.store.update(
            theme_from_album=self.theme_from_album.isChecked(),
            plate_core=core,
            # The edge stays proportionally thinner than the centre, preserving
            # the vignette that keeps the card from reading as a flat slab.
            plate_edge=round(core * 0.85, 3),
            text_scale=self.text_scale.value() / 100.0,
        )
        self.appearance_changed.emit()

    def _connect_clicked(self) -> None:
        client_id = self.client_id.text().strip()
        if not client_id:
            self.set_status("Enter a Client ID first.")
            return
        self.connect_button.setEnabled(False)
        self.set_status("Opening your browser to authorise…")
        self._on_connect_spotify(client_id)

    def set_status(self, message: str) -> None:
        self.status.setText(message)
        self.connect_button.setEnabled(True)

    def collect(self) -> dict:
        """Settings that are only committed when the dialog closes."""
        return {
            "theme_from_album": self.theme_from_album.isChecked(),
            "always_on_top": self.always_on_top.isChecked(),
            "start_with_windows": self.start_with_windows.isChecked(),
            "spotify_client_id": self.client_id.text().strip(),
            "preload_from_queue": self.preload.isChecked(),
        }
