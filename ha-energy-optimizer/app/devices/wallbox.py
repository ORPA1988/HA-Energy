"""Abstract wallbox interface + generic HA entity-based wallbox implementation."""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ha_client import get_ha_client

logger = logging.getLogger(__name__)


class WallboxCarState(int, Enum):
    NONE = 0
    CHARGING = 1
    DONE = 2
    READY = 3


@dataclass
class WallboxStatus:
    """Unified wallbox status across all implementations."""
    car_state: WallboxCarState = WallboxCarState.NONE
    current_a: int = 0
    power_w: float = 0.0
    energy_kwh_session: float = 0.0
    phases_active: int = 1
    temperature_c: float = 0.0
    enabled: bool = False
    max_current_a: int = 16
    connected: bool = False


class WallboxInterface(abc.ABC):
    """Abstract interface for wallbox control."""

    @abc.abstractmethod
    async def get_status(self) -> Optional[WallboxStatus]:
        """Fetch current wallbox status."""

    @abc.abstractmethod
    async def set_current(self, current_a: int) -> bool:
        """Set charging current in Amperes."""

    @abc.abstractmethod
    async def set_enabled(self, enabled: bool) -> bool:
        """Enable or disable charging."""

    @property
    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this wallbox is configured and reachable."""


class HAEntityWallbox(WallboxInterface):
    """
    Generic wallbox controlled via Home Assistant entities.

    Works with any wallbox that exposes HA entities:
    - A switch entity to enable/disable charging
    - A sensor entity for current power (W)
    - An optional number entity for setting charge current (A)
    - An optional sensor for session energy (kWh)
    - An optional sensor for car connection state
    """

    def __init__(
        self,
        name: str,
        switch_entity: str,
        power_sensor: str = "",
        current_number: str = "",
        session_sensor: str = "",
        car_state_sensor: str = "",
        max_current_a: int = 16,
        phases: int = 1,
    ):
        self._name = name
        self._switch = switch_entity
        self._power_sensor = power_sensor
        self._current_number = current_number
        self._session_sensor = session_sensor
        self._car_state_sensor = car_state_sensor
        self._max_current = max_current_a
        self._phases = phases
        self._ha = get_ha_client()
        self._last_status: Optional[WallboxStatus] = None

    @property
    def available(self) -> bool:
        return bool(self._switch)

    async def get_status(self) -> Optional[WallboxStatus]:
        if not self._switch:
            return None

        try:
            # Read switch state
            switch_state = await self._ha.get_state(self._switch)
            enabled = switch_state and switch_state.get("state") == "on"

            # Read power
            power_w = 0.0
            if self._power_sensor:
                power_w = await self._ha.get_state_value(self._power_sensor, 0.0)

            # Read session energy
            session_kwh = 0.0
            if self._session_sensor:
                session_kwh = await self._ha.get_state_value(self._session_sensor, 0.0)

            # Determine car state
            car_state = WallboxCarState.NONE
            if self._car_state_sensor:
                state_data = await self._ha.get_state(self._car_state_sensor)
                if state_data:
                    raw = state_data.get("state", "").lower()
                    if raw in ("charging", "laden"):
                        car_state = WallboxCarState.CHARGING
                    elif raw in ("connected", "plugged", "ready", "verbunden", "bereit"):
                        car_state = WallboxCarState.READY
                    elif raw in ("complete", "done", "fertig"):
                        car_state = WallboxCarState.DONE
            elif power_w > 50:
                car_state = WallboxCarState.CHARGING
            elif enabled:
                car_state = WallboxCarState.READY

            # Estimate current from power
            current_a = int(power_w / (self._phases * 230)) if power_w > 0 else 0

            status = WallboxStatus(
                car_state=car_state,
                current_a=current_a,
                power_w=power_w,
                energy_kwh_session=session_kwh,
                phases_active=self._phases,
                enabled=enabled,
                max_current_a=self._max_current,
                connected=car_state != WallboxCarState.NONE,
            )
            self._last_status = status
            return status

        except Exception as e:
            logger.warning("HAEntityWallbox '%s' status failed: %s", self._name, e)
            return self._last_status

    async def set_current(self, current_a: int) -> bool:
        if not self._current_number:
            logger.debug("No current_number entity configured for '%s'", self._name)
            return False
        current_a = max(0, min(current_a, self._max_current))
        return await self._ha.set_number(self._current_number, float(current_a))

    async def set_enabled(self, enabled: bool) -> bool:
        if not self._switch:
            return False
        if enabled:
            return await self._ha.turn_on(self._switch)
        return await self._ha.turn_off(self._switch)

    async def publish_to_ha(self, status: WallboxStatus) -> None:
        """Publish wallbox status as HA sensors."""
        prefix = self._name.lower().replace(" ", "_").replace("-", "_")
        ha = self._ha
        state_names = {0: "no_car", 1: "charging", 2: "done", 3: "ready"}
        await ha.publish_sensor(f"{prefix}_power_w", round(status.power_w, 1), "W",
                                device_class="power")
        await ha.publish_sensor(f"{prefix}_car_state",
                                state_names.get(status.car_state.value, "unknown"), "")
        await ha.publish_sensor(f"{prefix}_current_a", status.current_a, "A")
        await ha.publish_sensor(f"{prefix}_session_kwh",
                                round(status.energy_kwh_session, 3), "kWh",
                                device_class="energy")


class OCPPWallbox(WallboxInterface):
    """
    OCPP-compatible wallbox via HA OCPP integration.

    Uses the standard HA entities created by the OCPP integration:
    - switch.{name}_charge_control
    - sensor.{name}_power
    - sensor.{name}_current
    - sensor.{name}_session_energy
    - sensor.{name}_status
    """

    def __init__(self, name: str, entity_prefix: str, max_current_a: int = 32, phases: int = 3):
        self._name = name
        self._prefix = entity_prefix
        self._max_current = max_current_a
        self._phases = phases
        self._ha = get_ha_client()
        self._last_status: Optional[WallboxStatus] = None

    @property
    def available(self) -> bool:
        return bool(self._prefix)

    async def get_status(self) -> Optional[WallboxStatus]:
        if not self._prefix:
            return None
        try:
            p = self._prefix
            power = await self._ha.get_state_value(f"sensor.{p}_power", 0.0)
            current = await self._ha.get_state_value(f"sensor.{p}_current", 0.0)
            session = await self._ha.get_state_value(f"sensor.{p}_session_energy", 0.0)

            switch_state = await self._ha.get_state(f"switch.{p}_charge_control")
            enabled = switch_state and switch_state.get("state") == "on"

            status_data = await self._ha.get_state(f"sensor.{p}_status")
            raw_status = (status_data.get("state", "") if status_data else "").lower()

            car_state = WallboxCarState.NONE
            if "charging" in raw_status:
                car_state = WallboxCarState.CHARGING
            elif "preparing" in raw_status or "suspended" in raw_status:
                car_state = WallboxCarState.READY
            elif "finishing" in raw_status:
                car_state = WallboxCarState.DONE

            status = WallboxStatus(
                car_state=car_state,
                current_a=int(current),
                power_w=power,
                energy_kwh_session=session,
                phases_active=self._phases,
                enabled=enabled,
                max_current_a=self._max_current,
                connected=car_state != WallboxCarState.NONE,
            )
            self._last_status = status
            return status
        except Exception as e:
            logger.warning("OCPPWallbox '%s' status failed: %s", self._name, e)
            return self._last_status

    async def set_current(self, current_a: int) -> bool:
        current_a = max(0, min(current_a, self._max_current))
        return await self._ha.set_number(
            f"number.{self._prefix}_max_current", float(current_a)
        )

    async def set_enabled(self, enabled: bool) -> bool:
        entity = f"switch.{self._prefix}_charge_control"
        if enabled:
            return await self._ha.turn_on(entity)
        return await self._ha.turn_off(entity)
