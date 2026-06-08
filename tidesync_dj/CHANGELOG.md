# Changelog

## 0.4.0

The add-on connected to the Music Assistant socket but every command was
silently rejected, because **Music Assistant 2.8+ requires authentication** on
its WebSocket API. Fixes (validated live against MA 2.8.9):

- Authenticate after the handshake: `ma_username`/`ma_password` (builtin login)
  or an optional `ma_token`. Clear errors surface to the dashboard banner.
- `ma_connected` now reflects the authenticated socket, not whether a queue is
  active — so the UI no longer appears broken until something plays.
- List idle players too: MA reports players as `available: false` until woken,
  and they were being filtered out ("No players found"). Start Radio wakes them.
- Normalize the host field: a pasted `http://host:port` URL is reduced to the
  bare host (the port comes from `ma_port`).
- All Music Assistant command names were validated against the live server —
  every one is correct.

## 0.3.1

Build fixes (the 0.3.0 image failed to build on Home Assistant):

- Use a valid base image tag: `…/{arch}-base-python:3.12-alpine3.21` (the bare
  `:3.12` tag does not exist).
- Drop the unused `music-assistant-client` dependency — the MA client uses raw
  `websockets` — removing a large, compile-prone dependency tree.
- Use plain `uvicorn` instead of `uvicorn[standard]` to avoid musl-compiling
  `uvloop`/`httptools`/`watchfiles`.
- Add a temporary `build-base`/`libffi-dev` layer as a fallback for any C
  extension, removed after install.

## 0.3.0

- **Like in Tidal**: favorite the current track from the dashboard (`/like`),
  via Music Assistant's favorites (syncs to the Tidal provider).
- **Save session as a Tidal playlist**: create a Tidal playlist from every track
  heard this session (`/save_playlist`). New `tidal_provider` option (default
  `tidal`) selects the MA provider used for likes/playlists.
- The engine now tracks the ordered set of session track URIs for playlist save.

## 0.2.0

- **Player selection**: list Music Assistant players and choose which one the DJ
  targets (`/players`, `/players/select`, dashboard dropdown).
- **Start Radio**: one-click start — Claude picks an opening set from your taste
  profile + vibe + time of day, plays it on the selected player, then keeps the
  queue topped up (`/start_radio`).
- **Seed taste from YouTube Music**: bootstrap the taste profile from a public
  YouTube Music playlist via `ytmusicapi` (`/seed`). Playback still runs through
  Tidal in Music Assistant — the playlist only shapes what the DJ picks.
- Default model changed to `claude-sonnet-4-6`; dropped the `hassio_api`
  privilege; added config-form translations, DOCS.md, and onboarding banners.

## 0.1.0

Initial release.

- Home Assistant add-on scaffold (S6-overlay v3, ingress UI panel).
- Music Assistant WebSocket client (queue, history, search, enqueue, skip).
- Claude DJ brain using the Anthropic SDK with structured outputs, adaptive
  thinking, and prompt caching on the system + taste-profile prefix.
- Background DJ engine: queue-low + polling triggers, skip detection,
  time-of-day awareness, decision log, session stats.
- Persistent taste profile stored under `/data`, bootstrapped from the MA
  library and refreshed every 20 decisions.
- HA Supervisor integration: reads options from `/data/options.json`, fires
  `tidesync_dj_decision` events, optional `input_text` vibe polling.
- Dark-themed ingress dashboard.
