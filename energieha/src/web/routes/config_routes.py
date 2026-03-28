"""Configuration route: edit addon settings."""

import json
import logging
import os

from flask import Blueprint, render_template, request, redirect, url_for
from ...state import AppState
from ...config import load_config

logger = logging.getLogger(__name__)
bp = Blueprint("config_routes", __name__)

OPTIONS_PATH = "/data/options.json"


@bp.route("/")
def index():
    state = AppState()
    config = state.config
    # Read raw options for form population
    raw = {}
    path = os.environ.get("ENERGIEHA_OPTIONS_PATH", OPTIONS_PATH)
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)
    return render_template("config.html",
                           config=config, raw=raw,
                           active_page="config")


@bp.route("/save", methods=["POST"])
def save():
    """Save configuration changes."""
    path = os.environ.get("ENERGIEHA_OPTIONS_PATH", OPTIONS_PATH)
    try:
        # Read existing config
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)

        # Update with form values
        form = request.form
        for key in form:
            val = form[key]
            # Type conversion based on existing value type
            if key in existing:
                if isinstance(existing[key], bool):
                    existing[key] = val.lower() in ("true", "1", "on", "yes")
                elif isinstance(existing[key], int):
                    existing[key] = int(val)
                elif isinstance(existing[key], float):
                    existing[key] = float(val)
                else:
                    existing[key] = val
            else:
                existing[key] = val

        # Handle checkbox fields (unchecked = not in form)
        for bool_key in ["dry_run", "direct_control", "phev_enabled", "sungrow_tou_enabled"]:
            if bool_key not in form:
                existing[bool_key] = False

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        # Reload config in the planning loop
        state = AppState()
        state.config = load_config()

        logger.info("Configuration saved and reloaded")
    except Exception as e:
        logger.error("Failed to save config: %s", e)

    ingress = request.headers.get("X-Ingress-Path", "")
    return redirect(f"{ingress}/config")
