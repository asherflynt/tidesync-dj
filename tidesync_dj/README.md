# TideSync DJ

> Self-hosted, AI-powered Tidal DJ that runs as a **Home Assistant add-on**,
> drives playback through **Music Assistant**, and uses **Claude** as the DJ
> decision brain.

TideSync watches your Music Assistant queue and listening history, builds a
real-time mood context, and asks Claude what to play next — sequencing tracks
for energy flow rather than dumping a static playlist. It responds to a "vibe
prompt", honours skips, varies discovery vs. familiarity, and adapts to the
time of day.

## How it works

```
[HA Supervisor] → /data/options.json → configures the add-on
[FastAPI app]   → WebSocket → [Music Assistant]  (queue control, Tidal provider)
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
| `claude_model` | `claude-opus-4-8` (best), `claude-sonnet-4-6` (cheaper for frequent ticks), or `claude-haiku-4-5` | `claude-opus-4-8` |
| `ma_host` | Music Assistant hostname | `homeassistant.local` |
| `ma_port` | MA WebSocket port | `8095` |
| `dj_tick_interval` | Polling fallback interval (seconds) | `30` |
| `skip_penalty_seconds` | Track-change-within window counts as a skip | `30` |
| `vibe_input_text_entity` | Optional `input_text.*` helper to poll for the vibe | — |

Secrets are managed by Home Assistant — no `.env` file. Options are read from
`/data/options.json`; persistent state (taste profile, skip history) lives under
`/data` and survives restarts.

## API (served under ingress)

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | Ingress dashboard |
| `GET`  | `/status` | DJ state, now playing, active vibe, stats |
| `POST` | `/vibe` | Set vibe: `{"prompt": "late night focus"}` |
| `GET`  | `/queue` | Current MA queue |
| `POST` | `/tick` | Manually trigger a decision cycle |
| `GET`  | `/history` | Recent DJ decisions |

## Home Assistant integration

- Fires a `tidesync_dj_decision` event on every decision (use it in automations).
- Optionally polls an `input_text.tidesync_vibe` helper so the vibe can be set
  from any HA dashboard or phone.
- Uses the Supervisor token (no user-provided HA token needed).

## Notes & caveats

- The Music Assistant command names in `app/ma_client.py` follow MA's documented
  WebSocket schema. If your MA version differs, adjust the `CMD_*` constants.
- The model is configurable: Opus 4.8 gives the best sequencing; Sonnet 4.6 is a
  good cost/latency choice if the DJ ticks frequently.

## Reference

- [Music Assistant WebSocket API](https://music-assistant.io/integration/websocket/)
- [HA Add-on Development](https://developers.home-assistant.io/docs/add-ons/)
- [HA Supervisor API](https://developers.home-assistant.io/docs/supervisor/development/)
- [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python)
