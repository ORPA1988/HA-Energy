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
    from .routes.dashboard import bp as dashboard_bp
    from .routes.config_routes import bp as config_bp
    from .routes.planning import bp as planning_bp
    from .routes.inverter import bp as inverter_bp
    from .routes.logs import bp as logs_bp
    from .routes.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(planning_bp)
    app.register_blueprint(inverter_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(api_bp)

    # Debug: log all registered routes
    with app.app_context():
        rules = [f"{r.rule} [{','.join(r.methods - {'OPTIONS','HEAD'})}]" for r in app.url_rules]
        logger.info("Registered %d routes: %s", len(rules), "; ".join(rules))

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
