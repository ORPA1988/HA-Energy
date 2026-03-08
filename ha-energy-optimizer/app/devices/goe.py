"""go-e Charger integration — supports local HTTP API v2 and Cloud API."""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import get_config
from ha_client import get_ha_client
from models import CarState, GoeStatus

logger = logging.getLogger(__name__)


class GoeCharger:
    """
    Read and control a go-e Charger via:
    - Local HTTP API v2: http://<ip>/api/status and /api/set
    - Cloud API: https://api.go-e.io/api/status?token=<t>&serial=<s>
    """

    # go-e API v2 key mappings
    _KEY_CAR_STATE = "car"     # 0=no car, 1=charging, 2=done, 3=ready
    _KEY_CURRENT = "amp"       # amperes (6-32)
    _KEY_ENABLED = "alw"       # 0/1 allow_charging
    _KEY_POWER = "nrg"         # array [V_L1, V_L2, V_L3, V_N, A_L1, A_L2, A_L3, P_L1, P_L2, P_L3, P_N, P_total, ...]
    _KEY_SESSION_KWH = "dws"   # energy of current session in 10Wh units
    _KEY_PHASES = "psm"        # phase switch mode: 0=auto, 1=1-phase, 2=3-phase
    _KEY_TEMP = "tmp"          # temperature in 0.1°C
    _KEY_ERROR = "err"         # error code

    def __init__(self):
        cfg = get_config()
        self._enabled = cfg.goe_enabled
        self._conn_type = cfg.goe_connection_type
        self._local_ip = cfg.goe_local_ip
        self._cloud_serial = cfg.goe_cloud_serial
        self._cloud_token = cfg.goe_cloud_token
        self._max_current = cfg.goe_max_current_a
        self._phases = cfg.goe_phases
        self._ha = get_ha_client()
        self._last_status: Optional[GoeStatus] = None

    @property
    def available(self) -> bool:
        return self._enabled

    def _local_url(self, endpoint: str) -> str:
        return f"http://{self._local_ip}/api/{endpoint}"

    def _cloud_params(self) -> dict:
        return {"token": self._cloud_token, "serial": self._cloud_serial}

    async def get_status(self) -> Optional[GoeStatus]:
        """Fetch current status from go-e charger."""
        if not self._enabled:
            return None
        try:
            if self._conn_type == "local":
                return await self._get_status_local()
            else:
                return await self._get_status_cloud()
        except Exception as e:
            logger.warning("go-e status fetch failed: %s", e)
            return self._last_status

    async def _get_status_local(self) -> GoeStatus:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(self._local_url("status"))
            r.raise_for_status()
            data = r.json()
        return self._parse_status(data)

    async def _get_status_cloud(self) -> GoeStatus:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.go-e.io/api/status",
                params=self._cloud_params(),
            )
            r.raise_for_status()
            data = r.json()
        return self._parse_status(data)

    def _parse_status(self, data: dict) -> GoeStatus:
        """Parse go-e API v2 status response."""
        nrg = data.get(self._KEY_POWER, [0] * 15)
        power_w = nrg[11] * 10.0 if len(nrg) > 11 else 0.0  # index 11 = total power in 0.1W units

        # Session energy: dws is in 10Wh, convert to kWh
        session_kwh = data.get(self._KEY_SESSION_KWH, 0) / 100.0

        # Temperature in 0.1°C
        temp_raw = data.get(self._KEY_TEMP, 250)
        temperature = temp_raw / 10.0 if isinstance(temp_raw, (int, float)) else 25.0

        status = GoeStatus(
            car_state=CarState(data.get(self._KEY_CAR_STATE, 0)),
            current_a=int(data.get(self._KEY_CURRENT, 0)),
            power_w=power_w,
            energy_kwh_session=session_kwh,
            phases_active=self._detect_phases(nrg),
            temperature_c=temperature,
            error_code=data.get(self._KEY_ERROR, 0),
            enabled=bool(data.get(self._KEY_ENABLED, 0)),
            max_current_a=self._max_current,
            firmware_version=str(data.get("fwv", "")),
        )
        self._last_status = status
        return status

    def _detect_phases(self, nrg: list) -> int:
        """Count active phases by checking current on L1, L2, L3 (indices 4,5,6)."""
        if len(nrg) < 7:
            return 1
        active = sum(1 for i in range(4, 7) if nrg[i] > 5)  # >0.5A
        return max(1, active)

    async def set_current(self, current_a: int) -> bool:
        """Set charging current in Amperes (0 or min_current..max_current)."""
        if not self._enabled:
            return False
        current_a = max(0, min(current_a, self._max_current))
        try:
            if self._conn_type == "local":
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(
                        self._local_url("set"),
                        params={"amp": current_a},
                    )
                    r.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.go-e.io/api/set",
                        params={**self._cloud_params(), "amp": current_a},
                    )
                    r.raise_for_status()
            logger.debug("go-e: set current to %dA", current_a)
            return True
        except Exception as e:
            logger.warning("go-e set_current(%d) failed: %s", current_a, e)
            return False

    async def set_enabled(self, enabled: bool) -> bool:
        """Allow or disallow charging."""
        if not self._enabled:
            return False
        val = 1 if enabled else 0
        try:
            if self._conn_type == "local":
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(
                        self._local_url("set"),
                        params={"alw": val},
                    )
                    r.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.go-e.io/api/set",
                        params={**self._cloud_params(), "alw": val},
                    )
                    r.raise_for_status()
            logger.debug("go-e: set enabled=%s", enabled)
            return True
        except Exception as e:
            logger.warning("go-e set_enabled(%s) failed: %s", enabled, e)
            return False

    async def set_phase_mode(self, phases: int) -> bool:
        """
        Set phase mode: 1=force 1-phase, 2=force 3-phase, 0=auto.
        Only works on go-e HOME 22kW.
        """
        if not self._enabled:
            return False
        mode = min(2, max(0, phases))
        try:
            if self._conn_type == "local":
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(
                        self._local_url("set"),
                        params={"psm": mode},
                    )
                    r.raise_for_status()
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.get(
                        "https://api.go-e.io/api/set",
                        params={**self._cloud_params(), "psm": mode},
                    )
                    r.raise_for_status()
            logger.debug("go-e: set phase_mode=%d", mode)
            return True
        except Exception as e:
            logger.warning("go-e set_phase_mode(%d) failed: %s", mode, e)
            return False

    async def publish_to_ha(self, status: GoeStatus) -> None:
        """Publish go-e status to HA as virtual sensor entities."""
        ha = self._ha
        car_state_names = {0: "no_car", 1: "charging", 2: "done", 3: "ready"}
        await ha.publish_sensor("goe_power_w", round(status.power_w, 1), "W",
                                device_class="power")
        await ha.publish_sensor("goe_car_state", car_state_names.get(status.car_state.value, "unknown"),
                                "", {"numeric": status.car_state.value})
        await ha.publish_sensor("goe_session_kwh", round(status.energy_kwh_session, 3), "kWh",
                                device_class="energy")
        await ha.publish_sensor("goe_current_a", status.current_a, "A")
        await ha.publish_sensor("goe_temperature", round(status.temperature_c, 1), "°C",
                                device_class="temperature")
        await ha.publish_sensor("goe_phases_active", status.phases_active, "")
        await ha.publish_sensor("goe_enabled", int(status.enabled), "")


# Global singleton
_goe: Optional[GoeCharger] = None


def get_goe_charger() -> GoeCharger:
    global _goe
    if _goe is None:
        _goe = GoeCharger()
    return _goe
