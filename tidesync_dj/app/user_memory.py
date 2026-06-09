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

_LOGGER = logging.getLogger(__name__)

BLOCK_DAYS = 30
DEFAULT_PEOPLE = ["Mom", "Dad", "Kids"]
MAX_LIKES = 200
MAX_MOODS_PER_SLOT = 10

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

    def is_expired(self, now: datetime | None = None) -> bool:
        dt = _parse_dt(self.expires)
        if dt is None:
            return True  # unparseable expiry → treat as expired (never permanent)
        return (now or _now()) >= dt


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

    def add_block(self, label: str | None, uri: str | None, days: int = BLOCK_DAYS) -> None:
        if not uri:
            return
        expires = _iso(_now() + timedelta(days=days))
        self.blocks = [b for b in self.blocks if b.uri != uri]
        self.blocks.append(Block(expires=expires, label=label or uri, uri=uri))

    def purge_expired_blocks(self) -> bool:
        now = _now()
        kept = [b for b in self.blocks if not b.is_expired(now)]
        changed = len(kept) != len(self.blocks)
        self.blocks = kept
        return changed

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

    # ------------------------------------------------------------------ #
    # Views for the decision context
    # ------------------------------------------------------------------ #
    def recent_likes(self, n: int = 25) -> list[str]:
        return [lk.label for lk in self.likes[-n:]]

    def blocked_labels(self) -> list[str]:
        return [b.label for b in self.blocks]

    def blocked_uris(self) -> set[str]:
        return {b.uri for b in self.blocks}

    def moods_for(self, slot: str, n: int = 5) -> list[str]:
        return [m.vibe for m in self.moods if m.slot == slot][-n:]


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
            lines.append(f"- expires {b.expires} | {b.label} | {b.uri}")
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
            blocks.append(Block(expires=expires, label=parts[1], uri=parts[2]))
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
            if person.purge_expired_blocks():
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
                person = Person(name=name, slug=slugify(name))
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
