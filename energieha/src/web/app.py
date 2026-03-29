"""Flask application with HA ingress support - all routes inline.

IMPORTANT: Every route must be wrapped in try/except to prevent Flask from
crashing. A single unhandled exception kills the web server permanently
while the planning loop continues running in the background.
"""

import json
import logging
import os
import time as _time
import traceback

from flask import Flask, request, render_template, redirect, jsonify, Response

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.url_map.strict_slashes = False

    from ..state import AppState
    from .. import __version__

    # ---- Global error handlers ----
    @app.errorhandler(404)
    def _404(e):
        logger.warning("404: %s %s", request.method, request.path)
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found", "path": request.path}), 404
        return f"<h1>404 - Seite nicht gefunden</h1><p>Pfad: {request.path}</p>", 404

    @app.errorhandler(Exception)
    def _500(e):
        logger.error("500: %s %s → %s", request.method, request.path, e, exc_info=True)
        if request.path.startswith("/api/"):
            return jsonify({"error": str(e)}), 500
        return f"<h1>500 - Interner Fehler</h1><pre>{traceback.format_exc()}</pre>", 500

    @app.before_request
    def _ingress():
        request.ingress_path = request.headers.get("X-Ingress-Path", "")

    @app.context_processor
    def _ctx():
        return {"ingress_path": getattr(request, "ingress_path", ""), "version": __version__}

    # ================================================================
    # PAGE ROUTES - each wrapped in try/except
    # ================================================================

    @app.route("/")
    def dashboard():
        try:
            state = AppState()
            return render_template("dashboard.html", status=state.get_status_dict(), active_page="dashboard")
        except Exception as e:
            logger.error("Dashboard error: %s", e, exc_info=True)
            return render_template("dashboard.html", status={}, active_page="dashboard")

    @app.route("/planning")
    def planning():
        try:
            state = AppState()
            plan = state.plan
            snapshot = state.snapshot
            config = state.config
            timeline = []
            if plan and plan.slots:
                cumulative = 0.0
                for s in plan.slots[:96]:
                    h = s.duration_min / 60.0
                    gi = max(0, s.planned_grid_w)
                    cost = round(gi / 1000 * h * s.price_eur_kwh, 4)
                    cumulative += cost
                    surplus = s.pv_forecast_w - s.load_estimate_w
                    is_grid = s.planned_battery_w > 50 and s.planned_battery_w > max(0, surplus)
                    timeline.append({"time": s.start.strftime("%H:%M"), "mode": s.planned_battery_mode,
                        "soc": round(s.projected_soc, 1), "battery_w": round(s.planned_battery_w),
                        "pv_w": round(s.pv_forecast_w), "load_w": round(s.load_estimate_w),
                        "grid_w": round(s.planned_grid_w), "grid_charge": is_grid,
                        "price": round(s.price_eur_kwh, 4), "cost": round(cost, 4), "total": round(cumulative, 2)})
            threshold = 0.0
            if snapshot and snapshot.dynamic_price_threshold > 0:
                threshold = snapshot.dynamic_price_threshold
            elif config:
                threshold = config.price_threshold_eur
            return render_template("planning.html", timeline=timeline, plan=plan,
                                   snapshot=snapshot, config=config, threshold=threshold, active_page="planning")
        except Exception as e:
            logger.error("Planning error: %s", e, exc_info=True)
            return render_template("planning.html", timeline=[], plan=None,
                                   snapshot=None, config=None, threshold=0, active_page="planning")

    @app.route("/config")
    def config_page():
        try:
            state = AppState()
            raw = {}
            path = os.environ.get("ENERGIEHA_OPTIONS_PATH", "/data/options.json")
            if os.path.exists(path):
                with open(path) as f:
                    raw = json.load(f)
            msg = request.args.get("msg", "")
            msg_type = request.args.get("msg_type", "info")
            return render_template("config.html", config=state.config, raw=raw,
                                   msg=msg, msg_type=msg_type, active_page="config")
        except Exception as e:
            logger.error("Config error: %s", e, exc_info=True)
            return f"<h1>Config Fehler</h1><pre>{e}</pre>", 500

    @app.route("/config/save", methods=["POST"])
    def config_save():
        ingress = request.headers.get("X-Ingress-Path", "")
        try:
            from ..config import load_config, validate_config
            path = os.environ.get("ENERGIEHA_OPTIONS_PATH", "/data/options.json")
            existing = {}
            if os.path.exists(path):
                with open(path) as f:
                    existing = json.load(f)
            for key in request.form:
                val = request.form[key]
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
                    try: existing[key] = int(val)
                    except ValueError:
                        try: existing[key] = float(val)
                        except ValueError: existing[key] = val
            for bk in ["dry_run", "direct_control", "phev_enabled", "sungrow_tou_enabled"]:
                if bk not in request.form:
                    existing[bk] = False
            with open(path, "w") as f:
                json.dump(existing, f, indent=2)
            state = AppState()
            state.config = load_config()
            return redirect(f"{ingress}/config?msg=Gespeichert&msg_type=success")
        except Exception as e:
            return redirect(f"{ingress}/config?msg=Fehler&msg_type=error")

    @app.route("/inverter")
    def inverter_page():
        try:
            state = AppState()
            config = state.config
            inv = {}
            try:
                from ..ha_client import HaClient
                from ..inverter_control import InverterController
                inv = InverterController(HaClient(), config).read_inverter_state()
            except Exception as e:
                logger.warning("Inverter read: %s", e)
            return render_template("inverter.html", inverter=inv, config=config, active_page="inverter")
        except Exception as e:
            logger.error("Inverter page error: %s", e, exc_info=True)
            return render_template("inverter.html", inverter={}, config=None, active_page="inverter")

    @app.route("/inverter/tou/<int:n>", methods=["POST"])
    def inverter_tou(n):
        try:
            from ..ha_client import HaClient
            from ..inverter_control import InverterController
            ctrl = InverterController(HaClient(), AppState().config)
            ctrl.set_tou_program(n, request.form.get("start_time", "00:00"), "",
                                 request.form.get("mode", "Disabled"), int(request.form.get("soc_target", 0)))
        except Exception as e:
            logger.error("TOU set: %s", e)
        return redirect(f"{request.headers.get('X-Ingress-Path', '')}/inverter")

    @app.route("/inverter/phev", methods=["POST"])
    def inverter_phev():
        try:
            from ..ha_client import HaClient
            from ..inverter_control import InverterController
            InverterController(HaClient(), AppState().config).set_phev_charge_current(int(request.form.get("ampere", 0)))
        except Exception as e:
            logger.error("PHEV set: %s", e)
        return redirect(f"{request.headers.get('X-Ingress-Path', '')}/inverter")

    # ================================================================
    # API ROUTES - return JSON, never crash
    # ================================================================

    @app.route("/api/state")
    def api_state():
        try:
            return jsonify(AppState().get_status_dict())
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/plan")
    def api_plan():
        try:
            state = AppState()
            if not state.plan or not state.plan.slots:
                return jsonify({"slots": []})
            return jsonify({"slots": [{"time": s.start.strftime("%H:%M"), "mode": s.planned_battery_mode,
                "soc": round(s.projected_soc, 1), "battery_w": round(s.planned_battery_w),
                "pv_w": round(s.pv_forecast_w), "load_w": round(s.load_estimate_w),
                "grid_w": round(s.planned_grid_w), "price": round(s.price_eur_kwh, 4)}
                for s in state.plan.slots[:96]]})
        except Exception as e:
            return jsonify({"slots": [], "error": str(e)}), 500

    @app.route("/api/prices")
    def api_prices():
        try:
            state = AppState()
            prices = state.prices or []
            threshold = 0.0
            if state.snapshot and state.snapshot.dynamic_price_threshold > 0:
                threshold = state.snapshot.dynamic_price_threshold
            elif state.config:
                threshold = state.config.price_threshold_eur
            charge_ranges = []
            try:
                if state.plan and state.plan.slots:
                    from ..strategies.helpers import is_grid_charging
                    from datetime import timedelta
                    for s in state.plan.slots:
                        if (s.planned_battery_mode == "charge" and s.planned_battery_w > 50
                                and is_grid_charging(s.pv_forecast_w, s.load_estimate_w, s.planned_battery_w)):
                            charge_ranges.append({"start": s.start.isoformat(),
                                "end": (s.start + timedelta(minutes=s.duration_min)).isoformat()})
            except Exception as ex:
                logger.warning("charge_ranges calc: %s", ex)
            return jsonify({"prices": prices, "threshold": round(threshold, 4),
                            "charge_ranges": charge_ranges, "count": len(prices)})
        except Exception as e:
            return jsonify({"prices": [], "threshold": 0, "charge_ranges": [], "count": 0, "error": str(e)}), 500

    @app.route("/api/forecast")
    def api_forecast():
        try:
            state = AppState()
            return jsonify({"forecast": state.pv_forecast or [], "count": len(state.pv_forecast or [])})
        except Exception as e:
            return jsonify({"forecast": [], "count": 0, "error": str(e)}), 500

    @app.route("/api/savings")
    def api_savings():
        try:
            return jsonify(AppState().savings or {})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/cycles")
    def api_cycles():
        try:
            state = AppState()
            return jsonify([{"time": c.timestamp.strftime("%H:%M:%S") if c.timestamp else "",
                "strategy": c.strategy, "mode": c.battery_mode, "soc": round(c.battery_soc, 1),
                "pv": round(c.pv_power_w), "grid": round(c.grid_power_w), "load": round(c.load_power_w),
                "error": c.error or ""} for c in state.get_cycle_history(50)])
        except Exception as e:
            return jsonify([]), 500

    @app.route("/api/errors")
    def api_errors():
        try:
            return jsonify(AppState().get_error_log(20))
        except Exception as e:
            return jsonify([]), 500

    @app.route("/api/replan", methods=["POST"])
    def api_replan():
        try:
            AppState()._force_replan = True
            return jsonify({"status": "ok", "message": "Replan getriggert"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/inverter/reset-tou", methods=["POST"])
    def api_reset_tou():
        try:
            from ..ha_client import HaClient
            from ..inverter_control import InverterController
            ctrl = InverterController(HaClient(), AppState().config)
            for i in range(1, 7):
                ctrl.set_tou_program(i, "00:00", "", "Disabled", 0)
            return jsonify({"status": "ok", "message": "Alle TOU zurueckgesetzt"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/inverter/emergency-idle", methods=["POST"])
    def api_emergency():
        try:
            from ..ha_client import HaClient
            from ..inverter_control import InverterController
            ctrl = InverterController(HaClient(), AppState().config)
            for i in range(1, 7):
                ctrl.set_tou_program(i, "00:00", "", "Disabled", 0)
            AppState()._force_replan = True
            return jsonify({"status": "ok", "message": "Notfall-Idle + Replan"})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})

    @app.route("/debug")
    def debug_info():
        rules = sorted([r.rule for r in app.url_map.iter_rules() if not r.rule.startswith("/static")])
        return jsonify({"routes": rules, "count": len(rules), "path": request.path,
                        "ingress": request.headers.get("X-Ingress-Path", ""), "version": __version__})

    logger.info("Flask app: %d routes registered", len(list(app.url_map.iter_rules())))
    return app


def start_server():
    from .. import __version__
    app = create_app()
    port = int(os.environ.get("INGRESS_PORT", 5050))
    logger.info("EnergieHA web v%s on port %d", __version__, port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
