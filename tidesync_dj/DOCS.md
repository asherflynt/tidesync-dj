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

- Start playing something in Music Assistant. As the queue runs low, TideSync
  asks Claude for the next 2–3 tracks and appends them automatically.
- Type a **vibe** ("late night focus", "high energy cooking") in the panel and
  click **Set Vibe** to steer the mood.
- Click **Nudge DJ** to force a decision immediately.
- The **decision log** shows Claude's reasoning for each pick.

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
- **Music Assistant command errors** — MA's WebSocket command names can vary by
  version; they're centralized in `app/ma_client.py` (`CMD_*` constants).
