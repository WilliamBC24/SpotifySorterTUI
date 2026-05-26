#!/usr/bin/env python3
"""Simple TUI with Spotify PKCE login and playlist listing."""

from __future__ import annotations

import curses
import base64
import datetime
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import threading
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"
SPOTIFY_PLAYLIST_FIELDS = "items(id,name,snapshot_id,owner(id),tracks(total),items(total)),next,total"
SPOTIFY_PLAYLIST_ITEMS_FIELDS = "items(item(name,artists(name))),next,total"
SPOTIFY_PLAYLIST_TRACKS_FIELDS = "items(track(name,artists(name))),next,total"
SPOTIFY_PLAYLIST_TRACKS_LIMIT = 50
SPOTIFY_PLAYLIST_TRACKS_URL_TEMPLATE = "https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
SPOTIFY_PLAYLIST_ITEMS_URL_TEMPLATE = "https://api.spotify.com/v1/playlists/{playlist_id}/items"
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative user-library-read user-read-private user-read-email"
SPOTIFY_MAX_RETRIES = 4
TOKEN_EXPIRY_BUFFER_SECONDS = 30
DEFAULT_TOKEN_EXPIRY_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 1.0
PKCE_VERIFIER_BYTES = 64
DEFAULT_PLAYLIST_NAME = "Unnamed Playlist"
DEFAULT_TRACK_NAME = "Unknown Track"
UI_POLL_INTERVAL_MS = 100
DEFAULT_SPOTIFY_SYNC_INTERVAL_SECONDS = 60
UI_HELP_TEXT = "c: connect (disconnected only)  ↑/↓: move selection  Enter: open songs  q: quit"
MIN_COLS_FOR_SPLIT_PANE = 70
MIN_LEFT_PANEL_WIDTH = 24
ENTER_KEY_CODES = (curses.KEY_ENTER, 10, 13)


@dataclass(slots=True)
class PlaylistInfo:
    id: str
    name: str
    track_total: int
    snapshot_id: str = ""
    owner_id: str = ""
    tracks: list[str] = field(default_factory=list)
    tracks_loaded: bool = False


@dataclass(slots=True)
class SpotifySession:
    client_id: str
    token_cache: dict[str, object]


class SpotifyApiError(RuntimeError):
    def __init__(self, status_code: int, error_message: str) -> None:
        super().__init__(f"Spotify API error {status_code}: {error_message}")
        self.status_code = status_code
        self.error_message = error_message


@dataclass(slots=True)
class UiState:
    connection_status: str = "disconnected"
    status_message: str = "Press c to connect to Spotify with PKCE."
    error_message: str = ""
    playlists: list[PlaylistInfo] = field(default_factory=list)
    selected_index: int = 0
    opened_playlist_id: str | None = None
    session: SpotifySession | None = None


def _base64_url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _build_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(PKCE_VERIFIER_BYTES)
    challenge_raw = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = _base64_url_encode(challenge_raw)
    return code_verifier, code_challenge


def _open_authorization_page(auth_url: str) -> None:
    parsed_auth_url = urllib.parse.urlparse(auth_url)
    if parsed_auth_url.scheme != "https" or not parsed_auth_url.netloc:
        raise RuntimeError("Generated Spotify authorization URL is invalid.")

    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        try:
            subprocess.Popen(
                [xdg_open, auth_url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            return
        except OSError:
            pass

    if webbrowser.open(auth_url, new=1, autoraise=True):
        return

    raise RuntimeError(
        "Unable to open a browser automatically. "
        f"Open this URL manually to continue: {auth_url}"
    )


def _validate_redirect_uri(redirect_uri: str) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(redirect_uri)
    if not parsed.hostname:
        raise RuntimeError("SPOTIFY_REDIRECT_URI must include a hostname.")
    if parsed.scheme == "https":
        return parsed
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
        return parsed
    raise RuntimeError(
        "SPOTIFY_REDIRECT_URI must use https://, except http://127.0.0.1 for local development."
    )


def _wait_for_auth_code(redirect_uri: str, expected_state: str, timeout_seconds: int = 180) -> str:
    parsed = _validate_redirect_uri(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port:
        raise RuntimeError(
            "Local callback listener requires SPOTIFY_REDIRECT_URI like http://127.0.0.1:8888/callback."
        )

    result: dict[str, str] = {}
    done = threading.Event()

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            request_url = urllib.parse.urlparse(self.path)
            if request_url.path != parsed.path:
                self.send_response(404)
                self.end_headers()
                return

            query = urllib.parse.parse_qs(request_url.query)
            state = query.get("state", [""])[0]
            code = query.get("code", [""])[0]
            error = query.get("error", [""])[0]

            if state != expected_state:
                result["error"] = "Invalid OAuth state returned by Spotify."
            elif error:
                result["error"] = f"Spotify authorization error: {error}"
            elif not code:
                result["error"] = "No authorization code returned by Spotify."
            else:
                result["code"] = code

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Spotify authorization complete.</h2>"
                b"<p>You can return to the terminal.</p></body></html>"
            )
            done.set()

        def log_message(self, fmt: str, *args: object) -> None:
            pass

    server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if done.wait(timeout=0.25):
                break
        else:
            raise TimeoutError("Timed out waiting for Spotify authorization callback.")
    finally:
        server.shutdown()
        server.server_close()

    if "error" in result:
        raise RuntimeError(result["error"])
    return result["code"]


def _read_error_message(error_body: bytes, status_code: int) -> str:
    default_message = f"HTTP {status_code}"
    if not error_body:
        return default_message
    try:
        payload = json.loads(error_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default_message

    if isinstance(payload, dict):
        nested_error = payload.get("error")
        if isinstance(nested_error, dict):
            message = nested_error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        if isinstance(nested_error, str):
            description = payload.get("error_description")
            if isinstance(description, str) and description.strip():
                return f"{nested_error}: {description.strip()}"
            if nested_error.strip():
                return nested_error.strip()
    return default_message


def _safe_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _safe_non_empty_string(value: object, default: str) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized:
            return normalized
    return default


def _spotify_request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form_body: dict[str, str] | None = None,
    max_retries: int = SPOTIFY_MAX_RETRIES,
) -> dict[str, object]:
    body: bytes | None = None
    request_headers = dict(headers or {})
    if form_body is not None:
        body = urllib.parse.urlencode(form_body).encode("utf-8")
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"

    backoff_seconds = INITIAL_BACKOFF_SECONDS
    for attempt in range(max_retries + 1):
        request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                response_text = response.read().decode("utf-8")
            payload = json.loads(response_text) if response_text else {}
            if isinstance(payload, dict):
                return payload
            raise RuntimeError("Spotify returned an unexpected response format.")
        except urllib.error.HTTPError as exc:
            error_body = exc.read()
            error_message = _read_error_message(error_body, exc.code)
            if exc.code == 429 and attempt < max_retries:
                retry_after_header = exc.headers.get("Retry-After", "").strip()
                try:
                    sleep_seconds = max(0.0, float(retry_after_header))
                except ValueError:
                    sleep_seconds = backoff_seconds
                    try:
                        retry_after_time = datetime.datetime.strptime(
                            retry_after_header,
                            "%a, %d %b %Y %H:%M:%S GMT",
                        ).replace(tzinfo=datetime.timezone.utc)
                        now_utc = datetime.datetime.now(datetime.timezone.utc)
                        sleep_seconds = max(0.0, (retry_after_time - now_utc).total_seconds())
                    except ValueError:
                        pass
                time.sleep(sleep_seconds)
                backoff_seconds *= 2.0
                continue
            raise SpotifyApiError(exc.code, error_message) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Network error while contacting Spotify: {exc.reason}") from exc

    raise RuntimeError("Spotify request failed after retries.")


def _exchange_code_for_token(
    client_id: str, redirect_uri: str, code: str, code_verifier: str
) -> dict[str, object]:
    payload = _spotify_request_json(
        SPOTIFY_TOKEN_URL,
        method="POST",
        form_body={
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("No access token received from Spotify.")
    expires_in = _safe_int(payload.get("expires_in"), DEFAULT_TOKEN_EXPIRY_SECONDS)
    return {
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token"),
        "expires_at": time.time() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS,
    }


def _refresh_access_token(client_id: str, refresh_token: str) -> dict[str, object]:
    payload = _spotify_request_json(
        SPOTIFY_TOKEN_URL,
        method="POST",
        form_body={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    )
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("Spotify refresh response did not include an access token.")
    expires_in = _safe_int(payload.get("expires_in"), DEFAULT_TOKEN_EXPIRY_SECONDS)
    return {
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token", refresh_token),
        "expires_at": time.time() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS,
    }


def _get_access_token(
    client_id: str,
    token_cache: dict[str, object],
    force_refresh: bool = False,
) -> str:
    access_token = token_cache.get("access_token")
    raw_expires_at = token_cache.get("expires_at", 0)
    if isinstance(raw_expires_at, (float, int)):
        expires_at = float(raw_expires_at)
    elif isinstance(raw_expires_at, str):
        try:
            expires_at = float(raw_expires_at)
        except ValueError:
            expires_at = 0.0
    else:
        expires_at = 0.0

    current_time = time.time()
    if not force_refresh and isinstance(access_token, str) and access_token and current_time < expires_at:
        return access_token

    refresh_token = token_cache.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("Access token expired and no refresh token is available.")
    refreshed = _refresh_access_token(client_id, refresh_token)
    token_cache.update(refreshed)
    return str(token_cache["access_token"])


def _parse_playlist_item(item: object) -> PlaylistInfo | None:
    if not isinstance(item, dict):
        return None
    playlist_id = item.get("id")
    if not isinstance(playlist_id, str) or not playlist_id:
        return None
    name = _safe_non_empty_string(item.get("name"), DEFAULT_PLAYLIST_NAME)
    snapshot_id = _safe_non_empty_string(item.get("snapshot_id"), "")
    owner_id = ""
    owner = item.get("owner")
    if isinstance(owner, dict):
        owner_id = _safe_non_empty_string(owner.get("id"), "")
    tracks_value = item.get("tracks")
    if not isinstance(tracks_value, dict):
        tracks_value = item.get("items")
    track_total = 0
    if isinstance(tracks_value, dict):
        track_total = _safe_int(tracks_value.get("total"), 0)
    return PlaylistInfo(
        id=playlist_id,
        name=name,
        track_total=max(0, track_total),
        snapshot_id=snapshot_id,
        owner_id=owner_id,
    )


def _fetch_current_user_id(
    client_id: str,
    token_cache: dict[str, object],
) -> str:
    access_token = _get_access_token(client_id, token_cache)
    payload = _spotify_request_json(
        "https://api.spotify.com/v1/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return _safe_non_empty_string(payload.get("id"), "")


def _parse_track_item(item: object) -> str:
    if not isinstance(item, dict):
        return DEFAULT_TRACK_NAME
    track = item.get("item")
    if not isinstance(track, dict):
        track = item.get("track")
    if not isinstance(track, dict):
        return DEFAULT_TRACK_NAME
    track_name = _safe_non_empty_string(track.get("name"), DEFAULT_TRACK_NAME)
    artists_raw = track.get("artists", [])
    artist_names: list[str] = []
    if isinstance(artists_raw, list):
        for artist in artists_raw:
            if not isinstance(artist, dict):
                continue
            artist_name = _safe_non_empty_string(artist.get("name"), "")
            if artist_name:
                artist_names.append(artist_name)
    if not artist_names:
        return track_name
    return f"{track_name} — {', '.join(artist_names)}"


def _fetch_playlist_tracks_page(
    client_id: str,
    token_cache: dict[str, object],
    playlist_id: str,
    *,
    endpoint_url_template: str,
    fields: str,
    force_refresh: bool = False,
) -> list[str]:
    track_entries: list[str] = []
    query = urllib.parse.urlencode(
        {
            "limit": SPOTIFY_PLAYLIST_TRACKS_LIMIT,
            "fields": fields,
        }
    )
    quoted_playlist_id = urllib.parse.quote(playlist_id, safe="")
    next_url = endpoint_url_template.format(playlist_id=quoted_playlist_id)
    next_url = f"{next_url}?{query}"
    page_num = 0
    while next_url:
        page_num += 1
        access_token = _get_access_token(client_id, token_cache, force_refresh=force_refresh)
        payload = _spotify_request_json(
            next_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        items = payload.get("items", [])
        if not isinstance(items, list):
            items = []
        for item in items:
            parsed_track = _parse_track_item(item)
            track_entries.append(parsed_track)
        raw_next = payload.get("next")
        next_url = raw_next if isinstance(raw_next, str) and raw_next else ""
    return track_entries


def _fetch_playlist_tracks(
    client_id: str,
    token_cache: dict[str, object],
    playlist_id: str,
    force_refresh: bool = False,
) -> list[str]:
    try:
        return _fetch_playlist_tracks_page(
            client_id,
            token_cache,
            playlist_id,
            endpoint_url_template=SPOTIFY_PLAYLIST_ITEMS_URL_TEMPLATE,
            fields=SPOTIFY_PLAYLIST_ITEMS_FIELDS,
            force_refresh=force_refresh,
        )
    except SpotifyApiError as exc:
        if exc.status_code != 403:
            raise
        return _fetch_playlist_tracks_page(
            client_id,
            token_cache,
            playlist_id,
            endpoint_url_template=SPOTIFY_PLAYLIST_TRACKS_URL_TEMPLATE,
            fields=SPOTIFY_PLAYLIST_TRACKS_FIELDS,
            force_refresh=force_refresh,
        )


def _fetch_user_playlists(
    client_id: str,
    token_cache: dict[str, object],
    status_callback: Callable[[str], None] | None = None,
) -> list[PlaylistInfo]:
    playlists: list[PlaylistInfo] = []
    query = urllib.parse.urlencode(
        {
            "limit": 50,
            "fields": SPOTIFY_PLAYLIST_FIELDS,
        }
    )
    next_url = f"{SPOTIFY_PLAYLISTS_URL}?{query}"
    page_count = 0

    while next_url:
        page_count += 1
        if status_callback is not None:
            status_callback(f"Fetching playlists from Spotify (page {page_count})...")
        access_token = _get_access_token(client_id, token_cache)
        payload = _spotify_request_json(
            next_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        items = payload.get("items", [])
        if not isinstance(items, list):
            items = []
        for item in items:
            playlist_info = _parse_playlist_item(item)
            if playlist_info is not None:
                playlists.append(playlist_info)

        raw_next = payload.get("next")
        next_url = raw_next if isinstance(raw_next, str) and raw_next else ""

    return playlists


def _hydrate_playlist_tracks(
    client_id: str,
    token_cache: dict[str, object],
    current_user_id: str,
    playlists: list[PlaylistInfo],
    status_callback: Callable[[str], None] | None = None,
) -> list[PlaylistInfo]:
    hydrated: list[PlaylistInfo] = []
    total = len(playlists)
    for index, playlist in enumerate(playlists, start=1):
        if current_user_id and playlist.owner_id and playlist.owner_id != current_user_id:
            if status_callback is not None:
                status_callback(f"Skipping followed playlist: {playlist.name}")
            continue
        if status_callback is not None:
            status_callback(f"Fetching tracks for playlist {index}/{total}: {playlist.name}")
        try:
            tracks = _fetch_playlist_tracks(client_id, token_cache, playlist.id)
            updated_total = len(tracks)
        except SpotifyApiError as exc:
            if exc.status_code != 403:
                raise
            if status_callback is not None:
                status_callback(f"Skipping playlist due to Spotify permissions (403): {playlist.name}")
            continue
        hydrated.append(
            PlaylistInfo(
                id=playlist.id,
                name=playlist.name,
                track_total=updated_total,
                snapshot_id=playlist.snapshot_id,
                owner_id=playlist.owner_id,
                tracks=tracks,
                tracks_loaded=bool(tracks),
            )
        )
    return hydrated


def _sync_playlists(
    session: SpotifySession,
    current_user_id: str,
    status_callback: Callable[[str], None] | None = None,
) -> list[PlaylistInfo]:
    playlists = _fetch_user_playlists(session.client_id, session.token_cache, status_callback=status_callback)
    return _hydrate_playlist_tracks(
        session.client_id,
        session.token_cache,
        current_user_id,
        playlists,
        status_callback,
    )


def connect_and_get_session_playlists(
    status_callback: Callable[[str], None] | None = None,
) -> tuple[SpotifySession, list[PlaylistInfo]]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()
    if not client_id:
        raise RuntimeError("Set SPOTIFY_CLIENT_ID before connecting to Spotify.")
    _validate_redirect_uri(redirect_uri)

    code_verifier, code_challenge = _build_pkce_pair()
    state = secrets.token_urlsafe(32)
    auth_query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SPOTIFY_SCOPE,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
        }
    )
    auth_url = f"{SPOTIFY_AUTH_URL}?{auth_query}"

    if status_callback is not None:
        status_callback("Opening Spotify authorization in browser...")
    _open_authorization_page(auth_url)
    if status_callback is not None:
        status_callback("Waiting for Spotify login approval in browser...")
    code = _wait_for_auth_code(redirect_uri, state)
    if status_callback is not None:
        status_callback("Authorization approved. Fetching playlists from Spotify...")
    tokens = _exchange_code_for_token(client_id, redirect_uri, code, code_verifier)
    session = SpotifySession(client_id=client_id, token_cache=tokens)
    current_user_id = _fetch_current_user_id(session.client_id, session.token_cache)
    playlists = _sync_playlists(session, current_user_id, status_callback=status_callback)
    return session, playlists


def _clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return min(max(0, index), total - 1)


def _find_playlist_by_id(playlists: list[PlaylistInfo], playlist_id: str | None) -> PlaylistInfo | None:
    if not playlist_id:
        return None
    for playlist in playlists:
        if playlist.id == playlist_id:
            return playlist
    return None


def _add_line(
    stdscr: curses.window,
    row: int,
    col: int,
    text: str,
    width: int,
    attr: int = curses.A_NORMAL,
) -> None:
    if width <= 0:
        return
    rows, _cols = stdscr.getmaxyx()
    if row < 0 or row >= rows:
        return
    stdscr.addnstr(row, col, text, width, attr)


def _wrap_text_lines(text: str, width: int) -> list[str]:
    if width <= 0:
        return []
    lines: list[str] = []
    for raw_line in text.splitlines() or [""]:
        wrapped = textwrap.wrap(
            raw_line,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        lines.extend(wrapped or [""])
    return lines


def _add_wrapped_text(
    stdscr: curses.window,
    row: int,
    col: int,
    text: str,
    width: int,
    attr: int = curses.A_NORMAL,
) -> int:
    rows, _cols = stdscr.getmaxyx()
    if width <= 0 or row >= rows:
        return 0
    lines = _wrap_text_lines(text, width)
    drawn = 0
    for line in lines:
        draw_row = row + drawn
        if draw_row >= rows:
            break
        _add_line(stdscr, draw_row, col, line, width, attr)
        drawn += 1
    return drawn


def run(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.timeout(UI_POLL_INTERVAL_MS)
    stdscr.keypad(True)

    connection_lock = threading.Lock()
    state = UiState()
    stop_sync_event = threading.Event()
    sync_interval_seconds = max(
        10,
        _safe_int(
            os.getenv("SPOTIFY_SYNC_INTERVAL_SECONDS", str(DEFAULT_SPOTIFY_SYNC_INTERVAL_SECONDS)),
            DEFAULT_SPOTIFY_SYNC_INTERVAL_SECONDS,
        ),
    )

    def connect_worker() -> None:
        def update_status(message: str) -> None:
            with connection_lock:
                state.status_message = message

        try:
            established_session, fetched_playlists = connect_and_get_session_playlists(
                status_callback=update_status
            )
            with connection_lock:
                state.session = established_session
                state.playlists = fetched_playlists
                state.selected_index = 0
                state.error_message = ""
                state.connection_status = "connected"
                if state.playlists:
                    state.status_message = (
                        f"Connected. Synced {len(state.playlists)} playlist(s). Use up/down and Enter."
                    )
                else:
                    state.status_message = "Connected. No playlists found for this user."
        except (
            RuntimeError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            with connection_lock:
                state.session = None
                state.connection_status = "disconnected"
                state.error_message = f"Connection failed: {exc}"
                state.status_message = "Press c to retry Spotify connection."

    def sync_worker() -> None:
        def update_sync_status(message: str) -> None:
            with connection_lock:
                if state.connection_status == "connected":
                    state.status_message = message

        while not stop_sync_event.wait(timeout=sync_interval_seconds):
            try:
                with connection_lock:
                    active_session = state.session
                    current_user_id = ""
                    is_connected = (
                        state.connection_status == "connected" and active_session is not None
                    )
                    if is_connected:
                        state.status_message = "Connected. Syncing Spotify updates..."
                if not is_connected:
                    continue
                try:
                    current_user_id = _fetch_current_user_id(
                        active_session.client_id,
                        active_session.token_cache,
                    )
                except Exception as exc:
                    with connection_lock:
                        state.session = None
                        state.connection_status = "disconnected"
                        state.error_message = f"Connection lost: {exc}"
                        state.status_message = "Connection lost. Press c to reconnect."
                    continue
                refreshed_playlists = _sync_playlists(
                    active_session,
                    current_user_id,
                    status_callback=update_sync_status,
                )
                with connection_lock:
                    state.playlists = refreshed_playlists
                    state.selected_index = _clamp_index(state.selected_index, len(state.playlists))
                    if state.opened_playlist_id and not _find_playlist_by_id(
                        state.playlists, state.opened_playlist_id
                    ):
                        state.opened_playlist_id = None
                    state.error_message = ""
                    state.status_message = f"Connected. Last synced at {time.strftime('%H:%M:%S')}."
            except Exception as exc:
                with connection_lock:
                    state.session = None
                    state.connection_status = "disconnected"
                    state.error_message = f"Connection lost: {exc}"
                    state.status_message = "Connection lost. Press c to reconnect."

    def load_playlist_tracks_worker(playlist_id: str) -> None:
        with connection_lock:
            active_session = state.session
            if active_session is None or state.connection_status != "connected":
                return
        try:
            fetched_tracks = _fetch_playlist_tracks(
                active_session.client_id,
                active_session.token_cache,
                playlist_id,
            )
            with connection_lock:
                found = False
                for idx, playlist in enumerate(state.playlists):
                    if playlist.id != playlist_id:
                        continue
                    found = True
                    state.playlists[idx] = PlaylistInfo(
                        id=playlist.id,
                        name=playlist.name,
                        track_total=len(fetched_tracks),
                        snapshot_id=playlist.snapshot_id,
                        owner_id=playlist.owner_id,
                        tracks=fetched_tracks,
                        tracks_loaded=True,
                    )
                    break
                state.error_message = ""
                state.status_message = f"Loaded {len(fetched_tracks)} track(s) from selected playlist."
        except (
            RuntimeError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            SpotifyApiError,
        ) as exc:
            with connection_lock:
                if isinstance(exc, SpotifyApiError) and exc.status_code == 403:
                    state.playlists = [playlist for playlist in state.playlists if playlist.id != playlist_id]
                    state.selected_index = _clamp_index(state.selected_index, len(state.playlists))
                    if state.opened_playlist_id == playlist_id:
                        state.opened_playlist_id = None
                    state.error_message = ""
                    state.status_message = "Hidden playlist that Spotify does not allow this app to read."
                else:
                    state.error_message = f"Unable to load selected playlist tracks: {exc}"

    threading.Thread(target=sync_worker, daemon=True).start()

    while True:
        with connection_lock:
            status_snapshot = state.status_message
            error_snapshot = state.error_message
            playlists_snapshot = list(state.playlists)
            selected_snapshot = state.selected_index
            opened_playlist_snapshot = state.opened_playlist_id

        stdscr.erase()
        rows, cols = stdscr.getmaxyx()

        title = "Spotify Playlist Viewer TUI"
        width = max(1, cols - 1)
        left_panel_width = (
            width if cols < MIN_COLS_FOR_SPLIT_PANE else max(MIN_LEFT_PANEL_WIDTH, (width // 2) - 1)
        )
        right_panel_col = left_panel_width + 2
        right_panel_width = max(0, width - right_panel_col)

        content_row = 0
        content_row += _add_wrapped_text(stdscr, content_row, 0, title, width)
        content_row += _add_wrapped_text(stdscr, content_row, 0, UI_HELP_TEXT, width)
        if content_row < rows:
            stdscr.hline(content_row, 0, "-", width)
        content_row += 1
        content_row += _add_wrapped_text(stdscr, content_row, 0, status_snapshot, width)
        if error_snapshot:
            content_row += _add_wrapped_text(stdscr, content_row, 0, error_snapshot, width)

        list_header_row = content_row
        playlist_header_lines = _add_wrapped_text(stdscr, list_header_row, 0, "Playlists:", left_panel_width)
        left_list_start_row = list_header_row + max(1, playlist_header_lines)

        if not playlists_snapshot:
            _add_wrapped_text(stdscr, left_list_start_row, 0, "No playlists loaded yet.", left_panel_width)
        else:
            max_visible = max(1, rows - (left_list_start_row + 1))
            start_index = max(0, selected_snapshot - max_visible + 1)
            visible = playlists_snapshot[start_index : start_index + max_visible]
            left_draw_row = left_list_start_row
            for row_offset, playlist in enumerate(visible):
                if left_draw_row >= rows:
                    break
                playlist_index = start_index + row_offset
                line = f"{playlist_index + 1}. {playlist.name} ({playlist.track_total} tracks)"
                attr = curses.A_REVERSE if playlist_index == selected_snapshot else curses.A_NORMAL
                left_draw_row += _add_wrapped_text(stdscr, left_draw_row, 0, line, left_panel_width, attr)

        if right_panel_width > 0:
            for row in range(list_header_row, rows):
                _add_line(stdscr, row, left_panel_width + 1, "│", 1)
            opened_playlist = _find_playlist_by_id(playlists_snapshot, opened_playlist_snapshot)
            songs_header_lines = _add_wrapped_text(
                stdscr, list_header_row, right_panel_col, "Songs:", right_panel_width
            )
            right_list_start_row = list_header_row + max(1, songs_header_lines)
            if opened_playlist is None:
                _add_line(
                    stdscr,
                    right_list_start_row,
                    right_panel_col,
                    "Press Enter on a playlist to open songs.",
                    right_panel_width,
                )
            else:
                header = f"{opened_playlist.name} ({len(opened_playlist.tracks)} tracks)"
                songs_row = right_list_start_row
                songs_row += _add_wrapped_text(stdscr, songs_row, right_panel_col, header, right_panel_width)
                for idx, track_name in enumerate(opened_playlist.tracks):
                    if songs_row >= rows:
                        break
                    songs_row += _add_wrapped_text(
                        stdscr,
                        songs_row,
                        right_panel_col,
                        f"{idx + 1}. {track_name}",
                        right_panel_width,
                    )

        stdscr.refresh()

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            stop_sync_event.set()
            break
        if key in (ord("c"), ord("C")):
            should_start_connection = False
            with connection_lock:
                if state.connection_status == "connecting":
                    state.status_message = "Connection already in progress..."
                elif state.connection_status == "connected":
                    state.status_message = "Already connected. Waiting for automatic sync updates."
                else:
                    state.status_message = "Connecting to Spotify..."
                    state.error_message = ""
                    state.connection_status = "connecting"
                    should_start_connection = True
            if should_start_connection:
                threading.Thread(target=connect_worker, daemon=True).start()
            continue
        if key == curses.KEY_UP:
            with connection_lock:
                if state.playlists:
                    state.selected_index = _clamp_index(state.selected_index - 1, len(state.playlists))
            continue
        if key == curses.KEY_DOWN:
            with connection_lock:
                if state.playlists:
                    state.selected_index = _clamp_index(state.selected_index + 1, len(state.playlists))
            continue
        if key in ENTER_KEY_CODES:
            playlist_to_refresh: str | None = None
            with connection_lock:
                if state.playlists:
                    selected_playlist = state.playlists[state.selected_index]
                    state.opened_playlist_id = selected_playlist.id
                    if (
                        state.connection_status == "connected"
                        and state.session is not None
                        and not selected_playlist.tracks_loaded
                    ):
                        playlist_to_refresh = selected_playlist.id
                        state.status_message = (
                            f"Loading tracks for playlist: {selected_playlist.name}"
                        )
            if playlist_to_refresh is not None:
                threading.Thread(
                    target=load_playlist_tracks_worker, args=(playlist_to_refresh,), daemon=True
                ).start()
            continue


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
