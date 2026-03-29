"""Configuration route: edit addon settings."""

import json
import logging
import os

from flask import Blueprint, render_template, request, redirect, session
from ...state import AppState
from ...config import load_config, validate_config

logger = logging.getLogger(__name__)
bp = Blueprint("config_routes", __name__)

OPTIONS_PATH = "/data/options.json"


@bp.route("/")
def index():
    state = AppState()
    config = state.config
    raw = {}
    path = os.environ.get("ENERGIEHA_OPTIONS_PATH", OPTIONS_PATH)
    if os.path.exists(path):
        with open(path) as f:
            raw = json.load(f)

    # Flash message from save
    msg = request.args.get("msg", "")
    msg_type = request.args.get("msg_type", "info")

    return render_template("config.html",
                           config=config, raw=raw,
                           msg=msg, msg_type=msg_type,
                           active_page="config")


@bp.route("/save", methods=["POST"])
def save():
    """Save configuration changes with validation."""
    path = os.environ.get("ENERGIEHA_OPTIONS_PATH", OPTIONS_PATH)
    ingress = request.headers.get("X-Ingress-Path", "")

    try:
        existing = {}
        if os.path.exists(path):
            with open(path) as f:
                existing = json.load(f)

        form = request.form
        for key in form:
            val = form[key]
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
                # New field: try to auto-detect type
                try:
                    existing[key] = int(val)
                except ValueError:
                    try:
                        existing[key] = float(val)
                    except ValueError:
                        existing[key] = val

        # Handle checkbox fields (unchecked = not in form)
        for bool_key in ["dry_run", "direct_control", "phev_enabled", "sungrow_tou_enabled"]:
            if bool_key not in form:
                existing[bool_key] = False

        with open(path, "w") as f:
            json.dump(existing, f, indent=2)

        # Reload and validate
        new_config = load_config()
        if not validate_config(new_config):
            logger.warning("Config validation failed after save")
            return redirect(f"{ingress}/config?msg=Gespeichert+mit+Warnungen+(siehe+Logs)&msg_type=warning")

        state = AppState()
        state.config = new_config

        logger.info("Configuration saved and reloaded")
        return redirect(f"{ingress}/config?msg=Konfiguration+gespeichert&msg_type=success")

    except Exception as e:
        logger.error("Failed to save config: %s", e)
        return redirect(f"{ingress}/config?msg=Fehler:+{e}&msg_type=error")
