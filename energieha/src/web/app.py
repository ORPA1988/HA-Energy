"""Flask application factory with HA ingress support."""

import logging
import os

from flask import Flask, request

from ..state import AppState

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.url_map.strict_slashes = False

    # HA ingress path handling
    @app.before_request
    def handle_ingress_path():
        ingress_path = request.headers.get("X-Ingress-Path", "")
        request.ingress_path = ingress_path

    @app.context_processor
    def inject_globals():
        from .. import __version__
        ingress_path = getattr(request, "ingress_path", "")
        return {"ingress_path": ingress_path, "version": __version__}

    # Register blueprints WITHOUT url_prefix - routes have full paths
    blueprint_errors = []
    blueprints = [
        ("dashboard", ".routes.dashboard"),
        ("config", ".routes.config_routes"),
        ("planning", ".routes.planning"),
        ("inverter", ".routes.inverter"),
        ("logs", ".routes.logs"),
        ("api", ".routes.api"),
    ]
    for name, module_path in blueprints:
        try:
            import importlib
            mod = importlib.import_module(module_path, package=__name__.rsplit(".", 1)[0] + ".web")
            app.register_blueprint(mod.bp)
            logger.info("Blueprint '%s' registered OK", name)
        except Exception as e:
            logger.error("Blueprint '%s' FAILED: %s", name, e, exc_info=True)
            blueprint_errors.append(f"{name}: {e}")

    # Debug: log all registered routes
    with app.app_context():
        rules = [f"{r.rule} [{','.join(r.methods - {'OPTIONS','HEAD'})}]" for r in app.url_rules]
        logger.info("Registered %d routes: %s", len(rules), "; ".join(rules))

    # Debug endpoint: shows routes + request info
    @app.route("/debug")
    def debug_routes():
        from flask import jsonify
        rules = []
        for r in app.url_rules:
            rules.append({"rule": r.rule, "endpoint": r.endpoint,
                          "methods": list(r.methods - {"OPTIONS", "HEAD"})})
        return jsonify({
            "routes": rules,
            "request_path": request.path,
            "request_url": request.url,
            "ingress_path": request.headers.get("X-Ingress-Path", ""),
            "host": request.host,
            "headers": {k: v for k, v in request.headers if k.startswith("X-")},
        })

    # Log every request for debugging 404s
    @app.after_request
    def log_request(response):
        if response.status_code >= 400:
            logger.warning("HTTP %d: %s %s (ingress=%s)",
                          response.status_code, request.method, request.path,
                          request.headers.get("X-Ingress-Path", ""))
        return response

    # Publish route info to HA sensor on startup
    try:
        from ..ha_client import HaClient
        ha = HaClient()
        route_list = [r.rule for r in app.url_rules if r.rule != "/static/<path:filename>"]
        ha.set_state("sensor.energieha_routes", str(len(route_list)), {
            "friendly_name": "EnergieHA Routes",
            "icon": "mdi:routes",
            "routes": route_list,
            "blueprint_errors": blueprint_errors,
            "blueprints_ok": len(blueprints) - len(blueprint_errors),
        })
        logger.info("Published %d routes to HA sensor, %d blueprint errors",
                    len(route_list), len(blueprint_errors))
    except Exception as e:
        logger.error("Failed to publish routes sensor: %s", e)

    return app


def start_server():
    """Start the Flask web server (called from main entry point)."""
    from .. import __version__

    app = create_app()

    port = int(os.environ.get("INGRESS_PORT", 5050))
    logger.info("Starting EnergieHA web server v%s on port %d", __version__, port)

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
