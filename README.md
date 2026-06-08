# TideSync DJ — Home Assistant Add-on Repository

A Home Assistant add-on repository containing **TideSync DJ**: a self-hosted,
AI-powered Tidal DJ that drives playback through Music Assistant and uses Claude
as the decision brain.

## Add-ons in this repository

| Add-on | Description |
|--------|-------------|
| [TideSync DJ](./tidesync_dj) | AI-powered Tidal DJ using Music Assistant and Claude |

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** menu (top-right) → **Repositories**.
3. Add this repository URL:
   ```
   https://github.com/asherflynt/tidesync-dj
   ```
4. The **TideSync DJ** add-on now appears in the store — install it, configure
   it (Anthropic API key, Music Assistant host/port), and start it.
5. Open the **TideSync DJ** panel from the HA sidebar.

See the [add-on README](./tidesync_dj/README.md) for full configuration and API
details.
