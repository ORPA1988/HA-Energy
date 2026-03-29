"""JSON API routes for HTMX and programmatic access."""

import json
import logging

from flask import Blueprint, jsonify, request
from ...state import AppState

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__)


@bp.route("/state")
def get_state():
    """Return current system state as JSON."""
    state = AppState()
    return jsonify(state.get_status_dict())


@bp.route("/plan")
def get_plan():
    """Return current plan timeline as JSON."""
    state = AppState()
    plan = state.plan
    if not plan:
        return jsonify({"slots": [], "strategy": "none"})

    slots = []
    for s in plan.slots[:96]:
        slots.append({
            "time": s.start.strftime("%H:%M"),
            "mode": s.planned_battery_mode,
            "soc": round(s.projected_soc, 1),
            "battery_w": round(s.planned_battery_w),
            "pv_w": round(s.pv_forecast_w),
            "load_w": round(s.load_estimate_w),
            "grid_w": round(s.planned_grid_w),
            "price": round(s.price_eur_kwh, 4),
        })

    return jsonify({
        "strategy": plan.strategy,
        "created_at": plan.created_at.isoformat(),
        "slots": slots,
    })


@bp.route("/prices")
def get_prices():
    """Return EPEX price data for charts with planned charge hours."""
    state = AppState()
    prices = state.prices
    threshold = 0.0
    snap = state.snapshot
    if snap:
        threshold = snap.dynamic_price_threshold
    config = state.config
    if threshold <= 0 and config:
        threshold = config.price_threshold_eur

    # Get planned grid-charge time ranges from the plan (ISO timestamps)
    charge_ranges = []
    plan = state.plan
    if plan and plan.slots:
        from ...strategies.helpers import is_grid_charging
        for s in plan.slots:
            if (s.planned_battery_mode == "charge" and s.planned_battery_w > 50
                    and is_grid_charging(s.pv_forecast_w, s.load_estimate_w, s.planned_battery_w)):
                charge_ranges.append({
                    "start": s.start.isoformat(),
                    "end": (s.start + __import__('datetime').timedelta(minutes=s.duration_min)).isoformat(),
                })

    return jsonify({
        "prices": prices,
        "threshold": round(threshold, 4),
        "charge_ranges": charge_ranges,
        "count": len(prices),
    })


@bp.route("/forecast")
def get_forecast():
    """Return PV forecast data with confidence bands."""
    state = AppState()
    return jsonify({
        "forecast": state.pv_forecast,
        "count": len(state.pv_forecast),
    })


@bp.route("/savings")
def get_savings():
    """Return savings summary."""
    state = AppState()
    return jsonify(state.savings or {})


@bp.route("/cycles")
def get_cycles():
    """Return recent cycle history."""
    state = AppState()
    cycles = state.get_cycle_history(50)
    return jsonify([{
        "timestamp": c.timestamp.isoformat(),
        "strategy": c.strategy,
        "battery_soc": c.battery_soc,
        "battery_mode": c.battery_mode,
        "pv_power_w": c.pv_power_w,
        "grid_power_w": c.grid_power_w,
        "load_power_w": c.load_power_w,
        "error": c.error,
    } for c in cycles])


@bp.route("/errors")
def get_errors():
    """Return recent errors."""
    state = AppState()
    return jsonify(state.get_error_log(20))


@bp.route("/replan", methods=["POST"])
def trigger_replan():
    """Trigger an immediate planning cycle."""
    state = AppState()
    state._force_replan = True
    logger.info("Replan triggered via GUI")
    return jsonify({"status": "ok", "message": "Replan triggered"})


@bp.route("/inverter/reset-tou", methods=["POST"])
def reset_tou():
    """Reset all TOU programs to Disabled."""
    state = AppState()
    config = state.config
    if not config or config.dry_run:
        return jsonify({"status": "dry_run", "message": "Dry run - no changes"})
    try:
        from ...ha_client import HaClient
        from ...inverter_control import InverterController
        ha = HaClient()
        ctrl = InverterController(ha, config)
        for i in range(1, 7):
            ctrl.set_tou_program(i, "00:00:00", "", "Disabled", 0)
        return jsonify({"status": "ok", "message": "Alle TOU Programme auf Disabled gesetzt"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@bp.route("/inverter/emergency-idle", methods=["POST"])
def emergency_idle():
    """Emergency: set all to idle and force replan."""
    state = AppState()
    config = state.config
    if not config or config.dry_run:
        return jsonify({"status": "dry_run", "message": "Dry run - no changes"})
    try:
        from ...ha_client import HaClient
        from ...inverter_control import InverterController
        ha = HaClient()
        ctrl = InverterController(ha, config)
        for i in range(1, 7):
            ctrl.set_tou_program(i, "00:00:00", "", "Disabled", 0)
        state._force_replan = True
        return jsonify({"status": "ok", "message": "Notfall-Idle: Alle Programme Disabled + Replan"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
