# TideSync DJ

> Self-hosted, AI-powered DJ that runs as a **Home Assistant add-on**, drives
> playback through **Music Assistant** (any configured music source — Tidal,
> Spotify, local library, etc.), and uses **Claude** as the DJ decision brain.

TideSync watches your Music Assistant queue and listening history, builds a
real-time mood context, and asks Claude what to play next — sequencing tracks
for energy flow rather than dumping a static playlist. It responds to a "vibe
prompt", honours skips, varies discovery vs. familiarity, and adapts to the
time of day.

## How it works

```
[HA Supervisor] → /data/options.json → configures the add-on
[FastAPI app]   → WebSocket → [Music Assistant]  (queue control, your music providers)
                → REST API  → [Anthropic Claude]  (next-track decisions)
                → REST API  → [HA Supervisor API] (presence, events, vibe helper)
[Ingress UI]    → embedded panel in the HA sidebar
```

Each decision cycle hands Claude a structured context payload (taste profile,
recent history, current track, queue, recent skips, vibe, time of day, session
length). Claude returns 2–3 next tracks with reasoning, an optional mood-shift
flag, and a DJ note. The engine resolves those queries against Music Assistant
and appends them to the active queue.

## Installation (local add-on)

1. Copy the `tidesync-dj/` folder into your Home Assistant `addons/` share
   (e.g. `/addons/tidesync-dj`).
2. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** isn't needed for a
   local add-on — instead just **⋮ → Check for updates**, then find
   *TideSync DJ* under *Local add-ons* and install.
3. Open the add-on **Configuration** tab and set options (below).
4. Start the add-on and open the **TideSync DJ** panel from the sidebar.

## Configuration

| Option | Description | Default |
|--------|-------------|---------|
| `anthropic_api_key` | Anthropic API key | — |
| `claude_model` | `claude-sonnet-4-6` (default — best cost/quality balance), `claude-opus-4-8` (sharpest sequencing), or `claude-haiku-4-5` (cheapest) | `claude-sonnet-4-6` |
| `ma_host` | Music Assistant hostname/IP (a full `http://…:port` URL is also accepted and normalized) | `homeassistant.local` |
| `ma_port` | MA WebSocket port | `8095` |
| `ma_username` | Music Assistant username (MA 2.8+ requires a login) | — |
| `ma_password` | Music Assistant password | — |
| `ma_token` | Optional MA API token (used instead of username/password) | — |
| `dj_tick_interval` | Polling fallback interval (seconds) | `30` |
| `skip_penalty_seconds` | Track-change-within window counts as a skip | `30` |
| `vibe_input_text_entity` | Optional `input_text.*` helper to poll for the vibe | — |
| `playlist_provider` | MA provider for saved playlists; blank = auto-detect from what played | _(auto)_ |

> **Music Assistant 2.8+ requires authentication.** Create a user in Music
> Assistant (**Settings → Users**) and set `ma_username`/`ma_password`. Without
> it, the add-on connects to the socket but every command is rejected with
> "Authentication required" — the dashboard banner will say so.

Secrets are managed by Home Assistant — no `.env` file. Options are read from
`/data/options.json`; persistent state (taste profile, skip history) lives under
`/data` and survives restarts.

## Security & privacy

- Your **Anthropic API key** (and Music Assistant credentials) are stored only
  in Home Assistant's add-on options (`/data/options.json`, password-masked in
  the UI). They are **never** logged, returned by any API endpoint, shown in the
  dashboard, or committed to this repository. Third-party HTTP/SDK loggers
  (`anthropic`, `httpx`, `httpcore`) are pinned to `WARNING` so their DEBUG
  request dumps can't leak payloads or headers into the add-on log.
- The web UI is served **only through authenticated Home Assistant ingress** —
  there is no `ports:` mapping, so the service is not exposed on the host network.
- The add-on requests **least privilege**: only `homeassistant_api` (Core REST
  API for reading states and firing the `tidesync_dj_decision` event). It does
  not request Supervisor (`hassio_api`), host, or device access.
- All control endpoints take simple typed parameters and are forwarded to Music
  Assistant; there is no shell, SQL, or filesystem path built from request input.
  Listener names are slugified to `[a-z0-9-]` before becoming filenames, and the
  YouTube seed accepts only a playlist **id** (no arbitrary URL is fetched).
- Dynamic values rendered in the dashboard are HTML-escaped to prevent injection
  from track/artist names or Claude's notes.
- Runtime data files (`options.json`, `taste_profile.json`, `data/`) are
  `.gitignore`d so they can't be accidentally committed.

## Dashboard

The dashboard is a single self-contained page (no external fonts, icons, or
CDNs) that **adapts to screen size** so it's usable on a phone, a tablet, or a
wall display:

- **Phone** — a bottom tab bar: **Now Playing / Up Next / Search / Settings**.
- **Tablet / desktop** — a multi-column layout (Now Playing + Search on the
  left, Up Next and Settings alongside) with no tab switching.

Highlights:

- **Now Playing** — full, uncropped album art, transport controls, like/block.
- **Up Next** — the live queue with **remove** and **drag-to-reorder** (works
  with both mouse and touch).
- **Search** — find any track in Music Assistant and add it as **next up**
  (plays right after the current track without interrupting it) or append it to
  the end of the queue.
- **Settings** — player selection, Start Radio, vibe/nudge, the **Listening**
  (per-person) switcher, session stats, taste seeding, save-as-playlist, and the
  DJ decision log.

The goal is to run the whole session here without opening the Music Assistant
UI (except to build your own playlists).

## API (served under ingress)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | Ingress dashboard |
| `GET`  | `/status` | DJ state, now playing, active vibe, stats |
| `POST` | `/vibe` | Set vibe: `{"prompt": "late night focus"}` |
| `GET`  | `/queue` | Upcoming queue items (name, artist, art, `item_id`) |
| `GET`  | `/search?q=` | Search Music Assistant for tracks to add |
| `POST` | `/queue/play_next` | Insert a track after the current one: `{"uri": "...", "option": "next"\|"add"}` |
| `POST` | `/queue/remove` | Remove an upcoming item: `{"item_id": "..."}` |
| `POST` | `/queue/move` | Reorder an item: `{"item_id": "...", "to_index": N}` |
| `GET`  | `/players` | Available Music Assistant players |
| `POST` | `/players/select` | Choose a player: `{"player_id": "..."}` |
| `POST` | `/start_radio` | Start playback with a fresh AI-picked set |
| `POST` | `/playpause` | Toggle play/pause on the active player |
| `POST` | `/seed` | Seed taste from a YouTube Music playlist: `{"playlist": "<url>"}` |
| `POST` | `/like` | Like the current track (synced to its source provider) |
| `POST` | `/block` | Block the current track for 30 days |
| `POST` | `/nudge` | Force a fresh decision immediately |
| `POST` | `/save_playlist` | Save the session's tracks as a playlist: `{"name": "..."}` |
| `POST` | `/tick` | Manually trigger a decision cycle |
| `GET`  | `/history` | Recent DJ decisions |
| `GET`/`POST` | `/users`, `/users/select`, `/users/add` | List/switch/add listeners |

### Like tracks & save the session as a playlist

- **♥ Like** (on the Now Playing card) favorites the current track. This goes
  through Music Assistant's favorites, which syncs to whichever provider owns the
  track (the URI's source — Tidal, Spotify, etc.).
- **Save Session as a Playlist** creates a new playlist from every track heard
  this session. By default the target provider is auto-detected from the tracks
  that played; set the `playlist_provider` option to force a specific provider
  (useful for sessions that mixed multiple sources).

> These rely on Music Assistant's favorites/playlist commands and the source
> provider supporting library edits. If your MA version names these commands
> differently, they're centralized as `CMD_FAVORITE_ADD`,
> `CMD_PLAYLIST_CREATE`, and `CMD_PLAYLIST_ADD_TRACKS` in `app/ma_client.py`.

### Players & Start Radio

The dashboard lists the players Music Assistant knows about (your AirPlay zones,
etc.). Pick one and hit **Start Radio** — TideSync asks Claude for an opening
set based on your taste profile, vibe, and time of day, plays it on the selected
player, and then keeps the queue topped up automatically as it runs low.

### Seeding taste from YouTube Music

You can bootstrap your taste profile from a **public** YouTube Music playlist:
paste its URL in the dashboard and click **Seed**. TideSync reads the playlist's
track/artist names (unauthenticated, via `ytmusicapi`) and asks Claude to build
your taste summary from them.

> This only *shapes what the DJ picks* — playback still happens through your
> **Music Assistant** providers. TideSync does not play YouTube Music itself, and
> private playlists are not supported (the fetch is unauthenticated).

## Home Assistant integration

- Fires a `tidesync_dj_decision` event on every decision (use it in automations).
- Optionally polls an `input_text.tidesync_vibe` helper so the vibe can be set
  from any HA dashboard or phone.
- Uses the Supervisor token (no user-provided HA token needed).

## Notes & caveats

- The Music Assistant command names in `app/ma_client.py` follow MA's documented
  WebSocket schema. If your MA version differs, adjust the `CMD_*` constants.
- The model is configurable: Sonnet 4.6 (the default) is the best cost/latency
  choice for a DJ that ticks frequently; Opus 4.8 gives the sharpest sequencing.

## Reference

- [Music Assistant WebSocket API](https://music-assistant.io/integration/websocket/)
- [HA Add-on Development](https://developers.home-assistant.io/docs/add-ons/)
- [HA Supervisor API](https://developers.home-assistant.io/docs/supervisor/development/)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)
