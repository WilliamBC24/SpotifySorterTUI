#!/usr/bin/env python3
"""Simple TUI with Spotify PKCE login and playlist listing."""

from __future__ import annotations

import curses
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


def _base64_url_encode(data: bytes) -> str:
    return urllib.parse.quote_from_bytes(
        __import__("base64").urlsafe_b64encode(data).rstrip(b"=")
    )


def _build_pkce_pair() -> tuple[str, str]:
    code_verifier = secrets.token_urlsafe(64)[:128]
    challenge_raw = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = _base64_url_encode(challenge_raw)
    return code_verifier, code_challenge


def _wait_for_auth_code(redirect_uri: str, expected_state: str, timeout_seconds: int = 180) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
        raise RuntimeError("SPOTIFY_REDIRECT_URI must be an http:// URL with explicit host and port.")

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

        def log_message(self, format: str, *args: object) -> None:
            return

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


def _exchange_code_for_token(client_id: str, redirect_uri: str, code: str, code_verifier: str) -> str:
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        SPOTIFY_TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError("No access token received from Spotify.")
    return access_token


def _fetch_user_playlists(access_token: str) -> list[tuple[str, int]]:
    playlists: list[tuple[str, int]] = []
    next_url = f"{SPOTIFY_PLAYLISTS_URL}?limit=50"
    headers = {"Authorization": f"Bearer {access_token}"}

    while next_url:
        request = urllib.request.Request(next_url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for item in payload.get("items", []):
            name = item.get("name", "Unnamed Playlist")
            track_total = item.get("tracks", {}).get("total", 0)
            playlists.append((name, track_total))

        next_url = payload.get("next")

    return playlists


def connect_and_get_playlist_lines() -> list[str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip()
    if not client_id:
        raise RuntimeError("Set SPOTIFY_CLIENT_ID before connecting to Spotify.")

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
    access_token = _exchange_code_for_token(client_id, redirect_uri, code, code_verifier)
    playlists = _fetch_user_playlists(access_token)

    lines = [f"Fetched {len(playlists)} playlist(s):"]
    if not playlists:
        lines.append("No playlists found for this user.")
        return lines

    for name, track_total in playlists:
        lines.append(f"- {name} ({track_total} songs)")
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
            except (RuntimeError, TimeoutError, urllib.error.URLError) as exc:
                history.append(f"Connection failed: {exc}")
            continue

        label = KEY_LABELS.get(key, f"KEYCODE {key}")
        history.append(label)


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
