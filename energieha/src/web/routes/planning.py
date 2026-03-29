"""Planning route: 24h plan table and charts."""

from flask import Blueprint, render_template
from ...state import AppState

bp = Blueprint("planning", __name__)


@bp.route("")
def index():
    state = AppState()
    plan = state.plan
    snapshot = state.snapshot
    config = state.config

    # Build timeline data for template
    timeline = []
    if plan and plan.slots:
        cumulative_cost = 0.0
        for s in plan.slots[:96]:
            hours = s.duration_min / 60.0
            grid_import = max(0, s.planned_grid_w)
            slot_cost = round(grid_import / 1000 * hours * s.price_eur_kwh, 4)
            cumulative_cost += slot_cost
            # Determine if this is grid-charging
            surplus = s.pv_forecast_w - s.load_estimate_w
            is_grid = (s.planned_battery_w > 50 and s.planned_battery_w > max(0, surplus))
            timeline.append({
                "time": s.start.strftime("%H:%M"),
                "mode": s.planned_battery_mode,
                "soc": round(s.projected_soc, 1),
                "battery_w": round(s.planned_battery_w),
                "pv_w": round(s.pv_forecast_w),
                "load_w": round(s.load_estimate_w),
                "grid_w": round(s.planned_grid_w),
                "grid_charge": is_grid,
                "price": round(s.price_eur_kwh, 4),
                "cost": round(slot_cost, 4),
                "total": round(cumulative_cost, 2),
            })

    # TOU programs from state (via sungrow_tou reason)
    tou_reason = ""
    if hasattr(state, '_plan') and state._plan:
        # Try to get from status entity attributes
        pass

    # Get price threshold
    threshold = 0.0
    if snapshot and snapshot.dynamic_price_threshold > 0:
        threshold = snapshot.dynamic_price_threshold
    elif config:
        threshold = config.price_threshold_eur

    return render_template("planning.html",
                           timeline=timeline,
                           plan=plan,
                           snapshot=snapshot,
                           config=config,
                           threshold=threshold,
                           active_page="planning")
