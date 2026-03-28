"""Dashboard route: system overview with power flow and status."""

from flask import Blueprint, render_template
from ...state import AppState

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    state = AppState()
    return render_template("dashboard.html",
                           status=state.get_status_dict(),
                           active_page="dashboard")
