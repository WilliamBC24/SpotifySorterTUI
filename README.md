# SpotifySorterTUI
Terminal-based Spotify playlist organizer with keyboard-driven navigation.

## TUI Input Demo

This repository includes a TUI demo with Spotify PKCE connection and interactive playlist navigation.
It fetches and shows only playlists owned by the connected Spotify user.

### Run

```bash
export SPOTIFY_CLIENT_ID="your-spotify-app-client-id"
# Use https://... in production, or http://127.0.0.1:<port>/<path> for local development.
export SPOTIFY_REDIRECT_URI="http://127.0.0.1:8888/callback"
python3 tui_input_demo.py
```

### Controls

- `c`: connect to Spotify (only when disconnected)
- `↑` / `↓`: move selection in the currently focused pane
- `→`: open the highlighted playlist and move focus to the songs pane
- `←`: move focus back to the playlists pane
- `q`: quit the demo
