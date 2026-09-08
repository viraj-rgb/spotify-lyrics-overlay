"""Fetch line-synced lyrics from LRCLIB and derive word-level timings.

On word timing
--------------
LRCLIB (like the LRC format generally) is line-synced: every line carries one
timestamp marking when it starts. True word-level timing -- the kind that drives
Spotify's own karaoke highlight -- comes from Musixmatch's `richsync` format,
which is a paid commercial product and has no free equivalent. NetEase's `yrc`
field is word-level but returns empty without an app login cookie.

So word timings here are *derived*, not authored. Each line's duration is split
across its words proportional to length. The result reads as karaoke and is
usually within a beat of the vocal, but it cannot know that a singer held one
syllable for two seconds. Timing resnaps to truth at every line boundary.

`Line.words` is the renderer's native format, so swapping in a real richsync
provider later means implementing one function, not touching the UI.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, List, Optional

import requests

LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "spotify-lyrics-overlay/1.0 (https://github.com/viraj-rgb/spotify-lyrics-overlay)"

# A line is split across its words by character weight, but every word also gets
# a flat share so that short words ("a", "I") do not flash past unreadably.
WORD_FLAT_WEIGHT = 2.2

# A line's sweep occupies the whole window between its timestamp and the next
# line's -- that window *is* how long the line takes to sing, and using less
# makes the highlight race ahead of the vocal.
#
# The exception is a line followed by an instrumental break, where the window
# includes dead air. That is detected by comparing the window against the
# track's own singing pace: anything longer than GAP_RATIO times the expected
# duration is treated as a gap, and the sweep finishes early rather than
# crawling across silence.
#
# Pace varies enormously between songs -- a fast verse runs ~90ms per character,
# a slow ballad closer to 300ms -- so it is measured per track by
# `_median_rate`. This constant is only the fallback for tracks with too few
# lines to measure. Erring slow is deliberate: a highlight that lags slightly
# reads as expressive, one that runs ahead reads as broken.
DEFAULT_MS_PER_CHAR = 155
# How far past the track's own typical pace a line must run before its window is
# treated as containing an instrumental gap rather than just slow singing.
GAP_RATIO = 2.1
GAP_SWEEP_SCALE = 1.25
# Bounds on the measured rate, guarding against tracks whose timings are sparse
# or malformed enough to produce a nonsense median.
MIN_MS_PER_CHAR = 60
MAX_MS_PER_CHAR = 420
# Fraction of the window the sweep occupies, leaving the last sliver as a beat
# of hold before the line changes.
FILL_RATIO = 0.96
# Gap left before the next line starts, so the sweep visibly completes.
SWEEP_TAIL_MS = 40
# Assumed duration for the final line, which has no successor to bound it.
LAST_LINE_MS = 4000

_TIMESTAMP = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class Word:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class Line:
    start_ms: int
    end_ms: int
    text: str
    words: List[Word]
    # LRCLIB marks instrumental passages with a bare music note. The UI shows a
    # breathing indicator for these rather than rendering the glyph as a lyric.
    instrumental: bool = False


@dataclass
class LyricsResult:
    lines: List[Line]
    synced: bool
    source: str
    track: str = ""
    artist: str = ""

    @property
    def empty(self) -> bool:
        return not self.lines


def _parse_lrc(raw: str) -> List[tuple[int, str]]:
    """Parse LRC text into (timestamp_ms, text) pairs, sorted by time.

    Handles the multi-timestamp form (`[00:12.00][01:30.00] chorus line`) that
    LRC uses to avoid repeating a repeated chorus.
    """
    out: List[tuple[int, str]] = []
    for raw_line in raw.splitlines():
        stamps = list(_TIMESTAMP.finditer(raw_line))
        if not stamps:
            continue
        text = raw_line[stamps[-1].end():].strip()
        for match in stamps:
            minutes, seconds, frac = match.groups()
            ms = int(minutes) * 60_000 + int(seconds) * 1000
            if frac:
                # LRC fractions are centiseconds (2 digits) or milliseconds (3).
                ms += int(frac.ljust(3, "0")[:3]) if len(frac) == 3 else int(frac) * 10
            out.append((ms, text))

    out.sort(key=lambda pair: pair[0])
    return out


_INSTRUMENTAL_CHARS = set("♪♫♬♩ -~. ")


def _is_instrumental(text: str) -> bool:
    """True for lines that are only music-note markers, not sung words."""
    return bool(text) and all(ch in _INSTRUMENTAL_CHARS for ch in text)


def _split_words(text: str) -> List[str]:
    """Split on whitespace, keeping punctuation attached to its word."""
    return [w for w in text.split() if w]


def _visible_length(word: str) -> int:
    """Weight a word by its pronounceable length.

    Punctuation is stripped because a trailing comma costs no singing time, and
    combining marks are excluded so accented text is not over-weighted.
    """
    stripped = "".join(
        ch for ch in word
        if not unicodedata.combining(ch) and unicodedata.category(ch)[0] not in ("P", "Z")
    )
    return max(len(stripped), 1)


def _median_rate(pairs: List[tuple[int, str]]) -> float:
    """Measure this track's typical singing pace, in ms per character.

    Uses the median so that instrumental gaps -- which are exactly the outliers
    being detected -- cannot drag the estimate upward. Requires a handful of
    lines to be meaningful; below that the default is used.
    """
    rates: List[float] = []
    for index, (start_ms, text) in enumerate(pairs):
        if index + 1 >= len(pairs) or not text or _is_instrumental(text):
            continue
        window = pairs[index + 1][0] - start_ms
        chars = sum(_visible_length(w) for w in _split_words(text))
        if window > 0 and chars > 0:
            rates.append(window / chars)

    if len(rates) < 6:
        return float(DEFAULT_MS_PER_CHAR)

    rates.sort()
    median = rates[len(rates) // 2]
    return float(min(max(median, MIN_MS_PER_CHAR), MAX_MS_PER_CHAR))


def _time_words(text: str, start_ms: int, end_ms: int, ms_per_char: float) -> List[Word]:
    """Distribute a line's time budget across its words."""
    words = _split_words(text)
    if not words:
        return []

    available = max(end_ms - start_ms - SWEEP_TAIL_MS, 0)
    if available <= 0:
        return [Word(w, start_ms, start_ms) for w in words]

    total_chars = sum(_visible_length(w) for w in words)
    expected = total_chars * ms_per_char

    if available <= expected * GAP_RATIO:
        # Ordinary line: the window is the singing time. Use essentially all of it.
        sweep = available * FILL_RATIO
    else:
        # An instrumental gap follows. Finish at a plausible singing pace and
        # hold, instead of dragging the highlight across the dead air.
        sweep = min(available * FILL_RATIO, expected * GAP_SWEEP_SCALE)

    sweep = int(sweep)

    weights = [_visible_length(w) + WORD_FLAT_WEIGHT for w in words]
    total_weight = sum(weights)

    timed: List[Word] = []
    cursor = float(start_ms)
    for word, weight in zip(words, weights):
        span = sweep * (weight / total_weight)
        timed.append(Word(word, int(cursor), int(cursor + span)))
        cursor += span

    # Absorb rounding drift into the final word so the sweep ends exactly where
    # the budget said it would.
    if timed:
        timed[-1].end_ms = start_ms + sweep

    return timed


def _build_lines(pairs: List[tuple[int, str]]) -> List[Line]:
    """Turn timestamped text into fully timed lines.

    Blank LRC entries are kept as instrumental markers -- they correctly bound
    the previous line's sweep -- but are not emitted as renderable lines.
    """
    ms_per_char = _median_rate(pairs)

    lines: List[Line] = []
    for index, (start_ms, text) in enumerate(pairs):
        if index + 1 < len(pairs):
            end_ms = pairs[index + 1][0]
        else:
            end_ms = start_ms + LAST_LINE_MS

        if not text:
            continue

        if _is_instrumental(text):
            lines.append(
                Line(start_ms=start_ms, end_ms=end_ms, text="", words=[], instrumental=True)
            )
            continue

        lines.append(
            Line(
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                words=_time_words(text, start_ms, end_ms, ms_per_char),
            )
        )
    return lines


# --------------------------------------------------------------------------
# Track title normalisation
# --------------------------------------------------------------------------

# Spotify decorates titles in ways LRCLIB's index does not share.
_NOISE = re.compile(
    r"\s*[\(\[][^)\]]*(remaster|remastered|feat\.?|featuring|with|version|edit|"
    r"mono|stereo|deluxe|bonus|live|radio|explicit)[^)\]]*[\)\]]",
    re.IGNORECASE,
)
_DASH_NOISE = re.compile(
    r"\s+-\s+.*(remaster|remastered|version|edit|mono|stereo|live|radio|mix)\b.*$",
    re.IGNORECASE,
)


def clean_title(title: str) -> str:
    cleaned = _NOISE.sub("", title)
    cleaned = _DASH_NOISE.sub("", cleaned)
    return cleaned.strip() or title.strip()


def primary_artist(artist: str) -> str:
    """Take the first credited artist; LRCLIB indexes on that."""
    for separator in (",", ";", " & ", " feat. ", " featuring ", " x ", " X "):
        if separator in artist:
            artist = artist.split(separator)[0]
    return artist.strip()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


class LyricsProvider:
    """LRCLIB client with a disk cache.

    The cache also stores misses. A track with no lyrics available is common,
    and without negative caching every poll of an instrumental would hit the
    network again.
    """

    def __init__(self, cache_dir: Path, timeout: float = 8.0, memory_entries: int = 64):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})

        # Memory cache in front of the disk cache. Disk hits are ~6ms, which is
        # not slow, but a track change already competes with Chromium repainting
        # the card, so keeping recent tracks resident makes a replay or a
        # back-skip genuinely instantaneous.
        self._memory: "OrderedDict[str, LyricsResult]" = OrderedDict()
        self._memory_limit = memory_entries
        self._lock = threading.Lock()

        self._prewarm()

    def _prewarm(self) -> None:
        """Open the TLS connection to LRCLIB before it is first needed.

        The first HTTPS request of a session pays DNS plus TLS handshake, which
        measured at roughly 100-200ms of the first lookup. Doing it at startup
        moves that cost off the first track change, where it is visible.
        """

        def run() -> None:
            try:
                self.session.get(f"{LRCLIB_BASE}/get", params={"track_name": "", "artist_name": ""}, timeout=self.timeout)
            except requests.RequestException:
                pass

        threading.Thread(target=run, name="lrclib-prewarm", daemon=True).start()

    def _cache_path(self, artist: str, title: str, duration_ms: int) -> Path:
        # Duration is bucketed to 5s so a slightly different reported duration
        # still hits the same cache entry.
        key = f"{artist.lower()}|{title.lower()}|{duration_ms // 5000}"
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, path: Path) -> Optional[LyricsResult]:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        lines = [
            Line(
                start_ms=item["start_ms"],
                end_ms=item["end_ms"],
                text=item["text"],
                words=[Word(**w) for w in item["words"]],
            )
            for item in payload.get("lines", [])
        ]
        return LyricsResult(
            lines=lines,
            synced=payload.get("synced", False),
            source=payload.get("source", "cache"),
            track=payload.get("track", ""),
            artist=payload.get("artist", ""),
        )

    def _write_cache(self, path: Path, result: LyricsResult) -> None:
        try:
            path.write_text(
                json.dumps(
                    {
                        "lines": [asdict(line) for line in result.lines],
                        "synced": result.synced,
                        "source": result.source,
                        "track": result.track,
                        "artist": result.artist,
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _memory_key(self, artist: str, title: str, duration_ms: int) -> str:
        return f"{artist.lower()}|{title.lower()}|{duration_ms // 5000}"

    def fetch(self, artist: str, title: str, album: str, duration_ms: int) -> LyricsResult:
        memory_key = self._memory_key(artist, title, duration_ms)
        with self._lock:
            hit = self._memory.get(memory_key)
            if hit is not None:
                self._memory.move_to_end(memory_key)
                return hit

        cache_path = self._cache_path(artist, title, duration_ms)
        cached = self._read_cache(cache_path)
        if cached is not None:
            self._remember(memory_key, cached)
            return cached

        result = self._fetch_remote(artist, title, album, duration_ms)
        self._write_cache(cache_path, result)
        self._remember(memory_key, result)
        return result

    def _remember(self, key: str, result: LyricsResult) -> None:
        with self._lock:
            self._memory[key] = result
            self._memory.move_to_end(key)
            while len(self._memory) > self._memory_limit:
                self._memory.popitem(last=False)

    def _fetch_remote(self, artist: str, title: str, album: str, duration_ms: int) -> LyricsResult:
        """Try every lookup strategy at once, then pick by preference.

        These used to run in sequence, so a track that only the fuzzy search
        could resolve paid for two failed exact lookups first -- measured at
        427ms against 165ms for a track that hit on the first try. Run
        concurrently, the slowest strategy sets the total, not the sum.

        Preference order still favours the exact endpoints, because only they
        match on duration and can therefore tell an album cut from a radio edit.
        """
        duration_s = round(duration_ms / 1000) if duration_ms else 0
        clean = clean_title(title)
        primary = primary_artist(artist)

        strategies: List[Callable[[], Optional[LyricsResult]]] = [
            lambda: self._api_get(artist, title, album, duration_s),
        ]
        if (clean, primary) != (title, artist):
            strategies.append(lambda: self._api_get(primary, clean, album, duration_s))
        strategies.append(lambda: self._api_search(primary, clean, duration_s))

        results: List[Optional[LyricsResult]] = [None] * len(strategies)
        pending = set(range(len(strategies)))

        pool = ThreadPoolExecutor(max_workers=len(strategies))
        try:
            futures = {pool.submit(fn): i for i, fn in enumerate(strategies)}
            for future in as_completed(futures):
                index = futures[future]
                pending.discard(index)
                try:
                    results[index] = future.result()
                except Exception:
                    results[index] = None

                # Return as soon as the winner is decided, rather than waiting
                # for the slowest strategy. A track that hits the first exact
                # lookup must not be held up by a fuzzy search it does not need.
                decided = self._decide(results, pending)
                if decided is not None:
                    return decided
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Everything finished with nothing synced; settle for unsynced words.
        for result in results:
            if result is not None and result.lines:
                return result
        return LyricsResult(lines=[], synced=False, source="none")

    @staticmethod
    def _decide(
        results: List[Optional[LyricsResult]], pending: set[int]
    ) -> Optional[LyricsResult]:
        """Return the winning result if no still-running strategy could beat it.

        Strategies are in preference order, so a result at index `i` only wins
        once every strategy before it has finished and lost.
        """
        for index, result in enumerate(results):
            if index in pending:
                return None  # A higher-preference strategy may still win.
            if result is not None and result.synced and result.lines:
                return result
        return None

    def _api_get(self, artist: str, title: str, album: str, duration_s: int) -> Optional[LyricsResult]:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration_s:
            params["duration"] = duration_s
        try:
            response = self.session.get(f"{LRCLIB_BASE}/get", params=params, timeout=self.timeout)
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        try:
            return self._to_result(response.json(), source="lrclib:get")
        except (ValueError, KeyError):
            return None

    def _api_search(self, artist: str, title: str, duration_s: int) -> LyricsResult:
        try:
            response = self.session.get(
                f"{LRCLIB_BASE}/search",
                params={"track_name": title, "artist_name": artist},
                timeout=self.timeout,
            )
            candidates = response.json() if response.status_code == 200 else []
        except (requests.RequestException, ValueError):
            candidates = []

        synced = [c for c in candidates if c.get("syncedLyrics")]
        if not synced:
            # Last resort: an unsynced result still lets the overlay show the
            # correct words, just without a moving highlight.
            for candidate in candidates:
                if candidate.get("plainLyrics"):
                    return self._to_result(candidate, source="lrclib:search")
            return LyricsResult(lines=[], synced=False, source="none")

        if duration_s:
            synced.sort(key=lambda c: abs((c.get("duration") or 0) - duration_s))

        return self._to_result(synced[0], source="lrclib:search")

    def _to_result(self, payload: dict, source: str) -> LyricsResult:
        track = payload.get("trackName", "") or ""
        artist = payload.get("artistName", "") or ""

        raw_synced = payload.get("syncedLyrics")
        if raw_synced:
            pairs = _parse_lrc(raw_synced)
            lines = _build_lines(pairs)
            if lines:
                return LyricsResult(lines, synced=True, source=source, track=track, artist=artist)

        raw_plain = payload.get("plainLyrics")
        if raw_plain:
            # Unsynced: emit lines with no timing so the UI can show static text.
            lines = [
                Line(start_ms=0, end_ms=0, text=text.strip(), words=[])
                for text in raw_plain.splitlines()
                if text.strip()
            ]
            return LyricsResult(lines, synced=False, source=source, track=track, artist=artist)

        return LyricsResult(lines=[], synced=False, source="none", track=track, artist=artist)
