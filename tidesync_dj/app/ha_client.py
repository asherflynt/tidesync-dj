"""Home Assistant Supervisor API client.

Inside an add-on the Supervisor injects a SUPERVISOR_TOKEN env var and the
Supervisor is reachable at http://supervisor/. The Core REST API is proxied at
http://supervisor/core/api/ when `homeassistant_api: true` is set in
config.yaml.

Docs: https://developers.home-assistant.io/docs/add-ons/communication
"""
from __future__ import annotations

import logging
import os

import httpx

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_BASE = "http://supervisor"
CORE_API = f"{SUPERVISOR_BASE}/core/api"


class HAClient:
    def __init__(self) -> None:
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._client = httpx.AsyncClient(timeout=10.0)

    @property
    def available(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def get_state(self, entity_id: str) -> dict | None:
        """Return the full state object for an entity, or None on failure."""
        if not self.available:
            return None
        try:
            resp = await self._client.get(
                f"{CORE_API}/states/{entity_id}", headers=self._headers()
            )
            if resp.status_code == 200:
                return resp.json()
            _LOGGER.debug("HA get_state %s -> %s", entity_id, resp.status_code)
        except httpx.HTTPError as err:
            _LOGGER.warning("HA get_state failed for %s: %s", entity_id, err)
        return None

    async def get_state_value(self, entity_id: str) -> str | None:
        state = await self.get_state(entity_id)
        return state.get("state") if state else None

    async def fire_event(self, event_type: str, data: dict | None = None) -> bool:
        """Fire a custom HA event so users can trigger automations."""
        if not self.available:
            return False
        try:
            resp = await self._client.post(
                f"{CORE_API}/events/{event_type}",
                headers=self._headers(),
                json=data or {},
            )
            return resp.status_code in (200, 201)
        except httpx.HTTPError as err:
            _LOGGER.warning("HA fire_event %s failed: %s", event_type, err)
            return False

    async def is_present(self, entity_id: str) -> bool | None:
        """Convenience for presence / binary sensors (on/off, home/not_home)."""
        value = await self.get_state_value(entity_id)
        if value is None:
            return None
        return value.lower() in ("on", "home", "true", "open")

    async def close(self) -> None:
        await self._client.aclose()
