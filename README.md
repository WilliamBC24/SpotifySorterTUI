# SpotifySorterTUI
Terminal-based Spotify playlist organizer with keyboard-driven navigation.

## TUI Input Demo

This repository includes a minimal TUI demo with Spotify PKCE connection and playlist listing.

### Run

```bash
export SPOTIFY_CLIENT_ID="your-spotify-app-client-id"
# Use https://... in production, or http://127.0.0.1:<port>/<path> for local development.
export SPOTIFY_REDIRECT_URI="http://127.0.0.1:8888/callback"
python3 tui_input_demo.py
```

### Controls

- Arrow keys: recorded as `UP`, `DOWN`, `LEFT`, `RIGHT`
- Enter: recorded as `ENTER`
- `c`: connect to Spotify (Authorization Code with PKCE) and print playlists with song counts
- `q`: quit the demo
