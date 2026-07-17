"""
routes/download.py — Download API Routes
==========================================
Endpoints:
  POST /api/download/start     — start a background download, return task_id
  GET  /api/progress/<task_id> — SSE stream of progress updates
  POST /api/open-folder        — open the save folder in the OS file manager
  GET  /api/default-save-dir   — return the server's default save directory

Flow:
  1. Browser POSTs to /api/download/start → gets back { task_id, save_dir }
  2. Browser opens EventSource to /api/progress/<task_id>
  3. Server streams JSON events: { status, percent, speed, eta, filesize, … }
  4. On status=="done", browser shows success UI with filepath + save_dir
  5. On desktop, browser can POST /api/open-folder to reveal the file
"""

from __future__ import annotations

import json
import platform
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, Response, jsonify, request, stream_with_context

from routes.info import normalize_url
from services.downloader_service import (
    VideoServiceError,
    get_task_progress,
    remove_task,
    start_download,
)
from utils.file_manager import get_os_type, get_server_download_dir
from utils.logger import get_logger

log = get_logger(__name__)
download_bp = Blueprint("download", __name__)

# Allowed quality labels → max pixel height
QUALITY_HEIGHT: dict[str, int] = {
    "auto":  1080,
    "1080p": 1080,
    "720p":  720,
    "480p":  480,
    "360p":  360,
}


# ── Shared helpers ───────────────────────────────────────────────────────────

def _err(msg: str, code: int = 400) -> tuple[Response, int]:
    log.warning("[download] error %d: %s", code, msg)
    return jsonify({"success": False, "error": msg}), code


def _is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════════════════
# POST /api/download/start
# ════════════════════════════════════════════════════════════════════════════

@download_bp.route("/api/download/start", methods=["POST"])
def api_download_start() -> tuple[Response, int]:
    """
    Start a background download and return a task_id for SSE polling.

    Request JSON:
        {
            "url":      "https://...",
            "quality":  "720p",           ← optional, default "auto"
            "save_dir": "/custom/path"    ← optional, desktop only
        }

    Response 200:
        {
            "success":  true,
            "task_id":  "550e8400-...",
            "save_dir": "/Users/username/Downloads",
            "os_type":  "macos"
        }
    """
    body = request.get_json(silent=True)
    if not body:
        return _err("Request body must be JSON.")

    url:      str = (body.get("url")      or "").strip()
    quality:  str = (body.get("quality")  or "auto").strip().lower()
    save_dir: str = (body.get("save_dir") or "").strip()

    # Validate URL
    if not url:
        return _err("'url' field is required.")
    if url.startswith("www."):
        url = "https://" + url
    if not _is_valid_url(url):
        return _err("Invalid URL — must start with http:// or https://")

    # Validate quality
    if quality not in QUALITY_HEIGHT:
        return _err(f"Invalid quality '{quality}'. Allowed: {', '.join(QUALITY_HEIGHT)}")

    url = normalize_url(url)
    log.info("[/api/download/start] url=%s quality=%s save_dir=%s",
             url, quality, save_dir or "(default)")

    # Resolve save directory so we can return it to the client immediately
    resolved_dir = get_server_download_dir(save_dir if save_dir else None)
    os_type      = get_os_type()

    # Launch background thread, get task_id
    try:
        task_id = start_download(
            url=url,
            format_id=None,          # let downloader pick best ≤ quality height
            save_dir=str(resolved_dir),
        )
    except VideoServiceError as exc:
        return _err(str(exc), 422)
    except Exception as exc:
        log.exception("[/api/download/start] unexpected error")
        return _err(f"Server error: {exc}", 500)

    return jsonify({
        "success":  True,
        "task_id":  task_id,
        "save_dir": str(resolved_dir),
        "os_type":  os_type,
    }), 200


# ════════════════════════════════════════════════════════════════════════════
# GET /api/progress/<task_id>  — Server-Sent Events
# ════════════════════════════════════════════════════════════════════════════

@download_bp.route("/api/progress/<task_id>", methods=["GET"])
def api_progress(task_id: str) -> Response:
    """
    SSE endpoint — streams download progress as JSON events.

    Events format (text/event-stream):
        data: {"status":"downloading","percent":42.5,"speed":"2.1 MB/s",...}\n\n

    Terminal statuses: "done" | "error"
    After a terminal event the stream closes automatically.

    The client should add ?retry=true query param if reconnecting.
    """
    @stream_with_context
    def _generate():
        # Send an initial ping so the browser knows the connection is open
        yield "data: {\"status\":\"connecting\"}\n\n"

        last_status = None
        polls       = 0
        max_polls   = 1200   # 10 minutes at 0.5s interval — safety limit

        while polls < max_polls:
            progress = get_task_progress(task_id)

            if progress is None:
                # Task not found — might have been cleaned up or never started
                yield f"data: {json.dumps({'status':'error','error':'Task not found'})}\n\n"
                break

            status = progress.get("status", "")

            # Only send an event when something changed (reduces noise)
            if progress != last_status:
                last_status = dict(progress)
                yield f"data: {json.dumps(progress)}\n\n"

            if status in ("done", "error"):
                # Clean up progress store after a short delay
                # (give the client time to read the final event)
                import threading
                threading.Timer(30.0, lambda: remove_task(task_id)).start()
                break

            # Keep-alive comment every ~10 s to prevent proxy timeout
            if polls % 20 == 0 and polls > 0:
                yield ": keep-alive\n\n"

            time.sleep(0.5)
            polls += 1

        if polls >= max_polls:
            yield "data: {\"status\":\"error\",\"error\":\"Download timed out\"}\n\n"

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",     # Disable nginx buffering
            "Connection":       "keep-alive",
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# GET /api/default-save-dir
# ════════════════════════════════════════════════════════════════════════════

@download_bp.route("/api/default-save-dir", methods=["GET"])
def api_default_save_dir() -> tuple[Response, int]:
    """
    Return the server's default save directory and OS type.

    Response 200:
        {
            "success":  true,
            "save_dir": "/Users/username/Downloads",
            "os_type":  "macos"
        }
    """
    save_dir = get_server_download_dir()
    return jsonify({
        "success":  True,
        "save_dir": str(save_dir),
        "os_type":  get_os_type(),
    }), 200


# ════════════════════════════════════════════════════════════════════════════
# POST /api/open-folder
# ════════════════════════════════════════════════════════════════════════════

@download_bp.route("/api/open-folder", methods=["POST"])
def api_open_folder() -> tuple[Response, int]:
    """
    Open a folder or file in the OS native file manager.

    Supported on: macOS (Finder), Linux (xdg-open), Windows (Explorer).
    Returns 501 on unsupported platforms (Android, iOS server-side).

    Request JSON:
        { "path": "/Users/username/Downloads/video.mp4" }

    Response 200:
        { "success": true }
    """
    body = request.get_json(silent=True)
    if not body:
        return _err("Request body must be JSON.")

    raw_path: str = (body.get("path") or "").strip()
    if not raw_path:
        return _err("'path' field is required.")

    target = Path(raw_path).expanduser().resolve()
    if not target.exists():
        return _err(f"Path does not exist: {target}", 404)

    system = platform.system().lower()

    try:
        if system == "darwin":
            # -R reveals the file and selects it in Finder
            subprocess.Popen(["open", "-R", str(target)])

        elif system == "windows":
            # /select highlights the file in Explorer
            subprocess.Popen(["explorer", f"/select,{target}"])

        elif system == "linux":
            # Open the parent folder via xdg-open
            folder = str(target.parent if target.is_file() else target)
            subprocess.Popen(["xdg-open", folder])

        else:
            return jsonify({
                "success": False,
                "error":   f"Open-folder not supported on: {platform.system()}",
            }), 501

    except FileNotFoundError as exc:
        return _err(f"Cannot open folder: {exc}", 500)
    except Exception as exc:
        log.exception("[/api/open-folder] error for path=%s", target)
        return _err(f"Server error: {exc}", 500)

    return jsonify({"success": True}), 200
