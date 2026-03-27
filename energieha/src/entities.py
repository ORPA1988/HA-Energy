"""Entity publisher: creates/updates HA sensor entities for plan visualization."""

import json
import logging

from .ha_client import HaClient
from .models import Config, Plan, Snapshot

logger = logging.getLogger(__name__)

PREFIX = "sensor.energieha"


class EntityPublisher:
    """Publishes plan data as HA sensor entities for dashboards."""

    def __init__(self, client: HaClient, config: Config):
        self._client = client
        self._config = config

    def publish(self, plan: Plan, snapshot: Snapshot) -> None:
        """Publish all plan-related entities to HA."""
        try:
            self._publish_status(plan)
            self._publish_battery_plan(plan)
            self._publish_soc_projection(plan, snapshot)
            self._publish_savings(plan)
        except Exception as e:
            logger.error("Failed to publish entities: %s", e)

    def _publish_status(self, plan: Plan) -> None:
        """Publish overall add-on status."""
        slot = plan.current_slot
        mode = slot.planned_battery_mode if slot else "idle"

        attrs = {
            "friendly_name": "EnergieHA Status",
            "strategy": plan.strategy,
            "last_plan_time": plan.created_at.isoformat(),
            "phev_active": slot.planned_phev_w > 0 if slot else False,
            "dry_run": self._config.dry_run,
            "icon": "mdi:battery-sync",
        }
        # Show strategy fallback error if present
        if hasattr(plan, "strategy_error") and plan.strategy_error:
            attrs["strategy_error"] = plan.strategy_error
        self._client.set_state(f"{PREFIX}_status", mode, attrs)

    def _publish_battery_plan(self, plan: Plan) -> None:
        """Publish current battery plan with timeline."""
        slot = plan.current_slot
        current_power = slot.planned_battery_w if slot else 0

        # Build timeline with all fields for dashboard planning table
        timeline = []
        cumulative_cost = 0.0
        for s in plan.slots[:96]:  # max 24h at 15min = 96 slots
            hours = s.duration_min / 60.0
            # Cost: only for grid import (positive grid_w)
            slot_cost = round(max(0, s.planned_grid_w) / 1000 * hours * s.price_eur_kwh, 4)
            cumulative_cost += slot_cost
            # Grid-load: how much the battery charges from grid (not PV)
            from .strategies.helpers import is_grid_charging
            surplus = s.pv_forecast_w - s.load_estimate_w
            if is_grid_charging(s.pv_forecast_w, s.load_estimate_w, s.planned_battery_w):
                gridload_w = round(s.planned_battery_w - max(0, surplus))
            else:
                gridload_w = 0

            timeline.append({
                "t": s.start.strftime("%H:%M"),
                "mode": s.planned_battery_mode,
                "soc": round(s.projected_soc, 1),
                "batt": round(s.planned_battery_w),
                "pv": round(s.pv_forecast_w),
                "load": round(s.load_estimate_w),
                "grid": round(s.planned_grid_w),
                "gridload": gridload_w,
                "price": round(s.price_eur_kwh, 4),
                "cost": slot_cost,
                "total": round(cumulative_cost, 2),
            })

        current_mode = slot.planned_battery_mode if slot else "idle"
        self._client.set_state(f"{PREFIX}_battery_plan", str(round(current_power)), {
            "friendly_name": "EnergieHA Battery Plan",
            "unit_of_measurement": "W",
            "device_class": "power",
            "state_class": "measurement",
            "plan": json.dumps(timeline),
            "current_mode": current_mode,
            "icon": "mdi:battery-charging-medium",
        })

    def _publish_soc_projection(self, plan: Plan, snapshot: Snapshot) -> None:
        """Publish projected SOC timeline."""
        final_soc = plan.slots[-1].projected_soc if plan.slots else snapshot.battery_soc

        soc_timeline = []
        for s in plan.slots[:96]:
            soc_timeline.append({
                "t": s.start.strftime("%H:%M"),
                "soc": round(s.projected_soc, 1),
            })

        self._client.set_state(f"{PREFIX}_planned_soc", str(round(final_soc, 1)), {
            "friendly_name": "EnergieHA Planned SOC",
            "unit_of_measurement": "%",
            "device_class": "battery",
            "current_soc": round(snapshot.battery_soc, 1),
            "soc_timeline": json.dumps(soc_timeline),
            "icon": "mdi:battery-outline",
        })

    def _publish_savings(self, plan: Plan) -> None:
        """Publish estimated cost savings.

        Compares planned cost against baseline (no battery, all deficit from grid).
        Eigenverbrauch = how much load is covered by PV + battery discharge.
        """
        grid_import_wh = 0.0
        grid_export_wh = 0.0
        cost_with_battery = 0.0
        cost_without_battery = 0.0
        battery_discharge_wh = 0.0

        for slot in plan.slots:
            hours = slot.duration_min / 60.0

            # With battery: total grid cost (import for load + battery charging)
            grid_w = slot.planned_grid_w
            if grid_w > 0:
                grid_import_wh += grid_w * hours
                cost_with_battery += (grid_w / 1000.0) * hours * slot.price_eur_kwh
            else:
                grid_export_wh += abs(grid_w) * hours

            # Track battery discharge (serves load)
            if slot.planned_battery_w < 0:
                battery_discharge_wh += abs(slot.planned_battery_w) * hours

            # Without battery: all load deficit from grid
            deficit_w = max(0, slot.load_estimate_w - slot.pv_forecast_w)
            cost_without_battery += (deficit_w / 1000.0) * hours * slot.price_eur_kwh

        savings = max(0, cost_without_battery - cost_with_battery)
        total_load_wh = sum(s.load_estimate_w * s.duration_min / 60.0 for s in plan.slots)
        total_pv_wh = sum(s.pv_forecast_w * s.duration_min / 60.0 for s in plan.slots)

        # Eigenverbrauch: PV used locally (not exported) + battery discharge
        pv_local_wh = min(total_pv_wh, total_load_wh)  # PV directly consumed
        load_covered_wh = pv_local_wh + battery_discharge_wh
        self_consumption = (min(100, load_covered_wh / total_load_wh * 100.0)
                            if total_load_wh > 0 else 0)

        self._client.set_state(f"{PREFIX}_savings", str(round(savings, 2)), {
            "friendly_name": "EnergieHA Estimated Savings",
            "unit_of_measurement": "EUR",
            "grid_import_kwh": round(grid_import_wh / 1000.0, 2),
            "grid_export_kwh": round(grid_export_wh / 1000.0, 2),
            "self_consumption_percent": round(self_consumption, 1),
            "cost_with_battery_eur": round(cost_with_battery, 2),
            "cost_without_battery_eur": round(cost_without_battery, 2),
            "icon": "mdi:piggy-bank-outline",
        })
