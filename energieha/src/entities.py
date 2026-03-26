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

        # Build compact timeline for attributes (limit to avoid HA attribute size issues)
        timeline = []
        for s in plan.slots[:96]:  # max 24h at 15min = 96 slots
            timeline.append({
                "t": s.start.strftime("%H:%M"),
                "mode": s.planned_battery_mode,
                "w": round(s.planned_battery_w),
                "phev": round(s.planned_phev_w),
                "soc": round(s.projected_soc, 1),
                "p": round(s.price_eur_kwh, 4),
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

        Compares planned grid cost (with battery optimization) against
        a baseline without battery (all load from grid when PV < load).
        """
        grid_import_wh = 0.0
        grid_export_wh = 0.0
        cost_with_battery = 0.0
        cost_without_battery = 0.0
        pv_self_consumed_wh = 0.0

        for slot in plan.slots:
            hours = slot.duration_min / 60.0

            # With battery (planned)
            grid_w = slot.planned_grid_w
            if grid_w > 0:
                grid_import_wh += grid_w * hours
                cost_with_battery += (grid_w / 1000.0) * hours * slot.price_eur_kwh
            else:
                grid_export_wh += abs(grid_w) * hours

            # PV directly consumed by load (not exported, not stored)
            pv_to_load = min(slot.pv_forecast_w, slot.load_estimate_w)
            pv_self_consumed_wh += pv_to_load * hours

            # Without battery: all deficit from grid, surplus exported
            deficit_w = max(0, slot.load_estimate_w - slot.pv_forecast_w)
            cost_without_battery += (deficit_w / 1000.0) * hours * slot.price_eur_kwh

        savings = max(0, cost_without_battery - cost_with_battery)
        total_load_wh = sum(s.load_estimate_w * s.duration_min / 60.0 for s in plan.slots)
        # Self-consumption: % of load covered by PV + battery (not grid)
        load_from_grid = max(0, grid_import_wh - max(0, grid_import_wh - total_load_wh))
        self_consumption = ((total_load_wh - load_from_grid) / total_load_wh * 100.0
                            if total_load_wh > 0 else 0)
        self_consumption = max(0, min(100, self_consumption))

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
