"""Coordinator — merges realtime, LP, and genetic plan outputs into final actions."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import get_config
from ha_client import get_ha_client
from models import Actions, DailySchedule, EnergyState, EVStrategyType, LongTermPlan

logger = logging.getLogger(__name__)


class Coordinator:
    """
    Priority hierarchy for control decisions:
    1. Realtime controller handles EV current moment-to-moment (solar surplus)
    2. LP schedule determines optimal EV current for each hour and load on/off
    3. Genetic plan informs battery reserve and overnight EV strategy

    This class reads the current LP slot and genetic plan to:
    - Set the 'smart_current_a' in the realtime controller
    - Turn deferrable loads on/off per schedule
    - Prevent battery discharge below reserve (if EV charging planned)
    - Execute peak shaving if configured
    """

    def __init__(self):
        self._cfg = get_config()
        self._ha = get_ha_client()
        self._last_actions: Optional[Actions] = None

    def get_actions(
        self,
        state: EnergyState,
        lp_schedule: Optional[DailySchedule],
        long_term: Optional[LongTermPlan],
    ) -> Actions:
        cfg = self._cfg

        # Default actions
        ev_current_a = 0
        ev_enabled = False
        battery_charge_limit = float(cfg.battery_max_charge_w)
        battery_discharge_limit = float(cfg.battery_max_discharge_w)
        deferrable_loads: dict[str, bool] = {dl.switch: False for dl in cfg.deferrable_loads}
        savings_eur = 0.0
        active_strategy: Optional[EVStrategyType] = None

        now = datetime.now().replace(minute=0, second=0, microsecond=0)

        # Get current LP schedule slot
        current_slot = None
        if lp_schedule:
            for slot in lp_schedule.slots:
                if slot.hour == now:
                    current_slot = slot
                    break

        if current_slot:
            ev_current_a = current_slot.ev_current_a
            ev_enabled = current_slot.ev_charge_w > 0
            deferrable_loads = current_slot.deferrable_loads
            savings_eur = lp_schedule.estimated_savings_eur if lp_schedule else 0.0
            logger.info("LP schedule active: EV %dA (%.0fW), Loads: %s, Savings: €%.3f",
                       ev_current_a, current_slot.ev_charge_w,
                       {k: v for k, v in deferrable_loads.items() if v}, savings_eur)

        # Apply genetic plan: restrict battery discharge if EV charging planned
        if long_term:
            # Check if genetic plan recommends keeping battery charged for tonight
            battery_charge_limit = float(cfg.battery_max_charge_w)
            # Reserve SOC: don't discharge below reserve when EV charging is coming
            next_24h_slots = [s for s in long_term.slots if s.hour >= now][:24]
            ev_planned_wh = sum(s.ev_charge_w for s in next_24h_slots)
            if ev_planned_wh > 0 and state.battery_soc_percent <= long_term.battery_reserve_soc:
                # Prevent discharge below reserve when EV charge planned
                battery_discharge_limit = 0.0
                logger.debug("Battery discharge blocked: reserving for planned EV charging")

        # Peak shaving: if grid import > limit, discharge battery
        if cfg.peak_shaving_limit_w > 0 and state.grid_power_w > cfg.peak_shaving_limit_w:
            overshoot = state.grid_power_w - cfg.peak_shaving_limit_w
            logger.info("Peak shaving: grid %.0fW > limit %.0fW, activating discharge",
                        state.grid_power_w, cfg.peak_shaving_limit_w)
            battery_discharge_limit = min(float(cfg.battery_max_discharge_w), overshoot * 1.1)

        actions = Actions(
            ev_charge_current_a=ev_current_a,
            ev_enabled=ev_enabled,
            battery_charge_limit_w=battery_charge_limit,
            battery_discharge_limit_w=battery_discharge_limit,
            deferrable_loads_state=deferrable_loads,
            estimated_savings_eur=savings_eur,
            active_strategy=active_strategy,
        )
        self._last_actions = actions
        return actions

    async def apply_load_actions(self, actions: Actions) -> None:
        """Turn deferrable loads on/off as scheduled. Skipped in read-only mode."""
        if self._cfg.read_only:
            logger.info("[READ-ONLY] Skipping load actions: %s",
                       {k: v for k, v in actions.deferrable_loads_state.items() if v})
            return
        for switch_entity, should_be_on in actions.deferrable_loads_state.items():
            if should_be_on:
                await self._ha.turn_on(switch_entity)
            else:
                await self._ha.turn_off(switch_entity)

    async def publish_summary(self, state: EnergyState, actions: Actions,
                              lp: Optional[DailySchedule]) -> None:
        """Publish key coordinator metrics to HA."""
        ha = self._ha
        await ha.publish_sensor("pv_power_w", round(state.pv_power_w, 0), "W",
                                device_class="power")
        await ha.publish_sensor("surplus_w", round(state.surplus_w, 0), "W")
        await ha.publish_sensor("battery_soc", round(state.battery_soc_percent, 1), "%",
                                device_class="battery")
        await ha.publish_sensor("grid_power_w", round(state.grid_power_w, 0), "W",
                                device_class="power")
        await ha.publish_sensor("price_raw_ct", round(state.price_raw_ct_kwh, 2), "ct/kWh")
        await ha.publish_sensor("price_total_ct", round(state.price_total_ct_kwh, 2), "ct/kWh")
        await ha.publish_sensor("savings_today", round(actions.estimated_savings_eur, 3), "EUR")
        await ha.publish_sensor("balancing_status", state.balancing_status.value, "")

        if lp:
            import json
            schedule_json = {
                "slots": [
                    {
                        "hour": s.hour.isoformat(),
                        "battery_charge_w": round(s.battery_charge_w, 0),
                        "battery_discharge_w": round(s.battery_discharge_w, 0),
                        "ev_charge_w": round(s.ev_charge_w, 0),
                        "grid_import_w": round(s.grid_import_w, 0),
                        "grid_export_w": round(s.grid_export_w, 0),
                        "battery_soc_end": round(s.battery_soc_end, 1),
                        "cost_eur": round(s.cost_eur, 4),
                        "price_ct": round(s.price_ct, 2),
                    }
                    for s in lp.slots
                ],
                "total_cost_eur": round(lp.total_cost_eur, 3),
            }
            await ha.publish_sensor("schedule_json", json.dumps(schedule_json), "")


# Global singleton
_coordinator: Optional[Coordinator] = None


def get_coordinator() -> Coordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = Coordinator()
    return _coordinator
