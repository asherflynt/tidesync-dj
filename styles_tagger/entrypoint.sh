#!/bin/sh
# Mount the music share read-only, then run the tagger. Credentials come from a
# read-only secret file (username=/password= lines), never from the image.
set -e
mkdir -p /mnt/media
if ! mountpoint -q /mnt/media; then
  mount -t cifs "//${SMB_HOST:-192.168.3.100}/${SMB_SHARE:-media}" /mnt/media \
    -o "ro,credentials=/run/secrets/smb.cred,vers=3.0,iocharset=utf8,noserverino,actimeo=60"
fi
exec python /app/tagger.py "$@"
