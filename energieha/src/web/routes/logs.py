"""Logs route: real-time log viewer with SSE."""

import json
import logging
import time

from flask import Blueprint, render_template, Response
from ...state import AppState

bp = Blueprint("logs", __name__)
logger = logging.getLogger(__name__)


@bp.route("")
@bp.route("/")
def index():
    state = AppState()
    return render_template("logs.html",
                           cycles=state.get_cycle_history(50),
                           errors=state.get_error_log(20),
                           active_page="logs")


@bp.route("/stream")
def stream():
    """SSE endpoint for real-time log updates."""
    def generate():
        last_cycle = 0
        while True:
            state = AppState()
            current = state.cycle_count
            if current > last_cycle:
                last_cycle = current
                data = json.dumps(state.get_status_dict())
                yield f"data: {data}\n\n"
            time.sleep(5)

    return Response(generate(), mimetype="text/event-stream")
