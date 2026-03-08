"""Home Assistant REST API client using Supervisor token."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from config import get_config

logger = logging.getLogger(__name__)


class HAClient:
    """Async client for the Home Assistant REST API via Supervisor."""

    def __init__(self):
        cfg = get_config()
        self._base = cfg.ha_url + "/api"
        self._headers = {
            "Authorization": f"Bearer {cfg.supervisor_token}",
            "Content-Type": "application/json",
        }
        self._client: Optional[httpx.AsyncClient] = None
        
        # Rate limiting: max 100 requests per minute to protect HA instance
        self._rate_limit_max = 100
        self._rate_limit_window = 60.0  # seconds
        self._request_times: list[float] = []

    async def _check_rate_limit(self) -> None:
        """Enforce rate limiting to protect HA from overload."""
        now = datetime.now().timestamp()
        
        # Remove requests older than window
        self._request_times = [t for t in self._request_times if now - t < self._rate_limit_window]
        
        # If at limit, wait until oldest request expires
        if len(self._request_times) >= self._rate_limit_max:
            oldest = self._request_times[0]
            sleep_time = self._rate_limit_window - (now - oldest) + 0.1
            if sleep_time > 0:
                logger.warning("Rate limit reached (%d req/%ds), sleeping %.1fs",
                             self._rate_limit_max, int(self._rate_limit_window), sleep_time)
                await asyncio.sleep(sleep_time)
                now = datetime.now().timestamp()
                self._request_times = [t for t in self._request_times if now - t < self._rate_limit_window]
        
        # Record this request
        self._request_times.append(now)

    async def start(self):
        self._client = httpx.AsyncClient(
            headers=self._headers,
            timeout=10.0,
        )
        logger.info("HA client started, base URL: %s", self._base)

    async def stop(self):
        if self._client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Read state
    # ------------------------------------------------------------------

    async def get_state(self, entity_id: str) -> Optional[dict]:
        """Returns the full state dict for an entity or None on error."""
        if not self._client:
            return None
        try:
            await self._check_rate_limit()
            r = await self._client.get(f"{self._base}/states/{entity_id}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.warning("get_state(%s) failed: %s", entity_id, e)
            return None

    async def get_state_value(self, entity_id: str, default: float = 0.0) -> float:
        """Returns the numeric state value of an entity."""
        data = await self.get_state(entity_id)
        if not data:
            return default
        try:
            return float(data["state"])
        except (KeyError, ValueError, TypeError):
            return default

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def call_service(self, domain: str, service: str,
                           data: dict[str, Any]) -> bool:
        if not self._client:
            return False
        try:
            await self._check_rate_limit()
            r = await self._client.post(
                f"{self._base}/services/{domain}/{service}",
                json=data,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("call_service(%s.%s) failed: %s", domain, service, e)
            return False

    async def turn_on(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_on", {"entity_id": entity_id})

    async def turn_off(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return await self.call_service(domain, "turn_off", {"entity_id": entity_id})

    async def set_number(self, entity_id: str, value: float) -> bool:
        return await self.call_service(
            "number", "set_value",
            {"entity_id": entity_id, "value": str(value)},
        )

    async def notify(self, target: str, title: str, message: str) -> bool:
        service = target.split(".")[-1] if "." in target else target
        return await self.call_service(
            "notify", service,
            {"title": title, "message": message},
        )

    # ------------------------------------------------------------------
    # Publish virtual sensors
    # ------------------------------------------------------------------

    async def publish_sensor(
        self,
        sensor_id: str,
        value: Any,
        unit: str = "",
        attributes: Optional[dict] = None,
        device_class: Optional[str] = None,
    ) -> bool:
        """
        Create/update a virtual sensor via the HA REST API states endpoint.
        Entity ID will be: sensor.ha_energy_<sensor_id>
        """
        entity_id = f"sensor.ha_energy_{sensor_id}"
        payload: dict[str, Any] = {
            "state": str(value),
            "attributes": {
                "unit_of_measurement": unit,
                "friendly_name": sensor_id.replace("_", " ").title(),
                "source": "ha_energy_optimizer",
                **(attributes or {}),
            },
        }
        if device_class:
            payload["attributes"]["device_class"] = device_class

        if not self._client:
            return False
        try:
            await self._check_rate_limit()
            r = await self._client.post(
                f"{self._base}/states/{entity_id}",
                json=payload,
            )
            r.raise_for_status()
            return True
        except Exception as e:
            logger.warning("publish_sensor(%s) failed: %s", entity_id, e)
            return False

    async def publish_all_sensors(self, state_data: dict[str, Any]) -> None:
        """Publish a batch of sensors at once."""
        for sensor_id, (value, unit, attrs) in state_data.items():
            await self.publish_sensor(sensor_id, value, unit, attrs)


# Global singleton
_ha_client: Optional[HAClient] = None


def get_ha_client() -> HAClient:
    global _ha_client
    if _ha_client is None:
        _ha_client = HAClient()
    return _ha_client
