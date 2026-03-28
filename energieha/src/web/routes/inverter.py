"""Inverter control route: TOU programs, battery, PHEV."""

import logging

from flask import Blueprint, render_template, request, redirect, jsonify
from ...state import AppState
from ...inverter_control import InverterController

logger = logging.getLogger(__name__)
bp = Blueprint("inverter", __name__)


@bp.route("/")
def index():
    state = AppState()
    config = state.config

    # Read inverter state if we have an HA client
    inverter_state = {}
    try:
        from ...ha_client import HaClient
        client = HaClient()
        controller = InverterController(client, config)
        inverter_state = controller.read_inverter_state()
    except Exception as e:
        logger.warning("Could not read inverter state: %s", e)

    return render_template("inverter.html",
                           inverter=inverter_state,
                           config=config,
                           active_page="inverter")


@bp.route("/tou/<int:program_num>", methods=["POST"])
def set_tou(program_num):
    """Set a single TOU program."""
    state = AppState()
    config = state.config

    try:
        from ...ha_client import HaClient
        client = HaClient()
        controller = InverterController(client, config)

        start_time = request.form.get("start_time", "00:00:00")
        mode = request.form.get("mode", "Disabled")
        soc_target = int(request.form.get("soc_target", 0))

        ok = controller.set_tou_program(program_num, start_time, "", mode, soc_target)
        if ok:
            logger.info("TOU program %d updated via GUI", program_num)
        else:
            logger.warning("TOU program %d update failed", program_num)
    except Exception as e:
        logger.error("Error setting TOU program: %s", e)

    ingress = request.headers.get("X-Ingress-Path", "")
    return redirect(f"{ingress}/inverter")


@bp.route("/phev", methods=["POST"])
def set_phev():
    """Set PHEV charge current."""
    state = AppState()
    config = state.config

    try:
        from ...ha_client import HaClient
        client = HaClient()
        controller = InverterController(client, config)

        amps = int(request.form.get("ampere", 0))
        controller.set_phev_charge_current(amps)
    except Exception as e:
        logger.error("Error setting PHEV: %s", e)

    ingress = request.headers.get("X-Ingress-Path", "")
    return redirect(f"{ingress}/inverter")
