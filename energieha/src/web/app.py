"""Flask application factory with HA ingress support."""

import logging
import os

from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.url_map.strict_slashes = False

    @app.before_request
    def handle_ingress_path():
        ingress_path = request.headers.get("X-Ingress-Path", "")
        request.ingress_path = ingress_path

    @app.context_processor
    def inject_globals():
        from .. import __version__
        ingress_path = getattr(request, "ingress_path", "")
        return {"ingress_path": ingress_path, "version": __version__}

    # Import and register all blueprints directly
    from .routes.dashboard import bp as dashboard_bp
    from .routes.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(api_bp)

    # Register sub-page blueprints - each with try/except so one failure
    # doesn't take down the whole app
    try:
        from .routes.planning import bp as planning_bp
        app.register_blueprint(planning_bp)
        logger.info("Planning blueprint registered")
    except Exception as e:
        logger.error("Planning blueprint failed: %s", e)

    try:
        from .routes.config_routes import bp as config_bp
        app.register_blueprint(config_bp)
        logger.info("Config blueprint registered")
    except Exception as e:
        logger.error("Config blueprint failed: %s", e)

    try:
        from .routes.inverter import bp as inverter_bp
        app.register_blueprint(inverter_bp)
        logger.info("Inverter blueprint registered")
    except Exception as e:
        logger.error("Inverter blueprint failed: %s", e)

    try:
        from .routes.logs import bp as logs_bp
        app.register_blueprint(logs_bp)
        logger.info("Logs blueprint registered")
    except Exception as e:
        logger.error("Logs blueprint failed: %s", e)

    # Debug route
    @app.route("/debug")
    def debug_info():
        rules = [{"rule": r.rule, "methods": list(r.methods - {"OPTIONS", "HEAD"})}
                 for r in app.url_rules if not r.rule.startswith("/static")]
        return jsonify({
            "routes": rules,
            "path": request.path,
            "ingress": request.headers.get("X-Ingress-Path", ""),
        })

    # Log 404s for debugging
    @app.errorhandler(404)
    def not_found(e):
        logger.warning("404: %s %s", request.method, request.path)
        return f"<h1>404 Not Found</h1><p>Path: {request.path}</p>", 404

    return app


def start_server():
    """Start the Flask web server."""
    from .. import __version__
    app = create_app()
    port = int(os.environ.get("INGRESS_PORT", 5050))
    logger.info("Starting EnergieHA web server v%s on port %d", __version__, port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
