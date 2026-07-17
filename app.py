"""
app.py — Flask Application Entry Point
=======================================
Creates and configures the Flask app:
  - Loads .env
  - Configures CORS with SSE support (text/event-stream)
  - Registers blueprints: info_bp, download_bp
  - Exposes /health liveness endpoint
  - Exposes /debug diagnostics endpoint
  - Runs dev server on PORT (default 5000)

Compatible with Python 3.12+
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

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
    frontend_url = os.getenv("FRONTEND_URL", "http://127.0.0.1:5500")
    allowed_origins = list(dict.fromkeys([
        frontend_url,
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5000",
        "http://localhost:5000",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]))

    CORS(
        app,
        resources={r"/*": {"origins": allowed_origins}},
        supports_credentials=False,
        allow_headers=["Content-Type", "Authorization", "Cache-Control"],
        expose_headers=["Content-Disposition", "Content-Type", "Cache-Control", "Content-Length"],
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

    # ── Debug diagnostics ──────────────────────────────────────────────────
    @app.route("/debug", methods=["GET"])
    def debug_info():
        """
        Return system diagnostics for troubleshooting deployment issues.

        Response:
        {
            "ffmpeg": "/usr/bin/ffmpeg" or "NOT FOUND",
            "ffmpeg_version": "...",
            "yt_dlp_version": "2025.06.30",
            "python_version": "3.12.3",
            "platform": "Linux-...",
            "tmp_dir": "/tmp/videodl",
            "tmp_dir_exists": true,
            "tmp_dir_writable": true,
            "cwd": "/opt/render/project/src",
            "env_flask_env": "production",
            "env_ffmpeg_path": "/usr/bin/ffmpeg",
            "env_tmp_dir": "/tmp/videodl"
        }
        """
        import yt_dlp as _yt_dlp

        # FFmpeg detection
        ffmpeg_path = os.environ.get("FFMPEG_PATH", "")
        ffmpeg_resolved = "NOT FOUND"
        ffmpeg_version = "N/A"

        # Check explicit path
        if ffmpeg_path and os.path.isfile(ffmpeg_path):
            ffmpeg_resolved = ffmpeg_path
        else:
            # Try which
            try:
                result = subprocess.run(
                    ["which", "ffmpeg"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    ffmpeg_resolved = result.stdout.strip()
            except Exception:
                pass

            # Try common paths
            if ffmpeg_resolved == "NOT FOUND":
                for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
                    if os.path.isfile(p):
                        ffmpeg_resolved = p
                        break

        # Get FFmpeg version
        if ffmpeg_resolved != "NOT FOUND":
            try:
                result = subprocess.run(
                    [ffmpeg_resolved, "-version"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    first_line = result.stdout.split("\n")[0]
                    ffmpeg_version = first_line
            except Exception as e:
                ffmpeg_version = f"error: {e}"

        # Temp dir check
        tmp_dir = os.environ.get("TMP_DIR", "/tmp/videodl")
        tmp_exists = os.path.isdir(tmp_dir)
        tmp_writable = False
        if tmp_exists:
            tmp_writable = os.access(tmp_dir, os.W_OK)
        else:
            # Try to create it
            try:
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_exists = True
                tmp_writable = os.access(tmp_dir, os.W_OK)
            except Exception:
                pass

        return jsonify({
            "ffmpeg":           ffmpeg_resolved,
            "ffmpeg_version":   ffmpeg_version,
            "yt_dlp_version":   _yt_dlp.version.__version__,
            "python_version":   sys.version,
            "platform":         platform.platform(),
            "tmp_dir":          tmp_dir,
            "tmp_dir_exists":   tmp_exists,
            "tmp_dir_writable": tmp_writable,
            "cwd":              os.getcwd(),
            "env_flask_env":    os.environ.get("FLASK_ENV", "not set"),
            "env_ffmpeg_path":  ffmpeg_path or "not set",
            "env_tmp_dir":      os.environ.get("TMP_DIR", "not set"),
        }), 200

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
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
