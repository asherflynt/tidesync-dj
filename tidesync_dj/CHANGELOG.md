# Changelog

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
