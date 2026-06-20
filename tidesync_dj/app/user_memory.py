"""Per-person memory, stored as human-readable Markdown under /data/users.

Each person gets one Markdown file (e.g. ``/data/users/mom.md``) holding their
likes, time-limited blocks, learned moods by time of day, and a personal taste
summary. A tiny ``_active.json`` records which person is currently selected.

This is deliberately separate from :class:`TasteProfile`, which holds the
shared, library-derived household baseline. ``UserMemory`` is the *personal*
layer laid over that baseline at decision time.

The Markdown is the source of truth. Parsing is defensive: malformed lines are
skipped rather than raising, so a hand-edit can never crash the add-on.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from timectx import month_of_iso, slot_of_iso

_LOGGER = logging.getLogger(__name__)

BLOCK_DAYS = 30  # legacy default, used only to back-fill `created` on old lines
DEFAULT_PEOPLE = ["Mom", "Dad", "Kids"]
MAX_LIKES = 200
MAX_MOODS_PER_SLOT = 10
MAX_BLOCK_HISTORY = 200

# Escalating block cooldowns: the more often a track is blocked, the longer it
# rests before re-emerging. A single block is a short "I'm tired of this"; repeat
# blocks escalate toward a genuine, effectively-permanent dislike.
BLOCK_COOLDOWNS = {1: 21, 2: 60, 3: 180}  # days, keyed by block count
BLOCK_COOLDOWN_MAX = 3650                 # days for count >= 4 (~permanent)
DISLIKE_AT = 3                            # count >= this == genuine dislike

# Starter taste for seeded default people so a fresh profile already sounds like
# itself before any learning. Names not listed start blank and fall back to the
# household taste until learning fills them in.
DEFAULT_SUMMARIES = {
    "Kids": (
        "Upbeat, family-friendly kids music: high-energy dance and singalong "
        "songs, playful novelty tracks, and clean pop. Movie/show soundtracks "
        "(Disney, Encanto, Frozen, Moana, Trolls) and artists like Parry Gripp, "
        "Kidz Bop, Laurie Berkner, Koo Koo Kanga Roo, and They Might Be Giants' "
        "kids albums. Keep it cheerful, bouncy and age-appropriate — no explicit "
        "lyrics and no heavy or melancholy adult material."
    ),
}


def _block_cooldown_days(count: int) -> int:
    """Cooldown length (days) for the Nth block of a track."""
    return BLOCK_COOLDOWNS.get(count, BLOCK_COOLDOWN_MAX)

# Section headers used in the Markdown files.
_H_SUMMARY = "Taste Profile"
_H_LIKES = "Likes"
_H_BLOCKS = "Blocks"
_H_MOODS = "Moods by Time of Day"
_NONE_MARK = "(none yet)"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "person"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_dt(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class Like:
    ts: str
    label: str
    uri: str


@dataclass
class Block:
    expires: str
    label: str
    uri: str
    count: int = 1          # times this track has been blocked (survives expiry)
    created: str = ""       # ISO ts of the FIRST block, for slot/month bucketing

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the active cooldown has passed (the track may re-emerge).

        Expiry only ends *suppression* — the record itself is kept so `count`
        survives and escalation/dislike learning still works.
        """
        dt = _parse_dt(self.expires)
        if dt is None:
            return True  # unparseable expiry → treat as expired (never permanent)
        return (now or _now()) >= dt

    @property
    def is_disliked(self) -> bool:
        return self.count >= DISLIKE_AT


@dataclass
class Mood:
    slot: str
    ts: str
    vibe: str


@dataclass
class Person:
    name: str
    slug: str
    summary: str = ""
    likes: list[Like] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    moods: list[Mood] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # Mutations (callers persist via UserStore.save)
    # ------------------------------------------------------------------ #
    def add_like(self, label: str | None, uri: str | None) -> None:
        if not uri:
            return
        # De-dupe by uri — keep the most recent timestamp at the end.
        self.likes = [lk for lk in self.likes if lk.uri != uri]
        self.likes.append(Like(ts=_iso(_now()), label=label or uri, uri=uri))
        if len(self.likes) > MAX_LIKES:
            self.likes = self.likes[-MAX_LIKES:]

    def remove_like(self, uri: str | None) -> bool:
        """Remove a like by uri (the un-like half of the heart toggle)."""
        if not uri:
            return False
        before = len(self.likes)
        self.likes = [lk for lk in self.likes if lk.uri != uri]
        return len(self.likes) != before

    def is_liked(self, uri: str | None) -> bool:
        return bool(uri) and any(lk.uri == uri for lk in self.likes)

    def add_block(self, label: str | None, uri: str | None) -> None:
        """Block a track, escalating if it has been blocked before.

        First block rests the track for a short cooldown; re-blocking the same
        track bumps `count` and lengthens the cooldown toward a permanent
        dislike. The record is never deleted, so the count survives expiry.
        """
        if not uri:
            return
        now = _now()
        existing = next((b for b in self.blocks if b.uri == uri), None)
        if existing is not None:
            existing.count += 1
            if label:
                existing.label = label
            existing.expires = _iso(now + timedelta(days=_block_cooldown_days(existing.count)))
        else:
            self.blocks.append(
                Block(
                    expires=_iso(now + timedelta(days=_block_cooldown_days(1))),
                    label=label or uri,
                    uri=uri,
                    count=1,
                    created=_iso(now),
                )
            )
        self.cap_block_history()

    def cap_block_history(self) -> bool:
        """Bound the block list, evicting lowest-count / oldest first.

        Expired blocks are deliberately kept (their count drives escalation and
        the dislike signal), so we cap by history size instead of by expiry.
        Genuine dislikes (high count) are the last thing dropped.
        """
        if len(self.blocks) <= MAX_BLOCK_HISTORY:
            return False
        self.blocks.sort(key=lambda b: (b.count, b.created))
        self.blocks = self.blocks[len(self.blocks) - MAX_BLOCK_HISTORY:]
        return True

    def record_mood(self, slot: str, vibe: str) -> None:
        vibe = (vibe or "").strip()
        if not vibe:
            return
        self.moods.append(Mood(slot=slot, ts=_iso(_now()), vibe=vibe))
        # Trim to the most recent N entries per slot.
        per_slot: dict[str, list[Mood]] = {}
        for m in self.moods:
            per_slot.setdefault(m.slot, []).append(m)
        trimmed: list[Mood] = []
        for entries in per_slot.values():
            trimmed.extend(entries[-MAX_MOODS_PER_SLOT:])
        # Preserve chronological order.
        trimmed.sort(key=lambda m: m.ts)
        self.moods = trimmed

    def set_summary(self, text: str | None) -> None:
        self.summary = (text or "").strip()

    # ------------------------------------------------------------------ #
    # Views for the decision context
    # ------------------------------------------------------------------ #
    def recent_likes(self, n: int = 25) -> list[str]:
        return [lk.label for lk in self.likes[-n:]]

    def likes_for_slot(self, slot: str, n: int = 15) -> list[str]:
        """Likes whose timestamp falls in this part-of-day slot."""
        return [lk.label for lk in self.likes if slot_of_iso(lk.ts) == slot][-n:]

    def likes_for_month(self, month: int, n: int = 15) -> list[str]:
        """Likes whose timestamp falls in this calendar month (1-12)."""
        return [lk.label for lk in self.likes if month_of_iso(lk.ts) == month][-n:]

    def active_blocks(self, now: datetime | None = None) -> list[Block]:
        """Blocks still within their cooldown — the only ones that suppress."""
        now = now or _now()
        return [b for b in self.blocks if not b.is_expired(now)]

    def blocked_labels(self) -> list[str]:
        return [b.label for b in self.active_blocks()]

    def blocked_uris(self) -> set[str]:
        return {b.uri for b in self.active_blocks()}

    def disliked_labels(self) -> list[str]:
        """Tracks blocked enough times to count as a genuine dislike."""
        return [b.label for b in self.blocks if b.is_disliked]

    def moods_for(self, slot: str, n: int = 5) -> list[str]:
        return [m.vibe for m in self.moods if m.slot == slot][-n:]

    def moods_for_month(self, month: int, n: int = 5) -> list[str]:
        return [m.vibe for m in self.moods if month_of_iso(m.ts) == month][-n:]

    # ------------------------------------------------------------------ #
    # Learning signal bundle (fed to the per-person taste learner)
    # ------------------------------------------------------------------ #
    def learning_signals(self) -> dict[str, list[dict]]:
        """Structured per-person signals with time-of-day + month context.

        Blocks carry their `count`/`disliked` flag so the learner can weight a
        repeated block (real dislike) far above a one-off (momentary fatigue),
        and both above likes.
        """
        likes = [
            {"label": lk.label, "slot": slot_of_iso(lk.ts), "month": month_of_iso(lk.ts)}
            for lk in self.likes[-80:]
        ]
        blocks = [
            {
                "label": b.label,
                "count": b.count,
                "disliked": b.is_disliked,
                "slot": slot_of_iso(b.created),
                "month": month_of_iso(b.created),
            }
            for b in self.blocks
        ]
        moods = [
            {"vibe": m.vibe, "slot": m.slot, "month": month_of_iso(m.ts)}
            for m in self.moods
        ]
        return {"likes": likes, "blocks": blocks, "moods": moods}


# ---------------------------------------------------------------------- #
# Markdown (de)serialization
# ---------------------------------------------------------------------- #
def _serialize(person: Person) -> str:
    lines: list[str] = [f"# {person.name}", ""]

    lines.append(f"## {_H_SUMMARY}")
    lines.append(person.summary.strip() or _NONE_MARK)
    lines.append("")

    lines.append(f"## {_H_LIKES}")
    if person.likes:
        for lk in person.likes:
            lines.append(f"- {lk.ts} | {lk.label} | {lk.uri}")
    else:
        lines.append(_NONE_MARK)
    lines.append("")

    lines.append(f"## {_H_BLOCKS}")
    if person.blocks:
        for b in person.blocks:
            lines.append(
                f"- expires {b.expires} | x{b.count} | since {b.created} "
                f"| {b.label} | {b.uri}"
            )
    else:
        lines.append(_NONE_MARK)
    lines.append("")

    lines.append(f"## {_H_MOODS}")
    if person.moods:
        for m in person.moods:
            lines.append(f"- {m.slot} | {m.ts} | {m.vibe}")
    else:
        lines.append(_NONE_MARK)
    lines.append("")

    return "\n".join(lines) + "\n"


def _parse(text: str, slug: str) -> Person:
    name = slug
    section: str | None = None
    summary_lines: list[str] = []
    likes: list[Like] = []
    blocks: list[Block] = []
    moods: list[Mood] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not line.startswith("## "):
            name = line[2:].strip() or slug
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            continue

        if section == _H_SUMMARY:
            if line.strip() and line.strip() != _NONE_MARK:
                summary_lines.append(line)
            continue

        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        if body == _NONE_MARK:
            continue
        parts = [p.strip() for p in body.split(" | ")]

        if section == _H_LIKES and len(parts) >= 3:
            likes.append(Like(ts=parts[0], label=parts[1], uri=parts[2]))
        elif section == _H_BLOCKS and len(parts) >= 3:
            expires = parts[0]
            if expires.lower().startswith("expires "):
                expires = expires[len("expires "):].strip()
            rest = parts[1:]
            count = 1
            created = ""
            # Optional "x{n}" count token (new format).
            if rest and re.fullmatch(r"x\d+", rest[0]):
                count = int(rest[0][1:])
                rest = rest[1:]
            # Optional "since {iso}" created token (new format).
            if rest and rest[0].lower().startswith("since "):
                created = rest[0][len("since "):].strip()
                rest = rest[1:]
            if len(rest) < 2:
                continue  # malformed — need at least label + uri
            label, uri = rest[-2], rest[-1]
            if not created:
                # Legacy line: approximate first-block time as expiry minus the
                # old fixed 30-day window so slot/month bucketing still works.
                exp_dt = _parse_dt(expires)
                created = _iso(exp_dt - timedelta(days=BLOCK_DAYS)) if exp_dt else _iso(_now())
            blocks.append(
                Block(expires=expires, label=label, uri=uri, count=count, created=created)
            )
        elif section == _H_MOODS and len(parts) >= 3:
            moods.append(Mood(slot=parts[0], ts=parts[1], vibe=parts[2]))

    return Person(
        name=name,
        slug=slug,
        summary="\n".join(summary_lines).strip(),
        likes=likes,
        blocks=blocks,
        moods=moods,
    )


# ---------------------------------------------------------------------- #
# Store
# ---------------------------------------------------------------------- #
class UserStore:
    """Loads/saves the set of people and tracks which one is active."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / "users"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not create users dir %s: %s", self._dir, err)
        self._active_path = self._dir / "_active.json"
        self._people: dict[str, Person] = {}
        self._active_slug: str | None = None
        self._load_all()
        self._ensure_seed()

    # -- loading / seeding ------------------------------------------------
    def _load_all(self) -> None:
        for path in sorted(self._dir.glob("*.md")):
            try:
                person = _parse(path.read_text(encoding="utf-8"), slugify(path.stem))
            except OSError as err:  # noqa: BLE001
                _LOGGER.warning("Could not read %s: %s", path, err)
                continue
            if person.cap_block_history():
                self._write(person)
            self._people[person.slug] = person
        if self._active_path.exists():
            try:
                self._active_slug = json.loads(
                    self._active_path.read_text(encoding="utf-8")
                ).get("active")
            except (ValueError, OSError):
                self._active_slug = None

    def _ensure_seed(self) -> None:
        if not self._people:
            _LOGGER.info("Seeding default people: %s", DEFAULT_PEOPLE)
            for name in DEFAULT_PEOPLE:
                person = Person(
                    name=name,
                    slug=slugify(name),
                    summary=DEFAULT_SUMMARIES.get(name, ""),
                )
                self._people[person.slug] = person
                self._write(person)
        if self._active_slug not in self._people:
            self._active_slug = next(iter(self._people))
            self._save_active()

    # -- persistence ------------------------------------------------------
    def _path_for(self, slug: str) -> Path:
        return self._dir / f"{slug}.md"

    def _write(self, person: Person) -> None:
        try:
            self._path_for(person.slug).write_text(_serialize(person), encoding="utf-8")
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not write memory for %s: %s", person.slug, err)

    def _save_active(self) -> None:
        try:
            self._active_path.write_text(
                json.dumps({"active": self._active_slug}), encoding="utf-8"
            )
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not persist active person: %s", err)

    def save(self, person: Person) -> None:
        """Persist a person after a mutation."""
        self._write(person)

    # -- public API -------------------------------------------------------
    @property
    def active(self) -> Person:
        return self._people[self._active_slug]  # _ensure_seed guarantees validity

    @property
    def active_slug(self) -> str:
        return self._active_slug  # type: ignore[return-value]

    def get(self, slug: str) -> Person | None:
        """Look up a person by slug (read-only; None if unknown)."""
        return self._people.get(slug)

    def people(self) -> list[dict[str, str | bool]]:
        return [
            {"slug": p.slug, "name": p.name, "active": p.slug == self._active_slug}
            for p in self._people.values()
        ]

    def select(self, slug: str) -> bool:
        if slug not in self._people:
            return False
        self._active_slug = slug
        self._save_active()
        return True

    def add_person(self, name: str) -> Person:
        slug = slugify(name)
        if slug in self._people:
            return self._people[slug]
        person = Person(name=name.strip() or slug, slug=slug)
        self._people[slug] = person
        self._write(person)
        return person

    def rename(self, slug: str, name: str) -> bool:
        """Change a person's display NAME only — slug/file stay stable so all
        learned data (keyed by slug) is preserved. Names are free-text."""
        person = self._people.get(slug)
        if person is None or not name.strip():
            return False
        person.name = name.strip()
        self._write(person)
        return True

    def remove(self, slug: str) -> bool:
        """Delete a person and their file. Refuses to remove the last person."""
        if slug not in self._people or len(self._people) <= 1:
            return False
        self._people.pop(slug, None)
        try:
            self._path_for(slug).unlink(missing_ok=True)
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not delete memory for %s: %s", slug, err)
        if self._active_slug == slug:
            self._active_slug = next(iter(self._people))
            self._save_active()
        return True
