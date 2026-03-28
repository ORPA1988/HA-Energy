"""Flask application factory with HA ingress support."""

import logging
import os
import threading

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

    # HA ingress path handling
    @app.before_request
    def handle_ingress_path():
        """Set ingress base path from HA header for URL generation."""
        ingress_path = request.headers.get("X-Ingress-Path", "")
        request.ingress_path = ingress_path

    @app.context_processor
    def inject_ingress():
        """Make ingress_path available in all templates."""
        ingress_path = getattr(request, "ingress_path", "")
        return {"ingress_path": ingress_path}

    # Register route blueprints
    from .routes.dashboard import bp as dashboard_bp
    from .routes.config_routes import bp as config_bp
    from .routes.planning import bp as planning_bp
    from .routes.inverter import bp as inverter_bp
    from .routes.logs import bp as logs_bp
    from .routes.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(config_bp, url_prefix="/config")
    app.register_blueprint(planning_bp, url_prefix="/planning")
    app.register_blueprint(inverter_bp, url_prefix="/inverter")
    app.register_blueprint(logs_bp, url_prefix="/logs")
    app.register_blueprint(api_bp, url_prefix="/api")

    return app


def start_server():
    """Start the Flask web server (called from main entry point)."""
    from .. import __version__

    app = create_app()

    port = int(os.environ.get("INGRESS_PORT", 5050))
    logger.info("Starting EnergieHA web server v%s on port %d", __version__, port)

    # Run Flask in production mode (no reloader in addon container)
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
