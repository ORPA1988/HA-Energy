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
        hours = s.duration_min / 60.0
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
    # The planning loop checks this flag
    state._force_replan = True
    logger.info("Replan triggered via GUI")
    return jsonify({"status": "ok", "message": "Replan triggered"})
