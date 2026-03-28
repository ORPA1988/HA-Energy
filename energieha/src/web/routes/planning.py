"""Planning route: 24h plan table and charts."""

from flask import Blueprint, render_template
from ...state import AppState

bp = Blueprint("planning", __name__)


@bp.route("/")
def index():
    state = AppState()
    plan = state.plan
    snapshot = state.snapshot

    # Build timeline data for template
    timeline = []
    if plan and plan.slots:
        cumulative_cost = 0.0
        for s in plan.slots[:96]:
            hours = s.duration_min / 60.0
            slot_cost = round(max(0, s.planned_grid_w) / 1000 * hours * s.price_eur_kwh, 4)
            cumulative_cost += slot_cost
            timeline.append({
                "time": s.start.strftime("%H:%M"),
                "mode": s.planned_battery_mode,
                "soc": round(s.projected_soc, 1),
                "battery_w": round(s.planned_battery_w),
                "pv_w": round(s.pv_forecast_w),
                "load_w": round(s.load_estimate_w),
                "grid_w": round(s.planned_grid_w),
                "price": round(s.price_eur_kwh, 4),
                "cost": round(slot_cost, 4),
                "total": round(cumulative_cost, 2),
            })

    return render_template("planning.html",
                           timeline=timeline,
                           plan=plan,
                           snapshot=snapshot,
                           active_page="planning")
