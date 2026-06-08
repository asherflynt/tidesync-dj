# Changelog

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
