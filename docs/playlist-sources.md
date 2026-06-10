# Research spike: more public playlist seed sources

**Status:** research only — nothing implemented this round. Today the only seed
source is **YouTube Music** (`scheduler.seed_from_playlist`, via `ytmusicapi`,
no auth). This captures feasibility/effort for adding more public sources so a
future change can pick the best one.

## Goal

"Seed from a public playlist URL" should accept more than YouTube Music, so a
listener can point the DJ at a playlist they already have elsewhere. The
constraint: **public, no per-user login** (the add-on shouldn't require the user
to OAuth into a music account).

## Candidates

| Source | Auth for public playlists | Effort | Notes |
| --- | --- | --- | --- |
| **YouTube Music** | None (`ytmusicapi`) | ✅ done | Current implementation. |
| **Deezer** | **None** — public REST API (`api.deezer.com/playlist/{id}`) | Low | Best next add: returns tracks (title + artist) without any key. Just fetch + map to "Artist - Track" queries, same as the YT Music path. |
| **Spotify** | **App credentials** (client-id + secret, *client-credentials* flow — no user login) | Medium | Public playlists readable with an app token, but we'd need optional `spotify_client_id`/`spotify_client_secret` config and a token refresh. No user OAuth required. |
| **Apple Music** | Developer token (signed JWT) | High | Requires an Apple developer key; heavier. Skip. |

## Recommendation (future PR)

1. Generalize `seed_from_playlist` into a small **provider-detecting importer**
   keyed off the URL host (music.youtube.com → existing; deezer.com → Deezer;
   open.spotify.com → Spotify).
2. Add **Deezer first** — no credentials, lowest effort, immediate value.
3. Add **Spotify** behind optional `spotify_client_id`/`spotify_client_secret`
   config (skip gracefully when unset; show a hint that public Spotify import
   needs a free app key).
4. In all cases the importer just produces `"Artist - Track"` queries that flow
   through the existing taste-seed + enqueue path — Music Assistant still plays
   via the configured provider (e.g. Tidal). No new playback path.

## Tracking

Open a GitHub issue ("More public playlist seed sources") linking this doc.
