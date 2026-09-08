"""Read Spotify playback state from the Windows System Media Transport Controls.

SMTC is the same source that drives the Windows media flyout, so it needs no
OAuth, no Spotify app registration and no network access. The trade-off is that
Spotify's desktop app has to be running.

Position handling is the subtle part. SMTC does not tick `position` forward in
real time -- it publishes a snapshot along with the wall-clock time that snapshot
was taken (`last_updated_time`). Reading `position` alone therefore yields a
value that is anywhere from 0 to ~1000ms stale. Callers get a corrected value:
the reported position plus the time elapsed since the snapshot.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import DataReader

SPOTIFY_APP_ID_FRAGMENT = "spotify"

# Album art is normally ~150KB. Anything far larger is not art worth decoding.
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024

# How often to re-ask for album art while we still do not have it. Spotify
# does not always have artwork ready when a track starts -- it can appear
# seconds later, or never -- so this retries for as long as the track plays
# rather than giving up, throttled so it costs nothing.
ART_RETRY_SECONDS = 1.0


@dataclass
class Playback:
    """A snapshot of what Spotify is currently playing."""

    title: str = ""
    artist: str = ""
    album: str = ""
    duration_ms: int = 0
    playing: bool = False

    # Position as reported by SMTC, plus the monotonic clock reading at the
    # moment we captured it. `position_now_ms` extrapolates between the two.
    position_ms: int = 0
    captured_at: float = field(default_factory=time.monotonic)

    # Album art bytes. Present only on polls where art was requested and
    # Spotify actually had some -- reading the stream is far more expensive
    # than the rest of the snapshot, so it is not fetched on every poll.
    thumbnail: Optional[bytes] = None

    @property
    def track_key(self) -> str:
        """Identity used to decide whether the track changed.

        Deliberately excludes duration: Spotify occasionally revises the
        reported duration a beat after a track starts, and treating that as a
        new track would re-fetch lyrics and restart the animation.
        """
        return f"{self.artist}␟{self.title}"

    def position_now_ms(self) -> int:
        """Best estimate of the current playhead, extrapolated from the snapshot."""
        if not self.playing:
            return self.position_ms
        elapsed = (time.monotonic() - self.captured_at) * 1000.0
        pos = self.position_ms + elapsed
        if self.duration_ms:
            pos = min(pos, self.duration_ms)
        return int(pos)


def _to_ms(timespan) -> int:
    """winsdk surfaces TimeSpan values as datetime.timedelta."""
    if timespan is None:
        return 0
    try:
        return int(timespan.total_seconds() * 1000)
    except AttributeError:
        # Fall back to raw 100-nanosecond ticks if the projection changes.
        return int(getattr(timespan, "duration", 0) / 10_000)


async def _read_thumbnail(props) -> Optional[bytes]:
    """Read album art from the media properties, if the player supplies any.

    Spotify hands over a PNG through an IRandomAccessStreamReference. Any
    failure here is non-fatal -- the overlay simply keeps its neutral theme.
    """
    reference = getattr(props, "thumbnail", None)
    if reference is None:
        return None
    try:
        stream = await reference.open_read_async()
        size = stream.size
        if not size or size > MAX_THUMBNAIL_BYTES:
            return None
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        buffer = bytearray(size)
        reader.read_bytes(buffer)
        return bytes(buffer)
    except Exception:
        return None


async def _read_session(session, want_thumbnail: bool = False) -> Optional[Playback]:
    props = await session.try_get_media_properties_async()
    timeline = session.get_timeline_properties()
    info = session.get_playback_info()

    playing = info.playback_status == PlaybackStatus.PLAYING

    position_ms = _to_ms(timeline.position)
    duration_ms = _to_ms(timeline.end_time)

    # Correct for snapshot staleness. `last_updated_time` is an aware datetime
    # in UTC; if it is missing or nonsensical we simply treat the reading as
    # fresh, which is what the naive implementation would have done anyway.
    captured_at = time.monotonic()
    last_updated = getattr(timeline, "last_updated_time", None)
    if playing and last_updated is not None:
        try:
            age = (datetime.now(timezone.utc) - last_updated).total_seconds()
            # Guard against clock skew and against Spotify publishing a stale
            # timestamp when paused/seeking; anything beyond a few seconds is
            # more likely to be a bug than a genuinely old snapshot.
            if 0.0 <= age <= 5.0:
                position_ms += int(age * 1000)
        except (TypeError, ValueError, OverflowError):
            pass

    thumbnail = await _read_thumbnail(props) if want_thumbnail else None

    return Playback(
        title=props.title or "",
        artist=props.artist or "",
        album=props.album_title or "",
        duration_ms=duration_ms,
        playing=playing,
        position_ms=position_ms,
        captured_at=captured_at,
        thumbnail=thumbnail,
    )


async def _find_spotify_session(manager):
    """Prefer Spotify, but fall back to whatever is currently playing.

    Running the overlay against a browser tab or another player is a reasonable
    default -- the lyrics lookup is title/artist based and does not care which
    app produced the metadata.
    """
    sessions = list(manager.get_sessions())
    if not sessions:
        return None

    for session in sessions:
        app_id = (session.source_app_user_model_id or "").lower()
        if SPOTIFY_APP_ID_FRAGMENT in app_id:
            return session

    current = manager.get_current_session()
    return current if current is not None else sessions[0]


class SmtcWatcher:
    """Polls SMTC on a background thread and reports changes via callback.

    SMTC exposes change events, but they are unreliable for position (they fire
    on seek and state change, not continuously) and binding them from Python
    means keeping delegate objects alive across the COM boundary. Polling a
    handful of cheap local calls a few times a second is simpler and has no
    measurable cost.
    """

    def __init__(self, on_update: Callable[[Optional[Playback]], None], interval: float = 0.25):
        self._on_update = on_update
        self._interval = interval
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="smtc-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        asyncio.get_event_loop().run_until_complete(self._loop())

    async def _loop(self) -> None:
        manager = None
        seen_key = ""        # Track identity as of the previous poll.
        art_key = ""         # Track we have successfully read art for.
        last_art_try = 0.0   # Monotonic time of the last art request.
        while not self._stop.is_set():
            try:
                if manager is None:
                    manager = await SessionManager.request_async()

                session = await _find_spotify_session(manager)
                if session is None:
                    playback = None
                else:
                    # Ask for art whenever we do not already have it for the
                    # track seen on the previous poll. Spotify does not always
                    # have artwork ready -- sometimes it appears seconds into a
                    # track, and for some tracks never at all -- so this keeps
                    # asking rather than taking one shot at the track change.
                    # The request costs nothing extra: it rides along on the
                    # media-properties call this poll was already making.
                    now = time.monotonic()
                    want_art = (
                        art_key != seen_key
                        and (now - last_art_try) >= ART_RETRY_SECONDS
                    )
                    if want_art:
                        last_art_try = now

                    playback = await _read_session(session, want_thumbnail=want_art)

                    if playback is not None:
                        if playback.track_key != seen_key:
                            # New track: drop any art we were holding, and let
                            # the next poll fetch this one's.
                            seen_key = playback.track_key
                            art_key = ""
                            last_art_try = 0.0
                        elif playback.thumbnail:
                            art_key = playback.track_key
                self._on_update(playback)
            except Exception:
                # A dropped session, Spotify restarting, or a transient COM
                # failure should never kill the watcher. Force the manager to be
                # re-acquired and try again on the next tick.
                manager = None
                self._on_update(None)

            await asyncio.sleep(self._interval)
