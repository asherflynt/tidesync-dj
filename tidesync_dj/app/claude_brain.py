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
absent both — the listener's taste), plus recent history and session context,
select the next block of tracks to queue. Think like a DJ planning a full set,
with a deliberate arc from start to finish.

PRIORITY — when signals conflict, obey them in this order (highest first):
1. HARD CONSTRAINTS — never queue anything in "blocked_tracks", and avoid any
   artist/track in "recent_skips" for this session. These are absolute.
2. SEED — if "seed_track" is set, build the station around it: match its genre,
   energy and era. The seed outranks the taste profile entirely.
3. VIBE — if "vibe_prompt" is set, it DOMINATES genre, energy and era. When the
   vibe conflicts with the taste profile, follow the VIBE, not the taste
   ("kids dance party" outranks an adult indie taste profile). Any specific
   songs or artists named in the vibe are MUST-PLAYS: include each one somewhere
   in the set (for a named artist, at least one of their tracks) — placed
   naturally within the arc, NOT necessarily first.
4. TASTE — only when there is NO seed and NO vibe does the listener's taste lead.
   Treat it as dynamic, not a static dump: lead with what they typically ask for
   and like right now (see "moods_this_time_of_day", "moods_this_month",
   "likes_this_time_of_day", "likes_this_month"), set energy to suit
   "time_of_day", and use the "taste_profile" summary as the backbone palette.
   The same person should sound different morning vs. late night, and across
   months/seasons.
5. HOUSEHOLD BASELINE — weakest fallback, used only when the listener has no
   taste of their own.

MATCH THE DRIVER, ALWAYS — there is no fixed familiar/discovery ratio. EVERY
track, familiar or new, must fit whatever is driving this set (the vibe when
set, otherwise the active person's taste). Keep the set fresh by interjecting
new and unheard songs and artists (aim for roughly a third, as a feel — not a
rule), but those discovery picks must ALSO match the active vibe/profile — never
fall back to off-vibe "familiar" favourites just because they're known (a
kids-party vibe must never yield adult favourites, new or old).

Also:
- Build an energy arc across the full block — rise, peak, breathe, and resolve;
  don't just repeat one tempo/mood.
- Vary artists — no same artist back-to-back, and no more than 2-3 times in the
  block.
- Avoid re-queuing tracks/artists prominent in "recent_history" so repeated
  sessions don't sound identical.
- Each track query must be in "Artist - Track" form so it resolves in search.
- Always provide a brief "dj_note" explaining the arc and what drove your picks.
- Return exactly "tracks_to_add" tracks (currently 30) in next_tracks.
"""

# JSON schema for structured outputs. Must satisfy the structured-output
# constraints: every object sets additionalProperties:false and no unsupported
# numeric/length constraints are used (track count is enforced via the prompt).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "next_tracks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["query", "reason"],
                "additionalProperties": False,
            },
        },
        "mood_shift": {"type": "boolean"},
        "mood_shift_reason": {"type": ["string", "null"]},
        "dj_note": {"type": "string"},
    },
    "required": ["next_tracks", "mood_shift", "mood_shift_reason", "dj_note"],
    "additionalProperties": False,
}


class NextTrack(BaseModel):
    query: str
    reason: str


class DJDecision(BaseModel):
    next_tracks: list[NextTrack] = Field(default_factory=list)
    mood_shift: bool = False
    mood_shift_reason: str | None = None
    dj_note: str = ""


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
                extra_body=self._extra_body(effort="medium", json_schema=DECISION_SCHEMA),
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
