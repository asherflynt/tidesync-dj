"""Turn a vibe into a set plan (v2).

A plan says *what to search for* and *what shape the set has*; it never names
tracks. Two interpreters produce the same structure:

* ``heuristic_plan`` - keyword rules, no network, always available.
* ``ClaudePlanner``  - optional; one small Claude call for a richer reading.

If the Claude call fails for any reason the heuristic plan is used, so the DJ
never depends on an API key.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sequencer import ARCS

_LOGGER = logging.getLogger("tidesync.plan")

SET_LENGTH = 40  # tracks over which an arc plays out before it repeats


@dataclass
class Plan:
    vibe: str = ""
    queries: list[str] = field(default_factory=list)  # sound descriptions for sonic search
    avoid: str = ""  # sound description to steer away from
    arc: str = "wave"
    base_energy: float = 5.5
    span: float = 2.0
    note: str = ""
    source: str = "default"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_LOW = ("sleep", "calm", "chill", "relax", "quiet", "soft", "ambient", "study", "focus", "reading",
        "mellow", "gentle", "wind down", "winding down", "rainy", "morning coffee", "background")
_HIGH = ("party", "workout", "gym", "run", "dance", "hype", "energy", "energetic", "banger",
         "pump", "upbeat", "loud", "rage", "cleaning")
_BUILD = ("build", "warm up", "warm-up", "getting ready", "pregame", "pre-game", "ramp")
_DOWN = ("wind down", "winding down", "come down", "cool down", "bedtime", "falling asleep")

_SLOT_ENERGY = {"early_morning": 3.5, "morning": 5.0, "midday": 5.5, "afternoon": 6.0,
                "evening": 5.5, "night": 4.5, "late_night": 3.5}


def _has(text: str, words: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", text) for w in words)


def heuristic_plan(vibe: str, *, slot: str = "", energy_bias: int = 0) -> Plan:
    """Build a plan from keywords in the vibe and the time of day."""
    text = (vibe or "").strip()
    low = text.lower()
    base = _SLOT_ENERGY.get(slot, 5.5)
    arc, span = "wave", 2.0
    if text:
        if _has(low, _LOW):
            base, span = 3.0, 1.2
        if _has(low, _HIGH):
            base, span = 7.5, 1.5
        if _has(low, _BUILD):
            arc, span = "build", 2.5
        elif _has(low, _DOWN):
            arc, span = "wind_down", 2.0
    plan = Plan(
        vibe=text,
        queries=[text] if text else [],
        arc=arc,
        base_energy=base,
        span=span,
        note=f"Reading “{text}” as energy around {base:.0f}/10." if text
        else "No vibe set — following the sound of what is playing.",
        source="rules",
    )
    return apply_bias(plan, energy_bias)


def seed_plan(label: str, energy: float | None, *, slot: str = "", energy_bias: int = 0) -> Plan:
    """A mix from one song: stay around its energy and let sound similarity choose.

    No sound queries: the song itself is the description. The energy only gently
    waves around the seed's own level (time of day decides when the seed's energy
    is not known yet).
    """
    base = float(energy) if energy is not None else _SLOT_ENERGY.get(slot, 5.5)
    plan = Plan(
        vibe="",
        queries=[],
        arc="wave",
        base_energy=max(1.5, min(9.5, base)),
        span=0.8,
        note=f"Playing songs that sound like {label}." if label else "Playing songs that sound like your pick.",
        source="seed",
    )
    return apply_bias(plan, energy_bias)


def apply_bias(plan: Plan, energy_bias: int) -> Plan:
    """Shift a plan by the listener's energy up/down presses (each press = 1 point)."""
    plan.base_energy = max(1.5, min(9.5, plan.base_energy + energy_bias))
    return plan


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2-4 short descriptions of how the music should SOUND "
            "(instruments, texture, tempo, mood). No artist or track names.",
        },
        "avoid": {"type": "string", "description": "A sound to steer away from, or empty."},
        "arc": {"type": "string", "enum": list(ARCS)},
        "base_energy": {"type": "number", "description": "Centre energy of the set, 1 (still) to 10 (frantic)."},
        "span": {"type": "number", "description": "How far energy swings around the centre, 0.5 to 3."},
        "note": {"type": "string", "description": "One sentence telling the listener how you read the request."},
    },
    "required": ["queries", "avoid", "arc", "base_energy", "span", "note"],
    "additionalProperties": False,
}

_SYSTEM = """You plan DJ sets for a personal music library. You do not pick tracks.
An audio-embedding search finds the tracks from descriptions of SOUND, and a sequencer orders
them by measured tempo, key and energy. Your job is to translate the listener's request into:

- queries: 2-4 short phrases describing how the music should sound. The search model matches
  audio, so describe instruments, texture, tempo and mood ("warm acoustic guitar, slow, intimate").
  Never use artist names, track names, years or activities on their own ("gym" means nothing to
  it; "driving electronic beat, fast, aggressive" does). Make the phrases differ so the set has range.
- avoid: one phrase for sound that would break the mood, or an empty string.
- arc: flat (steady), build (rises), wind_down (falls), peak (rises then eases), wave (gentle swells).
- base_energy and span: where the set sits on a 1-10 energy scale and how far it moves.
- note: one plain sentence for the listener.

Respect the time of day and any energy adjustment given; they are in the request."""


class ClaudePlanner:
    """Optional vibe interpreter backed by the Anthropic API."""

    def __init__(self, api_key: str, model: str) -> None:
        self.model = model
        self._client = None
        if api_key:
            import anthropic  # imported lazily so the DJ runs without the package configured

            self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=45.0, max_retries=1)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def plan(self, vibe: str, *, slot: str, energy_bias: int, context: dict[str, Any]) -> Plan | None:
        """Ask Claude for a plan. Returns ``None`` on any failure (caller falls back)."""
        if not self._client or not vibe.strip():
            return None
        request = {"request": vibe, "time_of_day": slot, "energy_adjustment": energy_bias, **context}
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": PLAN_SCHEMA}}
        if "haiku-4-5" not in self.model:
            output_config["effort"] = "low"  # a short translation task; older Haiku rejects the field
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=4000,
                system=_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(request, ensure_ascii=False)}],
                extra_body={"output_config": output_config},
            )
            if response.stop_reason in ("refusal", "max_tokens"):
                _LOGGER.warning("Claude plan not usable (stop_reason=%s)", response.stop_reason)
                return None
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
        except Exception as err:  # noqa: BLE001 - any failure means "use the rules instead"
            _LOGGER.warning("Claude plan failed, using keyword rules: %s", err)
            return None
        queries = [q.strip() for q in data.get("queries") or [] if isinstance(q, str) and q.strip()][:4]
        if not queries:
            return None
        arc = data.get("arc") if data.get("arc") in ARCS else "wave"
        return Plan(
            vibe=vibe.strip(),
            queries=queries,
            avoid=str(data.get("avoid") or "").strip(),
            arc=arc,
            base_energy=max(1.5, min(9.5, float(data.get("base_energy") or 5.5))),
            span=max(0.5, min(3.0, float(data.get("span") or 2.0))),
            note=str(data.get("note") or "").strip(),
            source="claude",
        )
