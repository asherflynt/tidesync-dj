# TideSync DJ — Home Assistant Add-on Repository

A Home Assistant add-on repository containing **TideSync DJ**: a self-hosted,
DJ for your own music library in Music Assistant. It builds sets from tracks you
actually own and orders them by measured tempo, key and energy. Claude is optional.

## Add-ons in this repository

| Add-on | Description |
|--------|-------------|
| [TideSync DJ](./tidesync_dj) | A DJ for your Music Assistant library, sequenced by measured tempo, key and energy |

## Installation

1. In Home Assistant, go to **Settings → Apps** and click **Install app**
   (bottom-right) to open the App store.
2. Click the **⋮** menu (top-right) → **Repositories**.
3. Add this repository URL, then close the dialog:
   ```
   https://github.com/asherflynt/tidesync-dj
   ```
4. Back in the App store, find the **TideSync DJ** app, open it, and click
   **Install**. Then configure it (Music Assistant host and token) and start it.
5. Open the **TideSync DJ** panel from the HA sidebar.

See the [add-on README](./tidesync_dj/README.md) for full configuration and API
details.

## Credits

Tempo, key and sound analysis come from [Music Assistant](https://music-assistant.io)'s
on-device audio analysis (Smart Fades, Sonic Analysis and Sonic Similarity).
