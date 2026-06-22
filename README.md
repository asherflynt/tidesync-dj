# TideSync DJ — Home Assistant Add-on Repository

A Home Assistant add-on repository containing **TideSync DJ**: a self-hosted,
AI-powered DJ that drives playback through Music Assistant (any music source you
have configured — Tidal, Spotify, your local library, etc.) and uses Claude as
the decision brain.

## Add-ons in this repository

| Add-on | Description |
|--------|-------------|
| [TideSync DJ](./tidesync_dj) | AI-powered DJ for any Music Assistant source, powered by Claude |

## Installation

1. In Home Assistant, go to **Settings → Apps** and click **Install app**
   (bottom-right) to open the App store.
2. Click the **⋮** menu (top-right) → **Repositories**.
3. Add this repository URL, then close the dialog:
   ```
   https://github.com/asherflynt/tidesync-dj
   ```
4. Back in the App store, find the **TideSync DJ** app, open it, and click
   **Install**. Then configure it (Anthropic API key, Music Assistant
   host/port) and start it.
5. Open the **TideSync DJ** panel from the HA sidebar.

See the [add-on README](./tidesync_dj/README.md) for full configuration and API
details.

## Credits

TideSync grounds its track sequencing in tempo/key data from free, openly
licensed sources, looked up by ISRC (no audio is ever downloaded or analysed):

- BPM and key data provided by [GetSongBPM](https://getsongbpm.com).
- Acoustic features from [AcousticBrainz](https://acousticbrainz.org), with
  recordings resolved via [MusicBrainz](https://musicbrainz.org).
