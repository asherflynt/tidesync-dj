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

Your job each cycle: given the listener's taste profile, recent history,
current vibe, and session context, select the next 30 tracks to queue.
You are filling a substantial block of the session — think like a DJ
planning a full set, with a deliberate arc from start to finish.

Rules:
- Build an energy arc across the full 30-track block — don't just repeat the
  same tempo/mood; rise, peak, breathe, and resolve over the set
- Respect skip signals hard — a skipped artist/track means avoid for this session
- Balance ~70% familiar/loved artists with ~30% discovery picks that fit the vibe
- Time-of-day matters: mornings get energy builds, late night gets depth and space
- Vary artists — don't play the same artist back-to-back or more than 2-3 times
  in the 30-track block
- Always provide a brief "dj_note" explaining your overall arc for this block
- Each track query must be in "Artist - Track" form so it resolves cleanly in search
- Return exactly 30 tracks in next_tracks
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
