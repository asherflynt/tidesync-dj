# TideSync style tagger

An optional helper that runs on any Linux machine that can read your music files
(it does not run inside Home Assistant). For every audio file it measures, from the
sound itself, how much it resembles each of 400 Discogs music styles, using MTG's
Essentia Discogs-EffNet model. TideSync DJ uses this to keep mixes in their genre
(a soft R&B or K-pop ballad no longer follows a folk ballad just because both are
slow and gentle). It also uses it to reach songs Music Assistant has not analysed yet.

It never writes to your music: the share is mounted read-only.

## Run it

```sh
docker build -t tidesync-styles .
mkdir -p data secrets
# secrets/smb.cred: "username=..." and "password=..." lines for the music share
# secrets/navidrome_user, secrets/navidrome_password: optional, see below
docker run -d --name tidesync-styles --restart unless-stopped \
  --cap-add SYS_ADMIN --cap-add DAC_READ_SEARCH --security-opt apparmor=unconfined \
  -e SMB_HOST=192.168.3.100 -e SMB_SHARE=media \
  -v "$PWD/secrets/smb.cred:/run/secrets/smb.cred:ro" \
  -v "$PWD/secrets/navidrome_user:/run/secrets/navidrome_user:ro" \
  -v "$PWD/secrets/navidrome_password:/run/secrets/navidrome_password:ro" \
  -v "$PWD/data:/data" -p 8791:8791 \
  tidesync-styles --workers 8
```

The music folder inside the share defaults to `Music` (`--music /mnt/media/Music`).
Then set the add-on option **Style tagger address** to `http://<this machine>:8791`.

Expect roughly two seconds of CPU per song per worker, so a large library takes a
day or more on a busy machine. The DJ uses whatever is finished. New and changed
files are picked up every 6 hours.

## Matching files to your library

TideSync matches files by their path when Music Assistant reads the share directly
(filesystem provider). It also matches by Navidrome song id when your library comes
through Navidrome (opensubsonic provider). For the second, give the tagger a
Navidrome login. Navidrome must report real file paths to the Subsonic client
`ma-heron` (Navidrome > Settings > Players > Report Real Path). The tagger only reads.

## API

`GET /v1/status`, `GET /v1/labels`, `GET /v1/tracks?after=SEQ&limit=N`,
`GET /v1/similar?path=P&limit=N` (or `navidrome_id=ID`).

## Licence

The tagger code is part of TideSync. The Discogs-EffNet weights are by the Music
Technology Group (UPF), licensed CC BY-NC-SA 4.0: personal, non-commercial use only.
Essentia is AGPL-3.0.
