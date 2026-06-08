# TideSync DJ — Setup

An AI-powered Tidal DJ that controls your Music Assistant queue using Claude.

## Before you start

You need:

1. **Music Assistant** installed (as an HA add-on or elsewhere) with **Tidal**
   added as a provider and at least one player.
2. An **Anthropic API key** — create one at <https://console.anthropic.com>
   (it starts with `sk-ant-`). The key is stored privately by Home Assistant;
   it is never written to logs, the dashboard, or this repository.

## Quick start

1. Open the **Configuration** tab of this add-on.
2. Fill in the required fields:
   - **Anthropic API key** — paste your `sk-ant-...` key.
   - **Music Assistant host** — `homeassistant.local` if MA runs on this same
     machine, otherwise your HA server's IP.
   - **Music Assistant port** — usually `8095`.
3. (Optional) Pick a **Claude model**. Default is `claude-sonnet-4-6` — a good
   balance of quality and cost for frequent DJ decisions. Use
   `claude-opus-4-8` for the sharpest sequencing.
4. Click **Save**, then go to the **Info** tab and **Start** the add-on.
5. Open the **TideSync DJ** panel from the Home Assistant sidebar.

## Using it

- In the **Playback** card, pick a Music Assistant player (your AirPlay zone,
  etc.) and click **▶ Start Radio**. TideSync asks Claude for an opening set
  from your taste profile, vibe, and time of day, plays it on that player, then
  keeps the queue topped up automatically as it runs low.
- (Or, if you start playback yourself in Music Assistant, TideSync will take
  over topping up the queue once it runs low.)
- Type a **vibe** ("late night focus", "high energy cooking") in the panel and
  click **Set Vibe** to steer the mood.
- Click **Nudge DJ** to force a decision immediately.
- Click **♥ Like in Tidal** on the Now Playing card to favorite the current
  track (synced to Tidal through Music Assistant).
- Use **Save Session as a Tidal Playlist** to save everything you've heard this
  session to a new Tidal playlist.
- The **decision log** shows Claude's reasoning for each pick.

## Seed your taste from a YouTube Music playlist

In the **Seed Taste** card, paste the URL of a **public** YouTube Music playlist
and click **Seed**. TideSync reads the playlist's track and artist names and asks
Claude to build your taste profile from them.

- The playlist must be **public** — the fetch is unauthenticated.
- This only shapes *what the DJ picks*. Playback still happens through **Tidal in
  Music Assistant**; TideSync does not play YouTube Music itself.

## Optional: set the vibe from anywhere in HA

1. Create a helper: **Settings → Devices & Services → Helpers → Create Helper →
   Text**, named e.g. `tidesync_vibe`.
2. Put its entity id (e.g. `input_text.tidesync_vibe`) in the **Vibe helper
   entity** option.
3. Now setting that helper from any dashboard or the mobile app updates the DJ
   vibe.

## Troubleshooting

- **Panel loads but nothing plays** — check the add-on **Log** tab. The most
  common issue is the Music Assistant host/port or no active player/queue.
- **"DJ decisions will fail" in the log** — the Anthropic API key is missing or
  blank. Add it in the Configuration tab.
- **No players in the dropdown** — Music Assistant isn't reachable yet, or it has
  no available players. Confirm the host/port and that a player is set up in MA.
- **Start Radio says "none could be found"** — Claude picked tracks but Music
  Assistant couldn't resolve them via its providers. Make sure Tidal (or another
  provider) is connected and working in MA.
- **Seeding fails** — the YouTube Music playlist must be **public**. Double-check
  the URL contains a `list=` parameter.
- **Like / Save playlist fails** — these need Music Assistant's favorites and
  playlist commands plus a Tidal provider that allows library edits. Check the
  add-on **Log** tab for the exact error; the command names live in
  `app/ma_client.py` if your MA version differs.
- **Music Assistant command errors** — MA's WebSocket command names can vary by
  version; they're centralized in `app/ma_client.py` (`CMD_*` constants).
