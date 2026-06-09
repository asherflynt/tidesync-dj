# TideSync DJ — Setup

An AI-powered DJ that controls your Music Assistant queue using Claude — works
with any music source you've added to Music Assistant (Tidal, Spotify, your local
library, etc.).

## Before you start

You need:

1. **Music Assistant** installed (as an HA add-on or elsewhere) with at least one
   **music provider** (Tidal, Spotify, local library, etc.) and one player.
2. A **Music Assistant login.** Music Assistant 2.8+ requires authentication on
   its WebSocket API. In Music Assistant, go to **Settings → Users** and create
   a user with a username and password (or reuse an existing one). You'll enter
   these in the add-on config below.
3. An **Anthropic API key** — create one at <https://console.anthropic.com>
   (it starts with `sk-ant-`). The key is stored privately by Home Assistant;
   it is never written to logs, the dashboard, or this repository.

## Quick start

1. Open the **Configuration** tab of this add-on.
2. Fill in the required fields:
   - **Anthropic API key** — paste your `sk-ant-...` key.
   - **Music Assistant host** — `homeassistant.local` if MA runs on this same
     machine, otherwise your HA server's IP. A full `http://…:8095` URL is also
     accepted (the scheme/port are ignored — the port comes from the next field).
   - **Music Assistant port** — usually `8095`.
   - **Music Assistant username / password** — the MA user from step 2 above.
3. (Optional) Pick a **Claude model**. Default is `claude-sonnet-4-6` — a good
   balance of quality and cost for frequent DJ decisions. Use
   `claude-opus-4-8` for the sharpest sequencing.
4. Click **Save**, then go to the **Info** tab and **Start** the add-on.
5. Open the **TideSync DJ** panel from the Home Assistant sidebar. The
   "Waiting for Music Assistant" banner clears once it connects and logs in;
   if login fails, the banner shows the exact reason.

The dashboard resizes to your screen: on a phone you get a bottom tab bar
(**Now Playing / Up Next / Search / Settings**); on a tablet or computer those
panels are shown side by side. The **Playback**, **Set Vibe**, **Listening**,
seed, and save controls all live under **Settings**.

- In **Settings → Playback**, pick a Music Assistant player (your AirPlay zone,
  etc.) and click **▶ Start Radio**. TideSync asks Claude for an opening set
  from your taste profile, vibe, and time of day, plays it on that player, then
  keeps the queue topped up automatically as it runs low.
- (Or, if you start playback yourself in Music Assistant, TideSync will take
  over topping up the queue once it runs low.)
- **Up Next** shows what's coming. Tap the **×** to remove a track, or drag the
  handle to reorder the queue.
- **Search** lets you find any song and queue it without breaking the flow:
  tap the **play-next** button to slot it right after the current track, or
  **+** to add it to the end of the queue.
- In **Settings → Set Vibe**, type a vibe ("late night focus", "high energy
  cooking") and click **Set Vibe** to steer the mood; **Nudge DJ** forces a
  decision immediately.
- Use the **Listening** switcher (Mom / Dad / Kids, plus **＋** to add people) to
  choose whose taste drives the DJ.
- Click **♥** (Like) on the Now Playing card to favorite the current track
  (synced through Music Assistant to whichever source the track came from), or
  **🚫** to block it for 30 days.
- Use **Save Session as a Playlist** to save everything you've heard this session
  to a new playlist (created on the source the session played).
- The **decision log** shows Claude's reasoning for each pick.

## Seed your taste from a YouTube Music playlist

In the **Seed Taste** card, paste the URL of a **public** YouTube Music playlist
and click **Seed**. TideSync reads the playlist's track and artist names and asks
Claude to build your taste profile from them.

- The playlist must be **public** — the fetch is unauthenticated.
- This only shapes *what the DJ picks*. Playback still happens through your
  **Music Assistant** providers; TideSync does not play YouTube Music itself.

## Optional: set the vibe from anywhere in HA

1. Create a helper: **Settings → Devices & Services → Helpers → Create Helper →
   Text**, named e.g. `tidesync_vibe`.
2. Put its entity id (e.g. `input_text.tidesync_vibe`) in the **Vibe helper
   entity** option.
3. Now setting that helper from any dashboard or the mobile app updates the DJ
   vibe.

## Troubleshooting

- **"Waiting for Music Assistant" never clears** — the banner shows the reason.
  Most often it's the MA login: Music Assistant 2.8+ requires authentication, so
  set **Music Assistant username/password** in the Configuration tab (create a
  user in MA under **Settings → Users**). "Invalid username or password" means
  the credentials are wrong; "requires a login" means they're blank.
- **"DJ decisions will fail" in the log** — the Anthropic API key is missing or
  blank. Add it in the Configuration tab.
- **No players in the dropdown** — Music Assistant isn't connected yet (see the
  login note above). Idle players are listed even when they show as unavailable;
  Start Radio will wake the one you pick.
- **Start Radio says "none could be found"** — Claude picked tracks but Music
  Assistant couldn't resolve them via its providers. Make sure at least one music
  provider is connected and working in MA.
- **Seeding fails** — the YouTube Music playlist must be **public**. Double-check
  the URL contains a `list=` parameter.
- **Like / Save playlist fails** — these need Music Assistant's favorites and
  playlist commands plus a source provider that allows library edits (not every
  provider does). Check the
  add-on **Log** tab for the exact error; the command names live in
  `app/ma_client.py` if your MA version differs.
- **Music Assistant command errors** — MA's WebSocket command names can vary by
  version; they're centralized in `app/ma_client.py` (`CMD_*` constants).
