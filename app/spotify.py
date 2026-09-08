"""Optional Spotify Web API client, used only to read the play queue.

Why this exists
---------------
SMTC tells us what is playing *now* and nothing about what comes next, so the
first play of any track pays a lyrics round-trip. Spotify's Web API does expose
the queue, which lets the next track's lyrics be fetched while the current one
is still playing -- turning that round-trip into a cache hit.

Everything here is optional. With no client ID configured the app runs entirely
on SMTC, exactly as before.

Auth
----
Uses the PKCE authorization-code flow, which is the flow intended for apps that
cannot keep a secret. Only a client ID is needed; there is no client secret to
distribute, so the app can be shared and each person supplies their own ID.

The refresh token is stored in plain text in the config file. That is normal for
a local desktop app -- it is protected by the user's own filesystem permissions
and grants only the read-only scope below -- but it is worth knowing it is there.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, List, Optional, Tuple

import requests

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Read-only. This scope cannot modify playback, only observe it.
SCOPE = "user-read-playback-state"

REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 8888
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"

# Refresh a little before actual expiry so a request never races the deadline.
EXPIRY_MARGIN_S = 60


class SpotifyAuthError(Exception):
    """Raised when authorisation cannot be completed."""


def _pkce_pair() -> Tuple[str, str]:
    """Return (verifier, challenge) for a PKCE exchange."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


class _CallbackHandler(BaseHTTPRequestHandler):
    """Catches the single redirect Spotify makes back to localhost."""

    result: Dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        query = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(query))
        type(self).result = params

        ok = "code" in params
        body = (
            "<h2>Connected.</h2><p>You can close this tab and return to the overlay.</p>"
            if ok
            else f"<h2>Authorisation failed.</h2><p>{params.get('error', 'No code returned.')}</p>"
        )
        payload = f"<html><body style='font-family:system-ui;padding:3rem'>{body}</body></html>"
        encoded = payload.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args) -> None:
        """Silence the default stderr access log."""


class SpotifyClient:
    """Minimal Spotify client: authorise once, then read the queue."""

    def __init__(self, client_id: str, refresh_token: str = "", timeout: float = 8.0):
        self.client_id = client_id.strip()
        self.refresh_token = refresh_token.strip()
        self.timeout = timeout

        self._access_token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

        self.session = requests.Session()

    # -- authorisation ------------------------------------------------------

    @property
    def connected(self) -> bool:
        return bool(self.client_id and self.refresh_token)

    def authorize(self, on_done: Callable[[bool, str], None]) -> None:
        """Run the PKCE flow in the background, opening the user's browser.

        Calls `on_done(success, message)` when finished. The local server binds
        only to the loopback interface and serves exactly one request.
        """

        def run() -> None:
            try:
                verifier, challenge = _pkce_pair()
                state = secrets.token_urlsafe(16)

                query = urllib.parse.urlencode(
                    {
                        "client_id": self.client_id,
                        "response_type": "code",
                        "redirect_uri": REDIRECT_URI,
                        "code_challenge_method": "S256",
                        "code_challenge": challenge,
                        "scope": SCOPE,
                        "state": state,
                    }
                )

                _CallbackHandler.result = {}
                try:
                    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _CallbackHandler)
                except OSError as exc:
                    raise SpotifyAuthError(
                        f"Cannot listen on {REDIRECT_URI} — is something else using port "
                        f"{REDIRECT_PORT}? ({exc})"
                    ) from exc

                server.timeout = 180
                webbrowser.open(f"{AUTH_URL}?{query}")
                server.handle_request()
                server.server_close()

                params = _CallbackHandler.result
                if params.get("state") != state:
                    raise SpotifyAuthError("State mismatch — ignoring the response.")
                if "code" not in params:
                    raise SpotifyAuthError(params.get("error", "No authorisation code returned."))

                response = self.session.post(
                    TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": params["code"],
                        "redirect_uri": REDIRECT_URI,
                        "client_id": self.client_id,
                        "code_verifier": verifier,
                    },
                    timeout=self.timeout,
                )
                if response.status_code != 200:
                    raise SpotifyAuthError(f"Token exchange failed ({response.status_code}).")

                payload = response.json()
                with self._lock:
                    self._access_token = payload.get("access_token", "")
                    self.refresh_token = payload.get("refresh_token", "")
                    self._expires_at = time.monotonic() + payload.get("expires_in", 3600)

                on_done(True, "Connected to Spotify.")
            except SpotifyAuthError as exc:
                on_done(False, str(exc))
            except Exception as exc:  # network, JSON, browser launch
                on_done(False, f"Could not connect: {exc}")

        threading.Thread(target=run, name="spotify-auth", daemon=True).start()

    def _ensure_token(self) -> bool:
        with self._lock:
            fresh = self._access_token and time.monotonic() < self._expires_at - EXPIRY_MARGIN_S
            if fresh:
                return True
            refresh_token = self.refresh_token

        if not (self.client_id and refresh_token):
            return False

        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                },
                timeout=self.timeout,
            )
        except requests.RequestException:
            return False

        if response.status_code != 200:
            return False

        try:
            payload = response.json()
        except ValueError:
            return False

        with self._lock:
            self._access_token = payload.get("access_token", "")
            self._expires_at = time.monotonic() + payload.get("expires_in", 3600)
            # Spotify rotates refresh tokens on some grants; keep the new one.
            if payload.get("refresh_token"):
                self.refresh_token = payload["refresh_token"]

        return bool(self._access_token)

    # -- queue --------------------------------------------------------------

    def upcoming(self, limit: int = 3) -> List[Dict[str, str]]:
        """Return the next few queued tracks as {artist, title, album, duration_ms}.

        Returns an empty list on any failure. This is a best-effort optimisation,
        so a hiccup here must never surface as an error in the overlay.
        """
        if not self._ensure_token():
            return []

        with self._lock:
            token = self._access_token

        try:
            response = self.session.get(
                f"{API_BASE}/me/player/queue",
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return []

        if response.status_code != 200:
            return []

        try:
            payload = response.json()
        except ValueError:
            return []

        tracks: List[Dict[str, str]] = []
        for item in (payload.get("queue") or [])[:limit]:
            if not isinstance(item, dict) or item.get("type") != "track":
                continue
            artists = item.get("artists") or []
            tracks.append(
                {
                    "artist": artists[0].get("name", "") if artists else "",
                    "title": item.get("name", ""),
                    "album": (item.get("album") or {}).get("name", ""),
                    "duration_ms": item.get("duration_ms", 0),
                }
            )
        return tracks
