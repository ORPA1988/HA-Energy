"""Home Assistant REST API client."""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# HA Supervisor injects these automatically for add-ons
DEFAULT_URL = "http://supervisor/core/api"
TIMEOUT = 10
MAX_RETRIES = 2
RETRY_DELAY = 5


class HaClient:
    """Lightweight REST client for Home Assistant Supervisor API."""

    def __init__(self, base_url: str = None, token: str = None):
        self._base_url = (base_url or os.environ.get("HA_URL", DEFAULT_URL)).rstrip("/")
        self._token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> dict | None:
        """Execute an HTTP request with retry logic."""
        url = f"{self._base_url}{path}"
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=TIMEOUT, **kwargs)
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return None
            except requests.exceptions.RequestException as e:
                if attempt < MAX_RETRIES:
                    logger.warning("HA request failed (attempt %d/%d): %s",
                                   attempt + 1, MAX_RETRIES + 1, e)
                    time.sleep(RETRY_DELAY)
                else:
                    logger.error("HA request failed after %d attempts: %s",
                                 MAX_RETRIES + 1, e)
                    return None  # Don't crash the main loop

    def get_state(self, entity_id: str) -> dict:
        """Get the state of a single entity.

        Returns dict with 'state', 'attributes', 'entity_id', etc.
        """
        return self._request("GET", f"/states/{entity_id}")

    def get_state_value(self, entity_id: str) -> float | None:
        """Get numeric state value of an entity. Returns None if unavailable."""
        data = self.get_state(entity_id)
        if not data:
            return None
        state = data.get("state", "")
        if state in ("unavailable", "unknown", ""):
            logger.warning("Entity %s is %s", entity_id, state)
            return None
        try:
            return float(state)
        except (ValueError, TypeError):
            logger.warning("Entity %s has non-numeric state: %s", entity_id, state)
            return None

    def get_attributes(self, entity_id: str) -> dict:
        """Get the attributes dict of an entity."""
        data = self.get_state(entity_id)
        if not data:
            return {}
        return data.get("attributes", {})

    def set_state(self, entity_id: str, state: str, attributes: dict = None):
        """Create or update an entity's state (for publishing plan sensors)."""
        payload = {"state": state}
        if attributes:
            payload["attributes"] = attributes
        self._request("POST", f"/states/{entity_id}", json=payload)

    def call_service(self, domain: str, service: str, data: dict = None):
        """Call a Home Assistant service."""
        payload = data or {}
        self._request("POST", f"/services/{domain}/{service}", json=payload)
        logger.debug("Called service %s.%s with %s", domain, service, payload)

    def get_ha_config(self) -> dict:
        """Get HA configuration (timezone, location, etc.)."""
        return self._request("GET", "/config") or {}

    def is_available(self) -> bool:
        """Check if the HA API is reachable."""
        try:
            self._request("GET", "/")
            return True
        except Exception:
            return False
