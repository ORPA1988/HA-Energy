"""Load decomposition — subtract controllable loads from total consumption."""
from __future__ import annotations

import logging
from typing import Optional

from config import get_config
from ha_client import get_ha_client

logger = logging.getLogger(__name__)


class LoadDecomposer:
    """
    Decomposes total household power consumption into:
    - Base load (non-controllable: lights, fridge, standby, etc.)
    - Controllable loads (deferrable loads with known power sensors)

    Formula:
        base_load = total_power - sum(controllable_load_powers)

    This gives EMHASS / LP optimizer the "true" base load for forecasting,
    without the noise of controllable loads that the optimizer itself controls.
    """

    def __init__(self):
        self._cfg = get_config()
        self._ha = get_ha_client()

    async def get_base_load_w(self) -> float:
        """Calculate base load by subtracting known controllable loads from total."""
        total = await self._get_total_power()
        controllable = await self._get_controllable_power()
        base = max(0.0, total - controllable)
        return base

    async def get_decomposition(self) -> dict:
        """Full breakdown of power consumption."""
        total = await self._get_total_power()
        loads = await self._get_individual_loads()
        controllable_total = sum(loads.values())
        base_load = max(0.0, total - controllable_total)

        return {
            "total_power_w": round(total, 1),
            "base_load_w": round(base_load, 1),
            "controllable_total_w": round(controllable_total, 1),
            "loads": {name: round(w, 1) for name, w in loads.items()},
            "base_load_percent": round(base_load / total * 100, 1) if total > 0 else 100.0,
        }

    async def _get_total_power(self) -> float:
        """Read total house consumption from configured sensor."""
        sensor = self._cfg.total_power_sensor
        if not sensor:
            # Fallback: use grid + PV - battery as estimate
            grid = await self._ha.get_state_value(self._cfg.grid_power_sensor, 0.0)
            pv = await self._ha.get_state_value(self._cfg.pv_power_sensor, 0.0)
            bat = await self._ha.get_state_value(self._cfg.battery_power_sensor, 0.0)
            return max(0.0, grid + pv - max(0.0, bat))
        return await self._ha.get_state_value(sensor, 0.0)

    async def _get_controllable_power(self) -> float:
        """Sum of all controllable loads that should be subtracted."""
        total = 0.0
        for dl in self._cfg.deferrable_loads:
            if not dl.subtract_from_total:
                continue
            if dl.power_sensor:
                power = await self._ha.get_state_value(dl.power_sensor, 0.0)
                total += max(0.0, power)
            # If no sensor but load has a switch, check if it's on
            elif dl.switch:
                state = await self._ha.get_state(dl.switch)
                if state and state.get("state") == "on":
                    total += dl.power_w
        return total

    async def _get_individual_loads(self) -> dict[str, float]:
        """Get power reading for each controllable load."""
        loads = {}
        for dl in self._cfg.deferrable_loads:
            if not dl.subtract_from_total:
                continue
            if dl.power_sensor:
                power = await self._ha.get_state_value(dl.power_sensor, 0.0)
                loads[dl.name] = max(0.0, power)
            elif dl.switch:
                state = await self._ha.get_state(dl.switch)
                if state and state.get("state") == "on":
                    loads[dl.name] = float(dl.power_w)
                else:
                    loads[dl.name] = 0.0
        return loads


# Global singleton
_decomposer: Optional[LoadDecomposer] = None


def get_load_decomposer() -> LoadDecomposer:
    global _decomposer
    if _decomposer is None:
        _decomposer = LoadDecomposer()
    return _decomposer
