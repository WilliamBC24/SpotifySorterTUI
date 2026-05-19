#!/usr/bin/env python3
"""Simple TUI with Spotify PKCE login and playlist listing."""

from __future__ import annotations

import curses
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer


KEY_LABELS = {
    curses.KEY_UP: "UP",
    curses.KEY_DOWN: "DOWN",
    curses.KEY_LEFT: "LEFT",
    curses.KEY_RIGHT: "RIGHT",
    curses.KEY_ENTER: "ENTER",
    10: "ENTER",
    13: "ENTER",
}


SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_PLAYLISTS_URL = "https://api.spotify.com/v1/me/playlists"
SPOTIFY_SCOPE = "playlist-read-private playlist-read-collaborative"
SPOTIFY_MAX_RETRIES = 4
TOKEN_EXPIRY_BUFFER_SECONDS = 30


def _base64_url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _build_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(96)[:128]
    challenge_raw = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = _base64_url_encode(challenge_raw)
    return code_verifier, code_challenge


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
        def do_GET(self) -> None:  # noqa: N802
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

    backoff_seconds = 1.0
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
    expires_in = _safe_int(payload.get("expires_in", 3600), 3600)
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
    expires_in = _safe_int(payload.get("expires_in", 3600), 3600)
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
            name = raw_name if isinstance(raw_name, str) and raw_name else "Unnamed Playlist"
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


def connect_and_get_playlist_lines() -> list[str]:
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

    webbrowser.open(auth_url)
    code = _wait_for_auth_code(redirect_uri, state)
    tokens = _exchange_code_for_token(client_id, redirect_uri, code, code_verifier)
    playlists = _fetch_user_playlists(client_id, tokens)

    lines = ["Playlist data provided by Spotify.", f"Fetched {len(playlists)} playlist(s):"]
    if not playlists:
        lines.append("No playlists found for this user.")
        return lines

    for name, track_total in playlists:
        lines.append(f"- {name} ({track_total} tracks)")
    return lines


def run(stdscr: curses.window) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.keypad(True)

    history: list[str] = ["Press c to connect to Spotify with PKCE."]

    while True:
        stdscr.erase()
        rows, cols = stdscr.getmaxyx()

        title = "Spotify Sorter TUI - Spotify Connection"
        help_text = "Press c to connect. Press q to quit."
        width = max(1, cols - 1)

        stdscr.addnstr(0, 0, title, width)
        stdscr.addnstr(1, 0, help_text, width)
        stdscr.hline(2, 0, "-", width)
        stdscr.addnstr(3, 0, "Captured input:", width)

        available_lines = max(1, rows - 5)
        visible_history = history[-available_lines:]
        start_number = len(history) - len(visible_history) + 1
        for row_offset, item in enumerate(visible_history):
            event_number = start_number + row_offset
            stdscr.addnstr(4 + row_offset, 0, f"{event_number}. {item}", width)

        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            break
        if key in (ord("c"), ord("C")):
            history.append("Connecting to Spotify...")
            try:
                history.extend(connect_and_get_playlist_lines())
            except (
                RuntimeError,
                TimeoutError,
                urllib.error.URLError,
                json.JSONDecodeError,
                UnicodeDecodeError,
            ) as exc:
                history.append(f"Connection failed: {exc}")
            continue

        label = KEY_LABELS.get(key, f"KEYCODE {key}")
        history.append(label)


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
