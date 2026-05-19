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
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative"
SPOTIFY_MAX_RETRIES = 4
TOKEN_EXPIRY_BUFFER_SECONDS = 30
DEFAULT_TOKEN_EXPIRY_SECONDS = 3600
INITIAL_BACKOFF_SECONDS = 1.0
PKCE_VERIFIER_BYTES = 64
DEFAULT_PLAYLIST_NAME = "Unnamed Playlist"
UI_POLL_INTERVAL_MS = 100


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
            raise RuntimeError(f"Spotify API error {exc.code}: {error_message}") from exc
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


def _get_access_token(client_id: str, token_cache: dict[str, object]) -> str:
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
    if isinstance(access_token, str) and access_token and time.time() < expires_at:
        return access_token

    refresh_token = token_cache.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError("Access token expired and no refresh token is available.")
    refreshed = _refresh_access_token(client_id, refresh_token)
    token_cache.update(refreshed)
    return str(token_cache["access_token"])


def _fetch_user_playlists(client_id: str, token_cache: dict[str, object]) -> list[tuple[str, int]]:
    playlists: list[tuple[str, int]] = []
    next_url = f"{SPOTIFY_PLAYLISTS_URL}?limit=50"

    while next_url:
        access_token = _get_access_token(client_id, token_cache)
        payload = _spotify_request_json(
            next_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        for item in payload.get("items", []):
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name")
            name = raw_name if isinstance(raw_name, str) and raw_name else DEFAULT_PLAYLIST_NAME
            tracks = item.get("tracks", {})
            track_total = 0
            if isinstance(tracks, dict):
                raw_total = tracks.get("total", 0)
                if isinstance(raw_total, int):
                    track_total = raw_total
            playlists.append((name, track_total))

        raw_next = payload.get("next")
        next_url = raw_next if isinstance(raw_next, str) and raw_next else ""

    return playlists


def connect_and_get_playlists(
    status_callback: Callable[[str], None] | None = None,
) -> list[tuple[str, int]]:
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
    playlists = _fetch_user_playlists(client_id, tokens)
    return playlists


def run(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.timeout(UI_POLL_INTERVAL_MS)
    stdscr.keypad(True)

    connection_lock = threading.Lock()
    connection_status = "idle"
    status_message = "Press c to connect to Spotify with PKCE."
    error_message = ""
    playlists: list[tuple[str, int]] = []
    selected_index = 0

    def connect_worker() -> None:
        nonlocal connection_status, status_message, error_message, playlists, selected_index

        def update_status(message: str) -> None:
            nonlocal status_message
            with connection_lock:
                status_message = message

        try:
            fetched_playlists = connect_and_get_playlists(status_callback=update_status)
            with connection_lock:
                playlists = fetched_playlists
                selected_index = 0
                error_message = ""
                connection_status = "idle"
                if playlists:
                    status_message = f"Fetched {len(playlists)} playlist(s). Use ↑/↓ to navigate."
                else:
                    status_message = "Connected. No playlists found for this user."
        except (
            RuntimeError,
            TimeoutError,
            urllib.error.URLError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as exc:
            with connection_lock:
                connection_status = "idle"
                error_message = f"Connection failed: {exc}"
                status_message = "Press c to retry Spotify connection."

    while True:
        with connection_lock:
            status_snapshot = status_message
            error_snapshot = error_message
            playlists_snapshot = list(playlists)
            selected_snapshot = selected_index

        stdscr.erase()
        rows, cols = stdscr.getmaxyx()

        title = "Spotify Playlist Viewer TUI"
        help_text = "c: connect/reload  ↑/↓: move selection  q: quit"
        width = max(1, cols - 1)

        stdscr.addnstr(0, 0, title, width)
        stdscr.addnstr(1, 0, help_text, width)
        stdscr.hline(2, 0, "-", width)
        stdscr.addnstr(3, 0, status_snapshot, width)
        if error_snapshot:
            stdscr.addnstr(4, 0, error_snapshot, width)

        list_header_row = 5 if error_snapshot else 4
        stdscr.addnstr(list_header_row, 0, "Playlists:", width)

        if not playlists_snapshot:
            stdscr.addnstr(list_header_row + 1, 0, "No playlists loaded yet.", width)
        else:
            max_visible = max(1, rows - (list_header_row + 2))
            start_index = max(0, selected_snapshot - max_visible + 1)
            visible = playlists_snapshot[start_index : start_index + max_visible]
            for row_offset, (name, track_total) in enumerate(visible):
                playlist_index = start_index + row_offset
                line = f"{playlist_index + 1}. {name} ({track_total} tracks)"
                attr = curses.A_REVERSE if playlist_index == selected_snapshot else curses.A_NORMAL
                stdscr.addnstr(list_header_row + 1 + row_offset, 0, line, width, attr)

        stdscr.refresh()

        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), ord("Q")):
            break
        if key in (ord("c"), ord("C")):
            should_start_connection = False
            with connection_lock:
                if connection_status == "running":
                    status_message = "Connection already in progress..."
                else:
                    status_message = "Connecting to Spotify..."
                    error_message = ""
                    connection_status = "running"
                    should_start_connection = True
            if should_start_connection:
                threading.Thread(target=connect_worker, daemon=True).start()
            continue
        if key == curses.KEY_UP:
            with connection_lock:
                if playlists:
                    selected_index = max(0, selected_index - 1)
            continue
        if key == curses.KEY_DOWN:
            with connection_lock:
                if playlists:
                    selected_index = min(len(playlists) - 1, selected_index + 1)
            continue


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
