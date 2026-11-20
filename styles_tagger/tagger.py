"""TideSync style tagger: Discogs-EffNet styles and embeddings for every music file.

Runs on Heron next to the helper Music Assistants. It walks the music share (mounted
read-only), cuts three 10-second excerpts from each file, and runs Essentia's
Discogs-EffNet model on them (the dynamic-batch ONNX export, through ONNX Runtime:
about 20 times faster on CPU than the fixed-batch TensorFlow graph, same output).
Per file it keeps:

* ``styles``: probabilities for 400 Discogs styles ("Folk, World, & Country---Folk",
  "Funk / Soul---Contemporary R&B", "Pop---K-pop", ...), averaged over the excerpts,
  stored as 400 bytes (probability x 255),
* ``emb``: the 1280-dim style embedding, averaged and L2-normalised, as float16,
* tags read from the file (artist, title, album, track, disc, duration),
* the Navidrome song id(s) of the file, from Navidrome's real-path report to the
  Subsonic client "ma-heron" (see navidrome_ids()), so a client whose library knows
  songs by Navidrome id (Music Assistant's opensubsonic provider) can match files.

An HTTP API (port 8791) serves the results:

* ``GET /v1/status``  progress counts and speed
* ``GET /v1/labels``  the 400 style names, in vector order
* ``GET /v1/tracks?after=SEQ&limit=N``  analysed files changed after ``SEQ`` (a
  per-row counter that grows on every (re)analysis), for incremental sync
* ``GET /v1/similar?path=P&limit=N``  files whose style embedding is closest to P's

Model weights are by the MTG (UPF), CC BY-NC-SA 4.0: free for this personal,
non-commercial use.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import logging
import multiprocessing as mp
import os
import sqlite3
import subprocess
import secrets
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

LOG = logging.getLogger("tagger")
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma",
             ".alac", ".ape", ".wv", ".dsf"}
SR = 16000
CROP_SECONDS = 10.0
CROP_POSITIONS = (0.2, 0.5, 0.8)  # early, middle and late section: about 28 model patches
WHOLE_BELOW = 45.0  # shorter files are analysed whole
RESCAN_EVERY = 6 * 3600
NAVIDROME_EVERY = 6 * 3600
NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://192.168.3.100:4533/rest/")
NAVIDROME_CLIENT = "ma-heron"  # the Navidrome player entry with "Report Real Path" on
EMB_DIM, N_STYLES = 1280, 400
REFRESH_EVERY = 600  # similarity index reload interval while new files keep arriving

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    size INTEGER, mtime INTEGER,
    status INTEGER NOT NULL DEFAULT 0,
    priority INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    duration REAL, artist TEXT, title TEXT, album TEXT, track INTEGER, disc INTEGER,
    styles BLOB, emb BLOB,
    seq INTEGER,
    analysed_at INTEGER,
    navidrome_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_files_queue ON files(status, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_files_seq ON files(seq);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
PENDING, DONE, ERROR = 0, 1, 2


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def walk(root: Path):
    """Yield (relative path, size, mtime) for every audio file under ``root``."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif Path(entry.name).suffix.lower() in AUDIO_EXT:
                            st = entry.stat(follow_symlinks=False)
                            yield os.path.relpath(entry.path, root), st.st_size, int(st.st_mtime)
                    except OSError as err:
                        LOG.warning("skipping %s: %s", entry.path, err)
        except OSError as err:
            LOG.warning("cannot list %s: %s", folder, err)


def scan(conn: sqlite3.Connection, root: Path) -> dict[str, int]:
    """Bring the file table in line with the share: new and changed files become pending."""
    started = time.time()
    known = {r["path"]: (r["size"], r["mtime"]) for r in conn.execute("SELECT path, size, mtime FROM files")}
    seen, added, changed = set(), 0, 0
    batch: list[tuple] = []
    for rel, size, mtime in walk(root):
        seen.add(rel)
        old = known.get(rel)
        if old is None:
            batch.append(("new", rel, size, mtime))
            added += 1
        elif old != (size, mtime):
            batch.append(("changed", rel, size, mtime))
            changed += 1
        if len(batch) >= 2000:
            _apply_scan(conn, batch)
            batch.clear()
    _apply_scan(conn, batch)
    gone = [p for p in known if p not in seen]
    if seen:  # never wipe the table because the share was not mounted
        conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
    conn.commit()
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('scanned_at', ?)", (str(int(time.time())),))
    conn.commit()
    out = {"files": len(seen), "added": added, "changed": changed, "removed": len(gone) if seen else 0,
           "seconds": round(time.time() - started)}
    LOG.info("scan: %s", out)
    return out


def _apply_scan(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    for kind, rel, size, mtime in batch:
        if kind == "new":
            conn.execute("INSERT OR IGNORE INTO files(path, size, mtime) VALUES(?,?,?)", (rel, size, mtime))
        else:
            conn.execute("UPDATE files SET size=?, mtime=?, status=0, error=NULL WHERE path=?", (size, mtime, rel))
    conn.commit()


def _navidrome_call(endpoint: str, **params) -> dict:
    """Read-only Subsonic call to Navidrome (admin credentials from /run/secrets)."""
    user = Path("/run/secrets/navidrome_user").read_text().strip()
    pw = Path("/run/secrets/navidrome_password").read_text().strip()
    salt = secrets.token_hex(6)
    q = {"u": user, "t": hashlib.md5((pw + salt).encode()).hexdigest(), "s": salt,  # noqa: S324 - Subsonic auth
         "v": "1.16.1", "c": NAVIDROME_CLIENT, "f": "json", **params}
    with urllib.request.urlopen(NAVIDROME_URL + endpoint + "?" + urllib.parse.urlencode(q), timeout=120) as r:
        body = json.loads(r.read())["subsonic-response"]
    if body.get("status") != "ok":
        raise RuntimeError(f"{endpoint}: {body.get('error')}")
    return body


def _pkey(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def navidrome_ids(conn: sqlite3.Connection) -> dict[str, int]:
    """Record each file's Navidrome song id(s); rows whose ids change get a new seq.

    Navidrome reports real paths ("/music/<path under Music>") to the client
    "ma-heron". Lookup is exact first, then by share identity (NFC + casefold) when
    that names exactly one file, because the share is case-insensitive while the
    disk below it is not (folders differing only in case exist).
    """
    exact: dict[str, set[str]] = collections.defaultdict(set)
    meta: dict[str, tuple] = {}
    offset = 0
    while True:
        res = _navidrome_call("search3", query="", songCount=500, songOffset=offset, artistCount=0, albumCount=0)
        batch = (res.get("searchResult3") or {}).get("song") or []
        if not batch:
            break
        for song in batch:
            path = song.get("path") or ""
            if path.startswith("/music/"):
                rel = unicodedata.normalize("NFC", path[len("/music/"):])
                exact[rel].add(song["id"])
                meta[rel] = (song.get("duration"), song.get("artist"), song.get("title"), song.get("album"),
                             song.get("track"), song.get("discNumber"))
        offset += len(batch)
    if not exact:
        raise RuntimeError("Navidrome reported no real paths (is Report Real Path on for 'ma-heron'?)")
    by_key: dict[str, set[str]] = collections.defaultdict(set)
    for path in exact:
        by_key[_pkey(path)].add(path)

    def resolve(path: str) -> str | None:
        norm = unicodedata.normalize("NFC", path)
        if norm in exact:
            return norm
        paths = by_key.get(_pkey(path), set())
        return next(iter(paths)) if len(paths) == 1 else None

    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM files").fetchone()[0]
    changed = matched = 0
    for row in conn.execute("SELECT id, path, status, navidrome_ids FROM files").fetchall():
        key = resolve(row["path"])
        ids = sorted(exact[key]) if key else []
        matched += bool(ids)
        if key:
            dur, artist, title, album, track, disc = meta[key]
            conn.execute("UPDATE files SET duration=COALESCE(duration, ?), artist=?, title=?, album=?, track=?,"
                         " disc=? WHERE id=?", (dur, artist, title, album, track, disc, row["id"]))
        value = json.dumps(ids) if ids else None
        if value != row["navidrome_ids"]:
            changed += 1
            if row["status"] == DONE:  # resend analysed rows so clients pick up the new ids
                seq += 1
                conn.execute("UPDATE files SET navidrome_ids=?, seq=? WHERE id=?", (value, seq, row["id"]))
            else:
                conn.execute("UPDATE files SET navidrome_ids=? WHERE id=?", (value, row["id"]))
    conn.commit()
    out = {"navidrome_paths": len(exact), "files_matched": matched, "changed": changed}
    LOG.info("navidrome ids: %s", out)
    return out


def prioritise(conn: sqlite3.Connection, paths: list[str]) -> int:
    """Put the listed files at the very front of the queue (above priority 1 bulk queues)."""
    n = 0
    for p in paths:
        n += conn.execute("UPDATE files SET priority=MAX(priority, 2) WHERE path=? AND status<>1", (p,)).rowcount
    conn.commit()
    return n


# --------------------------------------------------------------------------
# analysis (runs in worker processes)
# --------------------------------------------------------------------------

_MODELS: dict[str, object] = {}


MODEL_FILE = "discogs-effnet-bsdynamic-1.onnx"
LABELS_FILE = "discogs-effnet-bs64-1.json"  # its "classes" are the 400 styles, in output order
PATCH_FRAMES, PATCH_HOP = 128, 62  # mel frames per model patch and between patches (Essentia's defaults)


def _init_worker(models_dir: str) -> None:
    os.nice(5)
    import onnxruntime as ort  # noqa: PLC0415
    from essentia.standard import TensorflowInputMusiCNN  # noqa: PLC0415  (mel bands only, no TensorFlow)

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1  # one core per worker; parallelism comes from the worker pool
    opts.inter_op_num_threads = 1
    _MODELS["sess"] = ort.InferenceSession(str(Path(models_dir) / MODEL_FILE), opts,
                                           providers=["CPUExecutionProvider"])
    _MODELS["mel"] = TensorflowInputMusiCNN()


def _embed(audio: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-patch style probabilities (n, 400) and embeddings (n, 1280) for 16 kHz audio."""
    from essentia.standard import FrameGenerator  # noqa: PLC0415

    mel_of = _MODELS["mel"]
    mel = np.array([mel_of(frame) for frame in FrameGenerator(audio, frameSize=512, hopSize=256,
                                                               startFromZero=True)], dtype=np.float32)
    if len(mel) < PATCH_FRAMES:
        raise RuntimeError("too little audio for one model patch")
    patches = np.stack([mel[i:i + PATCH_FRAMES] for i in range(0, len(mel) - PATCH_FRAMES + 1, PATCH_HOP)])
    styles, emb = _MODELS["sess"].run(["activations", "embeddings"], {"melspectrogram": patches})
    return np.asarray(styles, dtype=np.float32), np.asarray(emb, dtype=np.float32)


def _probe(path: str) -> dict:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True, timeout=60, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe: {res.stderr.strip()[:200]}")
    fmt = json.loads(res.stdout).get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    def num(v):
        try:
            return int(str(v).split("/")[0])
        except (TypeError, ValueError):
            return None

    return {"duration": float(fmt.get("duration") or 0.0), "artist": tags.get("artist") or tags.get("album_artist"),
            "title": tags.get("title"), "album": tags.get("album"), "track": num(tags.get("track")),
            "disc": num(tags.get("disc") or tags.get("discnumber"))}


def _decode(path: str, start: float, seconds: float | None) -> np.ndarray:
    cmd = ["ffmpeg", "-nostdin", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    if seconds:
        cmd += ["-t", f"{seconds:.2f}"]
    cmd += ["-i", path, "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    res = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg: {res.stderr.decode(errors='replace').strip()[:200]}")
    return np.frombuffer(res.stdout, dtype=np.float32)


_RESAMPLERS: dict[int, object] = {}


def _crops(dur: float) -> list[tuple[float, float | None]]:
    """(start, seconds) of the excerpts to analyse; the whole file when it is short."""
    if dur < WHOLE_BELOW:
        return [(0.0, None)]
    return [(max(0.0, dur * p - CROP_SECONDS / 2), CROP_SECONDS) for p in CROP_POSITIONS]


def _read_crops(path: str) -> tuple[np.ndarray, float]:
    """Excerpts at 16 kHz mono, opening the file once and seeking (FLAC, WAV, OGG, MP3).

    Opening once matters on a network share: every open re-reads the file's
    metadata, and FLAC files often carry megabytes of embedded cover art.
    """
    import soundfile as sf  # noqa: PLC0415
    from essentia.standard import Resample  # noqa: PLC0415

    with sf.SoundFile(path) as f:
        sr, frames = f.samplerate, f.frames
        if not sr or frames <= 0:
            raise RuntimeError("empty audio")
        dur = frames / sr
        if sr != SR and sr not in _RESAMPLERS:
            _RESAMPLERS[sr] = Resample(inputSampleRate=float(sr), outputSampleRate=float(SR), quality=1)
        parts = []
        for start, seconds in _crops(dur):
            f.seek(int(start * sr))
            data = f.read(frames if seconds is None else int(seconds * sr), dtype="float32", always_2d=True)
            mono = np.ascontiguousarray(data.mean(axis=1), dtype=np.float32)
            parts.append(mono if sr == SR else np.asarray(_RESAMPLERS[sr](mono), dtype=np.float32))
    return np.concatenate(parts), dur


def analyse(job: tuple[int, str]) -> tuple[int, dict | None, str | None]:
    """Styles and embedding for one file; ``(id, result, error)``."""
    file_id, path = job
    try:
        try:
            audio, dur = _read_crops(path)
            info = {"duration": dur, "artist": None, "title": None, "album": None, "track": None, "disc": None}
        except Exception:  # noqa: BLE001 - containers soundfile cannot read (m4a, opus, wma...)
            info = _probe(path)
            dur = info["duration"]
            if dur <= 0:
                raise RuntimeError("no duration") from None
            audio = np.concatenate([_decode(path, start, seconds) for start, seconds in _crops(dur)])
        if audio.size < SR * 3:
            raise RuntimeError("too little audio decoded")
        per_patch, emb = _embed(audio)
        if emb.ndim != 2 or emb.shape[1] != EMB_DIM or per_patch.shape[1] != N_STYLES:
            raise RuntimeError(f"unexpected model output {per_patch.shape} {emb.shape}")
        styles = per_patch.mean(axis=0)
        mean = emb.mean(axis=0)
        mean /= np.linalg.norm(mean) or 1.0
        info["styles"] = np.clip(np.rint(styles * 255), 0, 255).astype(np.uint8).tobytes()
        info["emb"] = mean.astype(np.float16).tobytes()
        return file_id, info, None
    except Exception as err:  # noqa: BLE001 - one bad file must not stop the run
        return file_id, None, str(err)[:300]


# --------------------------------------------------------------------------
# similarity index (embeddings of every analysed file, in memory)
# --------------------------------------------------------------------------


class Index:
    def __init__(self, db: Path) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._seq = -1
        self._loaded = 0.0
        self.paths: list[str] = []
        self.ids: list[list[str]] = []
        self.pos: dict[str, int] = {}
        self.by_id: dict[str, int] = {}
        self.matrix = np.zeros((0, EMB_DIM), dtype=np.float32)

    def keep_fresh(self) -> None:
        """Background thread: reload new embeddings every REFRESH_EVERY seconds."""
        while True:
            try:
                self.refresh(force=True)
            except Exception:  # noqa: BLE001 - never let the index thread die
                LOG.exception("similarity index refresh failed")
            time.sleep(REFRESH_EVERY)

    def refresh(self, force: bool = False) -> None:
        """Reload embeddings when new ones arrived (requests only load an empty index)."""
        if not force and self.paths:
            return
        self._loaded = time.time()
        conn = connect(self._db)
        try:
            top = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM files").fetchone()[0]
            if top == self._seq and not force:
                return
            rows = conn.execute("SELECT path, emb, navidrome_ids FROM files WHERE status=1 AND emb IS NOT NULL"
                                " ORDER BY id").fetchall()
        finally:
            conn.close()
        matrix = np.empty((len(rows), EMB_DIM), dtype=np.float32)
        for i, r in enumerate(rows):
            matrix[i] = np.frombuffer(r["emb"], dtype=np.float16)
        paths = [r["path"] for r in rows]
        ids = [json.loads(r["navidrome_ids"]) if r["navidrome_ids"] else [] for r in rows]
        with self._lock:
            self.matrix, self.paths, self.ids, self._seq = matrix, paths, ids, top
            self.pos = {p: i for i, p in enumerate(paths)}
            self.by_id = {sid: i for i, sids in enumerate(ids) for sid in sids}
        LOG.info("similarity index: %d files", len(paths))

    def similar(self, path: str, limit: int, song_id: str = "") -> list[dict] | None:
        with self._lock:
            i = self.pos.get(path) if path else self.by_id.get(song_id)
            if i is None:
                return None
            scores = self.matrix @ self.matrix[i]
            scores[i] = -2.0
            k = min(limit, len(scores) - 1)
            if k <= 0:
                return []
            top = np.argpartition(-scores, k - 1)[:k]
            top = top[np.argsort(-scores[top])]
            return [{"path": self.paths[j], "navidrome_ids": self.ids[j], "score": round(float(scores[j]), 4)}
                    for j in top]


# --------------------------------------------------------------------------
# HTTP API
# --------------------------------------------------------------------------


def make_handler(db: Path, labels: list[str], index: Index, state: dict):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            LOG.debug("http: " + fmt, *args)

        def _send(self, code: int, body) -> None:
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/v1/status":
                    return self._send(200, status(db, state))
                if url.path == "/v1/labels":
                    return self._send(200, {"labels": labels})
                if url.path == "/v1/tracks":
                    return self._send(200, tracks(db, int(q.get("after", 0)), min(int(q.get("limit", 1000)), 5000)))
                if url.path == "/v1/similar":
                    index.refresh()
                    res = index.similar(q.get("path", ""), min(int(q.get("limit", 100)), 2000),
                                        song_id=q.get("navidrome_id", ""))
                    if res is None:
                        return self._send(404, {"error": "not analysed", "path": q.get("path"),
                                                "navidrome_id": q.get("navidrome_id")})
                    return self._send(200, {"path": q.get("path"), "items": res})
                return self._send(404, {"error": "unknown endpoint"})
            except (ValueError, sqlite3.Error) as err:
                return self._send(400, {"error": str(err)})

    return Handler


def status(db: Path, state: dict) -> dict:
    conn = connect(db)
    try:
        counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM files GROUP BY status")}
        hour = conn.execute("SELECT COUNT(*) FROM files WHERE analysed_at > ?", (int(time.time()) - 3600,)).fetchone()[0]
        scanned = conn.execute("SELECT value FROM meta WHERE key='scanned_at'").fetchone()
    finally:
        conn.close()
    pending = counts.get(PENDING, 0)
    return {"files": sum(counts.values()), "done": counts.get(DONE, 0), "pending": pending,
            "errors": counts.get(ERROR, 0), "per_hour": hour,
            "eta_hours": round(pending / hour, 1) if hour else None,
            "scanned_at": int(scanned[0]) if scanned else None, "workers": state.get("workers"),
            "model": "discogs-effnet (ONNX, 400 Discogs styles + 1280-dim embedding)"}


def tracks(db: Path, after: int, limit: int) -> dict:
    conn = connect(db)
    try:
        rows = conn.execute(
            "SELECT seq, path, size, mtime, duration, artist, title, album, track, disc, styles, navidrome_ids"
            " FROM files"
            " WHERE status=1 AND seq > ? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
    finally:
        conn.close()
    items = [{**{k: r[k] for k in ("seq", "path", "size", "mtime", "duration", "artist", "title", "album",
                                    "track", "disc")},
              "navidrome_ids": json.loads(r["navidrome_ids"]) if r["navidrome_ids"] else [],
              "styles": base64.b64encode(r["styles"]).decode()} for r in rows]
    return {"items": items, "next": items[-1]["seq"] if items else after}


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------


def run(args) -> None:
    db = Path(args.data) / "styles.db"
    conn = connect(db)
    conn.executescript(SCHEMA)
    if "navidrome_ids" not in {r[1] for r in conn.execute("PRAGMA table_info(files)")}:
        conn.execute("ALTER TABLE files ADD COLUMN navidrome_ids TEXT")
        conn.commit()
    labels = json.loads((Path(args.models) / LABELS_FILE).read_text())["classes"]
    root = Path(args.music)
    state = {"workers": args.workers}
    index = Index(db)
    threading.Thread(target=index.keep_fresh, daemon=True, name="index").start()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(db, labels, index, state))
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
    LOG.info("API on port %d; music at %s", args.port, root)

    if not root.is_dir():
        raise SystemExit(f"music folder {root} is not available")

    def meta_time(key: str) -> float:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return float(row[0]) if row else 0.0

    last_scan = meta_time("scanned_at")
    if time.time() - last_scan > RESCAN_EVERY:  # a restart does not need a fresh listing
        scan(conn, root)
        last_scan = time.time()
    if args.priority_file and Path(args.priority_file).exists():
        paths = [ln.strip() for ln in Path(args.priority_file).read_text().splitlines() if ln.strip()]
        LOG.info("prioritised %d of %d listed files", prioritise(conn, paths), len(paths))
    last_ids = meta_time("navidrome_at_v2")

    ctx = mp.get_context("spawn")
    with ctx.Pool(args.workers, initializer=_init_worker, initargs=(args.models,), maxtasksperchild=2000) as pool:
        while True:
            if time.time() - last_scan > RESCAN_EVERY:
                scan(conn, root)
                last_scan = time.time()
            if time.time() - last_ids > NAVIDROME_EVERY:
                try:
                    navidrome_ids(conn)
                except Exception as err:  # noqa: BLE001 - Navidrome down: keep tagging, retry in an hour
                    LOG.warning("navidrome ids not refreshed: %s", err)
                    last_ids = time.time() - NAVIDROME_EVERY + 3600
                else:
                    last_ids = time.time()
                    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('navidrome_at_v2', ?)",
                                 (str(int(last_ids)),))
                    conn.commit()
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM files").fetchone()[0]
            rows = conn.execute(
                "SELECT id, path FROM files WHERE status=0 ORDER BY priority DESC, id LIMIT ?",
                (args.workers * 64,)).fetchall()
            if not rows:
                time.sleep(60)
                continue
            jobs = [(r["id"], str(root / r["path"])) for r in rows]
            done = 0
            for file_id, info, err in pool.imap_unordered(analyse, jobs, chunksize=4):
                now = int(time.time())
                if info is None:
                    conn.execute("UPDATE files SET status=2, error=?, analysed_at=? WHERE id=?", (err, now, file_id))
                else:
                    seq += 1
                    conn.execute(
                        "UPDATE files SET status=1, error=NULL, duration=?, artist=COALESCE(?, artist),"
                        " title=COALESCE(?, title), album=COALESCE(?, album), track=COALESCE(?, track),"
                        " disc=COALESCE(?, disc), styles=?, emb=?, seq=?, analysed_at=? WHERE id=?",
                        (info["duration"], info["artist"], info["title"], info["album"], info["track"],
                         info["disc"], info["styles"], info["emb"], seq, now, file_id))
                done += 1
                if done % 50 == 0:
                    conn.commit()
            conn.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music", default="/mnt/media/Music")
    ap.add_argument("--models", default="/models")
    ap.add_argument("--data", default="/data")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--priority-file", default="/data/priority.txt")
    ap.add_argument("--analyse", help="analyse one file and print the top styles (no database)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.analyse:
        _init_worker(args.models)
        labels = json.loads((Path(args.models) / LABELS_FILE).read_text())["classes"]
        t = time.time()
        _, info, err = analyse((0, args.analyse))
        if err:
            raise SystemExit(err)
        styles = np.frombuffer(info["styles"], dtype=np.uint8) / 255
        top = np.argsort(-styles)[:8]
        print(json.dumps({k: info[k] for k in ("duration", "artist", "title", "album")}, ensure_ascii=False))
        print(f"{time.time() - t:.2f}s", [(labels[i], round(float(styles[i]), 3)) for i in top])
        return
    run(args)


if __name__ == "__main__":
    main()
