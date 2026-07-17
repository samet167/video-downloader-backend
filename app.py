"""
app.py — Flask Application Entry Point
=======================================
Creates and configures the Flask app:
  - Loads .env
  - Configures CORS with SSE support (text/event-stream)
  - Registers blueprints: info_bp, download_bp
  - Exposes /health liveness endpoint
  - Runs dev server on PORT (default 5000)

Compatible with Python 3.12+
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env before importing anything else that reads env vars
load_dotenv()

from flask import Flask, jsonify
from flask_cors import CORS

from routes.download import download_bp
from routes.info import info_bp
from utils.logger import get_logger

log = get_logger(__name__)


def create_app() -> Flask:
    """Application factory — returns a configured Flask instance."""
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-prod")

    # ── CORS ──────────────────────────────────────────────────────────────
    # Allow the frontend origin(s) to call the API and receive SSE streams.
    # SSE (text/event-stream) requires Cache-Control and Content-Type to be
    # exposed, so we add them to expose_headers.
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5500")
    allowed_origins = list(dict.fromkeys([
        frontend_url,
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        # Common VS Code Live Server ports
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]))

    CORS(
        app,
        resources={r"/*": {"origins": allowed_origins}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization", "Cache-Control"],
        expose_headers=["Content-Disposition", "Content-Type", "Cache-Control"],
        methods=["GET", "POST", "OPTIONS"],
    )
    log.info("CORS enabled for origins: %s", allowed_origins)

    # ── Blueprints ─────────────────────────────────────────────────────────
    app.register_blueprint(info_bp)
    app.register_blueprint(download_bp)

    # ── Health check ───────────────────────────────────────────────────────
    @app.route("/health", methods=["GET"])
    def health():
        """Liveness probe — returns 200 OK with status JSON."""
        return jsonify({"status": "ok", "service": "video-downloader"}), 200

    # ── Generic error handlers ─────────────────────────────────────────────
    @app.errorhandler(404)
    def not_found(_e):
        return jsonify({"success": False, "error": "Endpoint not found."}), 404

    @app.errorhandler(405)
    def method_not_allowed(_e):
        return jsonify({"success": False, "error": "Method not allowed."}), 405

    @app.errorhandler(500)
    def server_error(_e):
        log.exception("Unhandled 500 error")
        return jsonify({"success": False, "error": "Internal server error."}), 500

    return app


# ── WSGI entry point ───────────────────────────────────────────────────────
app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info("Starting Flask dev server on http://0.0.0.0:%d", port)
    log.info("Press Ctrl+C to stop.")
    # threaded=True allows concurrent SSE streams + new requests
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
