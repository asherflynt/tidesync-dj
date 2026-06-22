"""Claude DJ brain.

Wraps the Anthropic Messages API and turns a structured context payload into a
queue decision. Design choices:

  * Official `anthropic` async SDK (AsyncAnthropic).
  * Structured outputs via `output_config.format` with a JSON schema, so the
    response is guaranteed to match our decision shape — no markdown fences to
    strip, no brittle parsing.
  * Adaptive thinking at `medium` effort: sequencing tracks for energy flow is
    "remotely complicated" reasoning, but the DJ ticks often, so medium keeps
    latency/cost in check.
  * Prompt caching: the frozen system prompt + the (semi-stable) taste profile
    form the cached prefix. Volatile per-tick context (history, queue, vibe,
    time of day) goes in the user turn so it never invalidates the cache.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError

_LOGGER = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are TideSync, an expert AI DJ with deep knowledge of music theory,
genre relationships, energy flow, and track sequencing. You control a
Tidal queue via Music Assistant.

Your job each cycle: given the active driver (a hand-picked seed, a vibe, or —
absent both — the listener's taste), plus the session's set plan, recent
history, and context, select the next block of tracks to queue. Think like a
world-class DJ programming a room — every transition deliberate.

PRIORITY — when signals conflict, obey them in this order (highest first):
1. HARD CONSTRAINTS — never queue anything in "blocked_tracks"; avoid any
   artist/track in "recent_skips" this session. Absolute.
2. SEED — if "seed_track" is set, build the station around it (its genre,
   energy, era). The seed outranks the taste profile.
3. VIBE — if "vibe_prompt" is set, it DOMINATES. First READ the vibe literally
   and fill "vibe_reading": restate what it is truly asking for — its intent,
   target energy and mood — then honor that direction. Take the words at face
   value: "chill / put me to sleep" means genuinely low, calming energy
   throughout (no upbeat singalongs); "high energy" means drive it. Songs or
   artists named in the vibe are MUST-PLAYS — include each somewhere in the set
   (a named artist ⇒ at least one of their tracks), placed naturally, not
   necessarily first.
4. TASTE — only when there is NO seed and NO vibe does taste lead. It is dynamic:
   lead with what they ask for and like right now ("moods_this_time_of_day",
   "moods_this_month", "likes_this_time_of_day", "likes_this_month"), set energy
   to suit "time_of_day", and use "taste_profile" as the backbone palette. The
   same person sounds different morning vs. night and across seasons.
5. HOUSEHOLD BASELINE — weakest fallback, only when the listener has no taste.

STAY DYNAMIC — honor the vibe's DIRECTION, but do NOT flatten the music. There
is no fixed familiar/discovery ratio and no banned categories: range widely,
surprise the listener, and keep the set breathing and varied WITHIN the
requested lane (a chill set still rises and falls gently; an energetic one has
peaks and breathers). Keep it fresh with new/unheard songs and artists, but
every pick — familiar or new — must serve the active driver (a kids-party vibe
never yields adult favourites, new or old).

FOLLOW THE SET PLAN — "set_plan" lays out the session's phases (an arc across
the night) and "arc_position" says where you are (elapsed time, % through,
current phase). Pick for the CURRENT phase and transition smoothly toward the
next; don't restart the arc every block.

CONTINUE THE ENERGY CURVE — "energy_arc" is the curve you have ACTUALLY built so
far: each recently played/queued track with the energy you assigned it (1=calmest,
10=peak) and, when known, its measured "bpm" and "camelot" key. This is ground
truth, not a guess. Read where the last few tracks left the energy (and tempo/key)
and make your first new pick flow from there — no jarring jump from where the
music actually is. Then ride the curve toward the current set-plan phase. Set each
track's "energy" (1-10) as its deliberate place on that arc; keep successive picks
moving smoothly (small steps, occasional intentional peak/breather) rather than
sawtoothing. Where "camelot"/"bpm" are present, prefer harmonically and tempo-
compatible neighbours for the smoothest blends, but never sacrifice the driver or
the arc just to match keys.

READ THE SIGNALS:
- "energy_bias" (negative = pull calmer, positive = push more energetic) shifts
  your target energy for this block.
- If "reroll" is true the listener pressed "try again" — they want a DIFFERENT
  direction than what just played in "recent_history": change genre/energy/feel
  meaningfully, don't repeat the last block's idea.
- Let "weather" and "outside_temp" subtly colour mood (cold rainy night ⇒
  cosier; hot bright afternoon ⇒ brighter), never overriding the driver.

CRAFT (programme like a $1M event DJ):
- Smooth tempo/energy transitions — no jarring jumps; rise, peak, breathe and
  resolve across the block and toward the next phase.
- Place peaks deliberately; don't blow the roof off too early. Strong opener and
  a satisfying closer.
- Drop singalongs / known moments where they land best.
- Vary artists — never back-to-back, no more than 2-3 times in the block.
- Avoid re-queuing tracks/artists prominent in "recent_history".
- If the listener or vibe implies kids/family, keep it clean — no explicit tracks.
- Each track query in "Artist - Track" form so it resolves in search.
- Fill "vibe_reading" (your read of the driver), set each track's "energy" (its
  place on the arc), give a brief "dj_note" on the arc and what drove your picks,
  and return exactly "tracks_to_add" tracks.
"""

# JSON schema for structured outputs. Must satisfy the structured-output
# constraints: every object sets additionalProperties:false and no unsupported
# numeric/length constraints are used (track count is enforced via the prompt).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vibe_reading": {
            "type": "object",
            "properties": {
                "energy_target": {"type": "integer"},
                "mood": {"type": "string"},
                "interpretation": {"type": "string"},
            },
            "required": ["energy_target", "mood", "interpretation"],
            "additionalProperties": False,
        },
        "next_tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "reason": {"type": "string"},
                    "energy": {"type": "integer"},
                },
                "required": ["query", "reason", "energy"],
                "additionalProperties": False,
            },
        },
        "mood_shift": {"type": "boolean"},
        "mood_shift_reason": {"type": ["string", "null"]},
        "dj_note": {"type": "string"},
    },
    "required": [
        "vibe_reading",
        "next_tracks",
        "mood_shift",
        "mood_shift_reason",
        "dj_note",
    ],
    "additionalProperties": False,
}

# Schema for the one-shot, session-level set plan (the long-form arc).
SET_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "target_energy": {"type": "integer"},
                    "mood": {"type": "string"},
                    "approx_minutes": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "target_energy", "mood", "approx_minutes", "notes"],
                "additionalProperties": False,
            },
        },
        "arc_note": {"type": "string"},
    },
    "required": ["phases", "arc_note"],
    "additionalProperties": False,
}


class NextTrack(BaseModel):
    query: str
    reason: str
    # The track's place on the energy arc (1 = calmest, 10 = peak). Persisted so
    # the next cycle sees the curve it actually built (see "energy_arc").
    energy: int = 5


class VibeReading(BaseModel):
    energy_target: int = 5
    mood: str = ""
    interpretation: str = ""


class DJDecision(BaseModel):
    vibe_reading: VibeReading = Field(default_factory=VibeReading)
    next_tracks: list[NextTrack] = Field(default_factory=list)
    mood_shift: bool = False
    mood_shift_reason: str | None = None
    dj_note: str = ""


class SetPhase(BaseModel):
    name: str = ""
    target_energy: int = 5
    mood: str = ""
    approx_minutes: int = 30
    notes: str = ""


class SetPlan(BaseModel):
    phases: list[SetPhase] = Field(default_factory=list)
    arc_note: str = ""


class ClaudeBrain:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model
        # Capability gating by model. `effort` and adaptive `thinking` are only
        # valid on Opus 4.x and Sonnet 4.6 — sending them to Haiku 4.5 returns a
        # 400. Structured outputs (output_config.format) work on all of these.
        m = model.lower()
        self._supports_effort = "opus" in m or "sonnet-4-6" in m
        self._supports_thinking = "opus" in m or "sonnet-4-6" in m

    def _extra_body(self, effort: str | None, json_schema: dict | None) -> dict:
        """Build the request body fields not in the SDK's typed signature.

        We pass `output_config` / `thinking` via extra_body so the request works
        regardless of the installed anthropic SDK version (older versions don't
        accept these as keyword arguments), and so we can gate them per model.
        """
        output_config: dict[str, Any] = {}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if effort and self._supports_effort:
            output_config["effort"] = effort

        extra: dict[str, Any] = {}
        if output_config:
            extra["output_config"] = output_config
        if self._supports_thinking:
            extra["thinking"] = {"type": "adaptive"}
        return extra

    def _system_blocks(self, taste_profile: str) -> list[dict[str, Any]]:
        """Stable cached prefix: core prompt + taste profile.

        cache_control on the last block caches the whole system prefix.
        """
        profile_text = taste_profile.strip() or "No taste profile yet."
        return [
            {"type": "text", "text": SYSTEM_PROMPT},
            {
                "type": "text",
                "text": f"=== LISTENER TASTE PROFILE ===\n{profile_text}",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    @staticmethod
    def _user_payload(context: dict[str, Any]) -> str:
        """Volatile per-tick context, kept out of the cached prefix."""
        # Trim the taste_profile out of the per-tick payload; it lives in system.
        payload = {k: v for k, v in context.items() if k != "taste_profile"}
        return (
            "Here is the current DJ context. Choose the next tracks.\n\n"
            + json.dumps(payload, indent=2, default=str)
        )

    async def decide(self, context: dict[str, Any]) -> DJDecision:
        """Run one decision cycle and return a validated DJDecision."""
        taste_profile = context.get("taste_profile", "")
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=self._system_blocks(taste_profile),
                messages=[{"role": "user", "content": self._user_payload(context)}],
                # Sequencing for energy/tempo/key flow is the hard reasoning here;
                # "high" buys better transitions. The DJ ticks every 30s+, so the
                # extra latency/cost is comfortably absorbed.
                extra_body=self._extra_body(effort="high", json_schema=DECISION_SCHEMA),
            )
        except anthropic.APIError as err:
            _LOGGER.error("Claude decision call failed: %s", err)
            raise

        if response.usage:
            _LOGGER.debug(
                "Claude tokens in=%s cache_read=%s out=%s",
                response.usage.input_tokens,
                getattr(response.usage, "cache_read_input_tokens", 0),
                response.usage.output_tokens,
            )

        text = next((b.text for b in response.content if b.type == "text"), "")
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> DJDecision:
        try:
            return DJDecision.model_validate_json(text)
        except ValidationError as err:
            _LOGGER.error("Could not validate DJ decision: %s\nraw=%s", err, text)
            return DJDecision(dj_note="(failed to parse Claude response)")

    async def plan_set(self, context: dict[str, Any]) -> SetPlan:
        """Plan the long-form arc of a session BEFORE picking tracks.

        Returns an ordered list of phases (with target energy/mood/minutes) that
        the per-tick `decide()` then fills, so the whole session has a coherent
        shape instead of 30-track islands. One-shot, low effort. Non-fatal:
        returns an empty plan on any error so the DJ still runs.
        """
        prompt = (
            "Plan the SHAPE of this listening session before any tracks are "
            "picked. Lay out an ordered set of phases forming a deliberate arc "
            "(e.g. ease-in -> build -> peak -> wind-down) that fits the driver, "
            "time of day and expected duration. Each phase: a name, target_energy "
            "(1-10), mood, approx_minutes, and notes. Honor the vibe's literal "
            "intent (a 'put me to sleep' vibe stays low-energy throughout; a party "
            "builds to a peak). Use 2-5 phases. Add a one-line arc_note.\n\n"
            "Session context:\n" + json.dumps(context, default=str)
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
                extra_body=self._extra_body(effort="low", json_schema=SET_PLAN_SCHEMA),
            )
        except anthropic.APIError as err:
            _LOGGER.error("Set plan call failed: %s", err)
            return SetPlan()
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return SetPlan.model_validate_json(text)
        except ValidationError as err:
            _LOGGER.error("Could not validate set plan: %s\nraw=%s", err, text)
            return SetPlan()

    async def summarize_taste(
        self, library_sample: list[dict[str, Any]], previous: str = ""
    ) -> str:
        """One-shot taste-profile summary (bootstrap or incremental update).

        Returns a compact prose summary the brain can carry in its system
        prefix. Plain text output — no structured schema needed here.
        """
        prompt = (
            "You are building a concise listener taste profile for an AI DJ.\n"
            "Summarize favourite artists, genres, energy preferences, and any "
            "patterns. Keep it under 200 words.\n\n"
        )
        if previous:
            prompt += f"Existing profile to refine:\n{previous}\n\n"
        prompt += "Listening data sample:\n" + json.dumps(
            library_sample[:200], default=str
        )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
                extra_body=self._extra_body(effort="low", json_schema=None),
            )
        except anthropic.APIError as err:
            _LOGGER.error("Taste summary failed: %s", err)
            return previous

        return next((b.text for b in response.content if b.type == "text"), previous)

    async def summarize_person_taste(
        self, signals: dict[str, Any], previous: str = ""
    ) -> str:
        """Refine ONE person's taste summary from their own tracked signals.

        Weights blocks above likes, and a repeated block (genuine dislike) far
        above a one-off (momentary fatigue). Captures time-of-day and monthly/
        seasonal patterns. Builds on `previous` so manually-seeded intent and
        prior learning are preserved rather than wiped. Returns `previous`
        unchanged on any API error, so learning is always non-fatal.
        """
        prompt = (
            "You maintain a single listener's taste profile for an AI DJ. Refine "
            "the profile from their tracked signals below.\n\n"
            "Weighting rules:\n"
            '- A BLOCK counts MORE than a like. A block with "disliked": true '
            "(blocked repeatedly) is a genuine dislike — record that track/artist "
            "as something to avoid. A single, low-count block just means they got "
            "tired of that track for a while, NOT that they dislike the artist or "
            "genre — do not mark it as avoid.\n"
            "- Likes are positive but weaker signals.\n"
            "- Note time-of-day and monthly/seasonal patterns (each signal has a "
            '"slot" and "month"), e.g. upbeat summer mornings, mellow December '
            "evenings.\n"
            "- Preserve and refine the existing profile; don't discard prior "
            "knowledge. Keep it under ~250 words of plain prose.\n\n"
        )
        if previous:
            prompt += f"Existing profile to refine:\n{previous}\n\n"
        prompt += "Tracked signals (likes, blocks, mood requests):\n" + json.dumps(
            signals, default=str
        )

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
                extra_body=self._extra_body(effort="low", json_schema=None),
            )
        except anthropic.APIError as err:
            _LOGGER.error("Person taste summary failed: %s", err)
            return previous

        return next((b.text for b in response.content if b.type == "text"), previous)
