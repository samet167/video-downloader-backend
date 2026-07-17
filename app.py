"""
app.py — Flask Application Entry Point
=======================================
Creates and configures the Flask app:
  - Loads .env
  - Configures CORS with SSE support
  - Registers blueprints: info_bp, download_bp
  - Exposes /health liveness endpoint
  - Exposes /debug diagnostics endpoint (Node.js, FFmpeg, yt-dlp version)

Compatible with Python 3.12+
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify
from flask_cors import CORS

from routes.download import download_bp
from routes.info import info_bp
from utils.logger import get_logger

log = get_logger(__name__)


def create_app() -> Flask:
    """Application factory."""
    app = Flask(__name__)
    app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key-change-in-prod")

    # ── CORS ──────────────────────────────────────────────────────────────
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
        return jsonify({"status": "ok", "service": "video-downloader"}), 200

    # ── Debug diagnostics ──────────────────────────────────────────────────
    @app.route("/debug", methods=["GET"])
    def debug_info():
        """
        Return system diagnostics for troubleshooting.

        GET /debug → JSON with python, yt-dlp, ffmpeg, node versions + paths.
        """
        import yt_dlp as _yt_dlp

        # FFmpeg
        ffmpeg_path = "NOT FOUND"
        ffmpeg_version = "N/A"
        env_ffmpeg = os.environ.get("FFMPEG_PATH", "")
        if env_ffmpeg and os.path.isfile(env_ffmpeg):
            ffmpeg_path = env_ffmpeg
        else:
            try:
                r = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    ffmpeg_path = r.stdout.strip()
            except Exception:
                pass
            if ffmpeg_path == "NOT FOUND":
                for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
                    if os.path.isfile(p):
                        ffmpeg_path = p
                        break

        if ffmpeg_path != "NOT FOUND":
            try:
                r = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    ffmpeg_version = r.stdout.split("\n")[0]
            except Exception as e:
                ffmpeg_version = f"error: {e}"

        # Node.js
        nodejs_path = "NOT FOUND"
        nodejs_version = "NOT FOUND"
        try:
            r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                nodejs_version = r.stdout.strip()
                rp = subprocess.run(["which", "node"], capture_output=True, text=True, timeout=5)
                if rp.returncode == 0:
                    nodejs_path = rp.stdout.strip()
        except Exception:
            pass

        # Temp dir
        tmp_dir = os.environ.get("TMP_DIR", "/tmp/videodl")
        tmp_exists = os.path.isdir(tmp_dir)
        tmp_writable = False
        if not tmp_exists:
            try:
                os.makedirs(tmp_dir, exist_ok=True)
                tmp_exists = True
            except Exception:
                pass
        if tmp_exists:
            tmp_writable = os.access(tmp_dir, os.W_OK)

        return jsonify({
            "python_version":   sys.version,
            "yt_dlp_version":   _yt_dlp.version.__version__,
            "ffmpeg_path":      ffmpeg_path,
            "ffmpeg_version":   ffmpeg_version,
            "node_path":        nodejs_path,
            "node_version":     nodejs_version,
            "platform":         platform.platform(),
            "cwd":              os.getcwd(),
            "temp_directory":   tmp_dir,
            "tmp_dir_exists":   tmp_exists,
            "tmp_dir_writable": tmp_writable,
            "env_flask_env":    os.environ.get("FLASK_ENV", "not set"),
            "env_ffmpeg_path":  env_ffmpeg or "not set",
        }), 200

    # ── Error handlers ─────────────────────────────────────────────────────
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
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
