# Changelog

## 1.2.1

Internal hardening and cleanup (no change to how you use the add-on):

- **Play/pause** is now correct even on Music Assistant builds whose
  `player_queues/all` returns plain queue ids instead of full objects — it
  reads the player's state as a fallback so a press never restarts a playing
  track.
- Removed the unused `skip_penalty_seconds` option and other dead skip-penalty
  code left over from an earlier design.
- The connection's internal "is the Docker gateway reachable?" probe no longer
  briefly blocks the event loop on (re)connect.
- The dashboard pauses its background polling while its browser tab is hidden,
  and refreshes immediately when you return to it.
- Added an automated test suite and CI so regressions in transport, stats,
  resolution, and per-person memory get caught before release.

## 1.2.0

### Seamless speaker switching
- Switching the speaker mid-song now **transfers the queue in place** instead of
  restarting the track. The native Music Assistant transfer was being called with
  the wrong argument name and always fell back to clearing + re-queuing (which
  reset the song to 0:00); it now preserves the exact playback position.

### More reliable play/pause
- Play/pause is now driven through Music Assistant's **queue-level transport** and
  targets the queue that's actually playing (not just the selected player, which
  could differ for groups or when you switched players in MA).
- It now **resumes a queue MA had let go idle** after a long pause — previously a
  press did nothing because the player-level toggle can't resume from idle.

### Session stats that reflect reality
- A new listening session now also begins on **Set Vibe**, and a session
  **auto-ends after 2 hours** with no playback — so the numbers reflect your
  current sitting instead of accumulating for days.
- **"songs heard"** (formerly "tracks added") and **"artists"** now count what
  *actually played*, not what was queued. Tracks Music Assistant accepts but then
  can't stream (e.g. region-locked versions) no longer inflate the counts, and a
  saved session playlist contains only songs that were really heard.

### Track resolution
- Track search now returns the first **usable** candidate (one with a playable
  URI) and, all else equal, prefers your **configured provider** — instead of
  blindly taking the top hit. (Rare playback failures from stale Tidal catalog
  IDs are a provider-side issue; Music Assistant skips and the DJ refills.)

## 1.1.1

### Clearer energy controls
- The two energy-adjuster buttons beside **Set Vibe** now use a small flame
  (less energy) and a large flame (more energy) instead of two near-identical
  trend-line squiggles that gave no sense of direction.

## 1.0.0

First stable release. Highlights since 0.6:

- **Responsive Material dashboard** — phone tabs, tablet 2-column, desktop
  3-column; resizable left panel with album art that fits the viewport and an
  integrated Search.
- **Reliable transport** — atomic play/pause with instant (optimistic) UI, plus
  a new **Stop** button that halts playback, clears the queue, and parks the
  auto-DJ until you restart it.
- **Seed & Play** — a public YouTube Music playlist seeds the taste profile and
  starts a queue that mixes the playlist's own tracks among fresh discoveries.
- **No repeated tracks within a session.**
- Start-radio-from-a-seed-song, Up Next queue (remove / drag-reorder),
  per-person memory, and save-session-as-a-Tidal-playlist.

See the per-version notes below for the full detail.

## 0.8.6

### Stop button
- New **Stop** button (under Start Radio) halts playback, clears the queue, and
  parks the auto-DJ so it won't add any more songs. It stays parked until you
  explicitly restart — Start Radio, Set Vibe, Nudge, a person switch, or seeding
  all resume it. While stopped, Now Playing shows "Stopped".
- Backed by a new `/stop` endpoint and MA's `players/cmd/stop`; the parked state
  gates every auto-fill path (queue-low events, the poll loop, and manual ticks).

## 0.8.5

### No repeated tracks within a session
- Every decision now filters out tracks already heard or queued this session (the
  existing `session_uris` set is fed into the enqueue block list alongside the
  person's blocks), so the DJ never replays a song until the add-on restarts —
  even across Start Radio / Nudge / vibe changes.
- `enqueue_queries` also dedupes within a single batch, so two queries that
  resolve to the same track (e.g. a seeded playlist song Claude also picked) only
  queue once.

## 0.8.4

### Playlist songs actually play in the seeded queue
- Seeding now weaves a shuffled sample of the playlist's own tracks (up to 15,
  resolved to Tidal via Music Assistant search) **among** Claude's discovery
  picks in the opening queue — so you hear songs from the playlist you uploaded,
  not just music "in their style". A familiar track leads off the set, and the
  rest are spread evenly through the discoveries.

## 0.8.3

### Seed now starts a queue
- The **Seed Taste from YouTube Music** button (now "Seed & Play") seeds the
  taste profile from the playlist *and* immediately starts a fresh queue built
  from that just-updated taste — one action to learn the playlist and start
  music that reflects it. The taste seed still counts as successful if playback
  can't start (e.g. no MA player); the message reports playback separately.

## 0.8.2

### Reliable play/pause
- The transport button now toggles via MA's atomic `players/cmd/play_pause`
  instead of reading the state and sending the opposite command. The old path
  raced MA's eventually-consistent player state — a stale read could pause an
  already-paused player, so **resume appeared to do nothing**. The atomic toggle
  also resumes a paused queue more reliably than a bare play.
- The icon now flips **optimistically** on click for instant feedback, then
  reconciles after MA settles (the 5s status poll is the backstop), so it no
  longer takes seconds to switch from pause to play.

### Left-panel layout + heart icon
- On tablet/desktop the left column no longer overflows on short screens: the
  album art resizes to fit the viewport (square, capped at 440px), and Search is
  part of the same column with only its results list scrolling internally —
  rather than the whole column scrolling as one.
- Fixed the lopsided **like** heart: the SVG path is now symmetric (centered in
  its viewBox).

## 0.7.5

### Start radio from a seed song
- Each search result now has a third **Start radio** button alongside *Add to
  end* and *Play next*. It clears the queue, plays the hand-picked song
  immediately, and asks the Claude DJ brain to build a ~30-track station that
  flows from that seed (matching its genre, energy and era; never repeating the
  seed or queuing blocked tracks).
- Reuses the existing rebuild path (connection guard → player check → clear →
  re-baseline), so as the queue later drains the normal auto-DJ tick continues
  the station with no extra wiring.

## 0.7.4

### Touch-friendly UI
- Bigger tap targets (≥44px) for the queue remove button, **drag handle**, and
  the people switcher.
- Drag-to-reorder now **auto-scrolls** when you hold a track near the top/bottom
  edge, so a long queue is fully reorderable on a phone; an interrupted drag now
  resets cleanly.
- Press feedback on every control (taps no longer feel dead); the grey tap-flash
  is suppressed and Like/Block no longer "stick" tinted after a tap on touch.
- Search field uses a proper search keyboard (no autocorrect mangling titles,
  with a "Search" enter key).
- Scroll panels no longer rubber-band the whole page / trigger pull-to-refresh.

## 0.7.3

### Security hardening
- Pin third-party loggers (`anthropic`, `httpx`, `httpcore`, `urllib3`,
  `ytmusicapi`) to `WARNING` so their DEBUG request dumps can no longer write the
  Claude request payload — or auth headers — into the add-on log. The app's own
  modules still log at DEBUG.
- Constrain the `/queue/play_next` `option` field to `next`/`add`.

### Docs
- README + DOCS updated for the responsive UI, Search, and the Up Next queue
  (remove / reorder), and the API table now lists the search/queue endpoints.

## 0.7.2

- Fix: the pause button now correctly turns into a **play** button when
  playback is paused (and back again on resume).

## 0.7.1

- On tablet/desktop, **Search now lives in the left column** beneath the player
  (using the previously empty space); the right side holds Up Next + Settings.
  Mobile keeps Search as its own tab.

## 0.7.0

### Responsive Material UI
- The dashboard now resizes to the screen: a bottom tab bar on phones
  (Now Playing / Up Next / Search / Settings), a 2-column tablet layout, and a
  3-column desktop layout. Self-contained — no external fonts or icons.
- Album art is shown in full (square frame, no cropping).
- The people / **Listening** switcher moved into Settings.

### Up Next queue panel
- See what's coming up, **remove** a track, or **drag to reorder** the queue.

### Search & "play next"
- Search for a song and add it as **next up** — it plays right after the
  current track without interrupting it or the rest of the queue (or append it
  to the end of the queue).

## 0.6.0

### Per-person memory (Markdown)
- Each person gets a human-readable memory file at `/data/users/<name>.md`
  recording their **likes**, **30-day blocks**, learned **moods by time of
  day**, and a personal taste summary.
- Fresh installs are seeded with three people: **Mom**, **Dad**, **Kids**.
  Add more from the UI (＋). No accounts/auth — just whose preferences are live.
- Likes now log to the active person's memory in addition to favoriting in
  Tidal via MA.

### Listener toggle
- Name buttons in the left panel switch whose preferences drive the DJ.
  Switching a person **rebuilds the queue immediately** for them.

### Start Radio with no vibe
- If no vibe is set, the opening set is inferred from that person's
  mood-for-this-time-of-day history plus their recent likes.

### Block button (new)
- 🚫 **Block** removes the current song for **30 days** for the active person,
  **stops it immediately**, and prevents it from being re-queued. Auto-expires
  after 30 days — nothing is ever permanently blocked.

### Skip behavior — no more false skips
- Removed time-based skip detection entirely (clearing the queue / vibe
  rebuilds were being mis-counted as skips, permanently blacklisting artists).
- A skip is now recorded **only** when you press the TideSync ⏭ button, and it
  is a soft, session-only signal — never persisted, never blocks a track.

### Vibe change & Nudge DJ now actually do something
- **Set Vibe** clears the queue and rebuilds a fresh set, cutting to it now
  (previously it only changed appended tracks, so it appeared to "revert").
- **Nudge DJ** clears the queue and rebuilds with the live vibe (previously a
  no-op when the queue was already full).

### Likes fix
- The dashboard swallowed server errors (`postJSON` blindly parsed JSON), so a
  failed like only ever showed "Like failed." All POSTs now surface the real
  error, and a catch-all handler guarantees JSON responses instead of a
  plain-text 500.

## 0.5.0

### Queue depth & Claude call reduction
- Claude now picks **30 tracks** per decision (was 2–3); `max_tokens` raised to 4096
- Refill threshold raised to 5 remaining (was 2) — Claude is only called when:
  - **Start Radio** (initial 30-track set)
  - **Skip detected** (immediate re-evaluation with skip context)
  - **Nudge DJ** button
  - **Vibe changes** (via input_text entity or Set Vibe button)
  - **Queue low** (< 5 tracks remaining → refill with 30 more)

### Playback controls
- **Pause** (⏸) and **Skip** (⏭) buttons on the dashboard
- Skip routes through the engine so it's tracked and triggers a Claude re-evaluation

### Now Playing
- **Album art** displayed from Tidal/MA metadata
- Cleaner layout: art + title + controls side-by-side

### Engine
- `media_item_played` event now used as skip detection trigger
- Vibe change via input_text entity triggers an immediate tick
- `skip_penalty_seconds` default raised from 30 s → 60 s

## 0.4.7

- **Fix 30-second disconnect (hairpin NAT)**: when `ma_host` is a private LAN
  IP, the add-on now probes `172.30.32.1` (the HA OS Docker bridge gateway)
  first. MA uses host-networking so it listens there too, and this path avoids
  the Docker hairpin NAT that HA OS evicts after ~30 s.
- **Fix items_remaining calculation**: the `queue_updated` event carries
  `items` (total count) and `current_index` — compute remaining directly
  from these instead of making a round-trip `get_queue()` call that can hit
  a closing socket.
- **Add `media_item_played` event handling**: MA fires this when a new track
  starts; use it as the primary skip-detection trigger.
- **Raise skip_penalty_seconds default** from 30 s → 60 s so skips after a
  longer listen are still recorded.

## 0.4.6

- Suppress websockets library DEBUG noise so app-level logs are visible.
- Log every MA event type received (DEBUG) so we can confirm which event
  names MA actually sends and fix the queue_updated listener if needed.

## 0.4.5

- Fix auth deadlock introduced in 0.4.4: `_command` now gates on socket-open
  (`_is_open`) rather than fully-authenticated (`is_connected`), so the auth
  handshake can proceed without timing out.
- Enable DEBUG logging temporarily to capture raw `queue_updated` event
  payloads and skip-detection inputs for diagnosing skip detection.

## 0.4.4

- **Log real WS close codes** — disconnect log now shows the WebSocket close
  code and reason (e.g. 1006 / 1011) so the root cause of drops is identifiable.
- **Commands wait for reconnect** — if a command fires during a brief socket
  drop, it waits up to 5 s for the reconnect to complete before failing. Fixes
  Start Radio losing tracks mid-sequence when the socket drops between search and
  play_media calls.
- **Skip detection hardened** — if `current_item` is absent in a queue response
  (typical during a reconnect gap), skip detection no longer misreads the gap as
  a track change and incorrectly records a skip.
- `_authenticated` asyncio.Event added to `MusicAssistantClient`; cleared on
  disconnect, set on successful auth — used by the command wait mechanism above.

## 0.4.3

- Keep the Music Assistant connection alive: send a lightweight `info` command
  every 15s so the authenticated socket isn't reset after ~30s idle (the cause
  of the periodic "disconnect" flicker). Faster reconnect (2s).
- Don't flash a scary "can't reach host/port" message on a transient drop of an
  already-connected socket — only a real failure-to-open sets it.
- Dashboard: a clear "Connecting…" / "Connected" indicator in the header (amber
  pulsing dot while connecting, green when connected); the player dropdown shows
  "Connecting…" instead of "No players found" until the connection is up.

## 0.4.2

Fixes Start Radio / Nudge DJ / Seed all failing with
"AsyncMessages.create() got an unexpected keyword argument 'output_config'":

- Pass `output_config` / `thinking` via the SDK's `extra_body` instead of as
  typed keyword arguments, so requests work regardless of the installed
  anthropic SDK version. (Verified against anthropic 0.69.0.)
- Gate `effort` and adaptive `thinking` by model: they're only sent for Opus
  4.x and Sonnet 4.6. Haiku 4.5 (which rejects `effort`) now uses structured
  outputs only — fixing decisions for the default-cheap model choice.
- Seeding now reports a clear error if the taste analysis fails instead of a
  bare 500 / "Seed failed".

## 0.4.1

- Surface socket-level connection failures (wrong host/port, connection refused)
  in `last_error` so the dashboard banner shows the real reason instead of the
  generic "confirm host/port" text.

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
