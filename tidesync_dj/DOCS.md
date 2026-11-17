# TideSync DJ

A DJ for your own music library in Music Assistant. You give it a vibe (or a
starting song); it builds a set from tracks you own and orders them by measured
tempo, key and energy. There is no voice-over and nothing is invented: every
pick is a real track in your library.

## How it works

1. **Library mirror.** TideSync keeps a local copy of your Music Assistant track
   list, with tempo, key and an energy value for every analysed track.
2. **Finding candidates.** A vibe is matched against how your tracks *sound*,
   using Music Assistant's sonic search. That search only knows the tracks in
   Music Assistant's sound index, which can lag far behind its analysis, so
   tracks that resemble the best matches, and analysed tracks with a fitting
   energy, are added too. A starting song or your recent likes pull in tracks
   that sound similar. A few tracks you have not heard in a while are mixed in
   for variety.
3. **Sequencing.** Plain code orders the candidates to follow an energy curve
   and keeps each transition close in tempo and key, with artists spaced out.
4. **Rolling queue.** It keeps about 8 tracks queued and re-plans on each
   top-up, so skips, likes and energy changes take effect within a song or two.

Only analysed tracks are played. A track counts as analysed as soon as Music
Assistant has measured its tempo or key, whether or not the sound index has it
yet. While the library is being analysed, a "Getting your library ready" card on
Now Playing shows how many songs are ready. With an admin token it also says
roughly how long the rest will take, once it has watched Music Assistant's
nightly analysis for about a day. The "What the DJ is doing" view also shows how
many songs a vibe can match by sound.

## Using the dashboard

On a phone there are three tabs:

- **Now Playing**: **Start the music** and the speaker it plays in; the current
  song with Like, Previous, Play/Pause, Skip and Block; one-tap vibes (Focus,
  Workout, Dinner, Wind down, Party, Sleep) or your own words; **Calmer** and
  **Hotter**; **Try something different**; and who is listening.
- **Up Next**: what is queued, with search at the top. Tap a search result to
  play it next; its menu (the dots) adds it to the end or starts a new mix from
  it. Drag the handle to reorder, or remove a song.
- **Settings**: the speaker, session stats, **Why these songs**, and Advanced
  (save the session as a playlist, what the DJ knows about each listener, and
  how vibes are read).

Tablets show Now Playing beside Up Next and Settings; laptops show all three
side by side. While the DJ is choosing songs, a banner on Now Playing shows its
progress; **Details** or **What's the DJ doing?** opens the full view.

## Requirements in Music Assistant

- Music Assistant 2.10 or newer.
- Your music added through a **filesystem provider** (local, SMB or NFS).
  Music Assistant only analyses files from those providers in the background.
- These providers enabled: **Smart Fades**, **Sonic Analysis** and
  **Sonic Similarity**, with Sonic Similarity's Character index and free-text
  search turned on.

Analysis is slow: expect a few tracks a minute on a small machine. Music
Assistant runs it nightly; the **Keep analysis running** option restarts the
scan whenever it stops with tracks left.

## Configuration

| Option | Meaning |
|---|---|
| `ma_host`, `ma_port` | Where Music Assistant is reachable. |
| `ma_token` | Long-lived Music Assistant token (admin user for analysis progress and "Keep analysis running"). |
| `ma_username`, `ma_password` | Alternative to a token. |
| `anthropic_api_key` | Optional. Lets Claude interpret the vibe. |
| `claude_model` | Model used when a key is set. |
| `dj_tick_interval` | Seconds between queue checks. |
| `vibe_input_text_entity` | Optional `input_text` helper; a change sets the vibe. |
| `ha_action_entity` | Optional helper that drives the DJ from automations. |
| `keep_analysis_running` | Restart Music Assistant's analysis scan until the library is done. |

### Driving it from Home Assistant

Write one of these to the action helper: `play`, `stop`, `skip`, `previous`,
`nudge`, `energy up`, `energy down`, `vibe: rainy sunday`, `player: Kitchen`.

## Claude is optional

Without an API key the vibe text goes straight to the sound search and keyword
rules set the energy ("chill", "workout", "wind down" and so on). With a key,
one small Claude request per vibe turns it into two to four descriptions of
sound and an energy shape. Claude never chooses tracks.

## Privacy

- With no API key, nothing leaves your network.
- With a key, each vibe change sends: the vibe text, the time of day, the active
  listener's name (a label you choose) and their recent vibe phrases. No track
  list, file path or play history is sent.
- Likes, blocks and the library mirror are stored under the add-on's `/data`.

## People, likes and blocks

Each listener has their own likes and blocks. A like also marks the track as a
favourite in Music Assistant. A block skips the track and keeps it out for three
weeks, longer each time it is blocked again. The message after a block has an
**Undo** button that puts everything back and plays the song again. Skipped
tracks are ranked lower for about two months.

Tap **Add** to add a listener, and **Edit** (or press and hold a name) to rename
or remove one. Names are just labels.

## HTTP API

`GET /status`, `GET /dj/status`, `GET /queue`, `GET /search?q=`, `GET /players`,
`GET /history`, `GET /users`, `POST /vibe`, `POST /start_radio`,
`POST /start_radio_seed`, `POST /stop`, `POST /playpause`, `POST /skip`,
`POST /previous`, `POST /nudge`, `POST /energy`, `POST /like`, `POST /block`,
`POST /block/undo`, `POST /volume`, `POST /players/select`, `POST /save_playlist`,
`POST /queue/play_next`, `POST /queue/remove`, `POST /queue/move`.
