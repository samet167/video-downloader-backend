"""
routes/info.py — POST /api/info
=================================
Validates and normalizes the video URL, then returns metadata.

Error handling:
  - Always returns detailed JSON error (never empty 422).
  - Includes: error message, exception type, yt-dlp version, runtime info.
"""

from __future__ import annotations

import os
import re
import subprocess
import traceback
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from flask import Blueprint, Response, jsonify, request

from services.downloader_service import VideoServiceError, get_video_info
from utils.logger import get_logger

log = get_logger(__name__)
info_bp = Blueprint("info", __name__)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_runtime_info() -> dict:
    """Gather runtime diagnostics for error responses."""
    import yt_dlp
    info = {
        "yt_dlp_version": yt_dlp.version.__version__,
    }
    # FFmpeg
    try:
        r = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True, timeout=3)
        info["ffmpeg"] = r.stdout.strip() if r.returncode == 0 else "NOT FOUND"
    except Exception:
        info["ffmpeg"] = "NOT FOUND"
    # Node.js
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=3)
        info["nodejs"] = r.stdout.strip() if r.returncode == 0 else "NOT FOUND"
    except Exception:
        info["nodejs"] = "NOT FOUND"
    return info


def _err(msg: str, code: int = 400, exc: BaseException | None = None) -> tuple[Response, int]:
    """
    Return a detailed JSON error response.
    Always includes runtime diagnostics so we can debug on Render.
    """
    log.warning("[/api/info] %d: %s", code, msg)

    payload: dict = {
        "success": False,
        "error": msg,
    }

    # Always include diagnostics for yt-dlp errors
    if exc is not None:
        payload["type"] = type(exc).__name__
        payload["details"] = str(exc)
        # Include runtime info for 422/500 errors
        if code >= 422:
            payload.update(_get_runtime_info())
        # Include traceback in non-production
        if os.environ.get("FLASK_ENV", "").lower() != "production":
            payload["traceback"] = traceback.format_exc()

    return jsonify(payload), code


def _is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def normalize_url(url: str) -> str:
    """
    Clean a video URL for yt-dlp compatibility.
    """
    parsed = urlparse(url)
    host   = parsed.netloc.lower()

    # youtu.be short link
    if host in ("youtu.be", "www.youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0]
        if video_id:
            qs = parse_qs(parsed.query)
            clean_qs: dict[str, str] = {}
            if "t" in qs:
                clean_qs["t"] = qs["t"][0]
            query = "v=" + video_id
            if clean_qs:
                query += "&" + urlencode(clean_qs)
            return urlunparse(("https", "www.youtube.com", "/watch", "", query, ""))

    # YouTube / YouTube Music
    if re.search(r"(youtube\.com|music\.youtube\.com)", host):
        _REMOVE = {"si", "feature", "pp", "ab_channel", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "igshid"}
        qs      = parse_qs(parsed.query)
        cleaned = {k: v[0] for k, v in qs.items() if k not in _REMOVE}
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(cleaned), ""))

    # TikTok
    if "tiktok.com" in host:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    # Generic
    _GENERIC = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "igshid"}
    qs      = parse_qs(parsed.query)
    cleaned = {k: v[0] for k, v in qs.items() if k not in _GENERIC}
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(cleaned), ""))


# ── Route ────────────────────────────────────────────────────────────────────

@info_bp.route("/api/info", methods=["POST"])
def api_info() -> tuple[Response, int]:
    """
    Fetch video metadata without downloading.

    Request JSON:
        { "url": "https://www.youtube.com/watch?v=..." }

    Response 200:
        { "success": true, "title": "...", "duration": "4:05", ... }

    Error 422 (detailed):
        {
            "success": false,
            "error": "human-readable message",
            "type": "VideoServiceError",
            "details": "full exception text",
            "yt_dlp_version": "2025.xx.xx",
            "ffmpeg": "/usr/bin/ffmpeg",
            "nodejs": "v20.x.x"
        }
    """
    body = request.get_json(silent=True)
    if not body:
        return _err("Request body must be JSON with a 'url' field.")

    url: str = (body.get("url") or "").strip()
    if not url:
        return _err("'url' field is required.")

    if url.startswith("www."):
        url = "https://" + url

    if not _is_valid_url(url):
        return _err("Invalid URL — must start with http:// or https://")

    url = normalize_url(url)
    log.info("[/api/info] normalized url=%s", url)

    try:
        data = get_video_info(url)
    except VideoServiceError as exc:
        return _err(str(exc), 422, exc)
    except Exception as exc:
        log.exception("[/api/info] unexpected error for %s", url)
        return _err(f"Server error: {exc}", 500, exc)

    best_quality = data["formats"][0]["quality"] if data["formats"] else "N/A"

    return jsonify({
        "success":   True,
        "title":     data["title"],
        "duration":  data["duration_str"] or "N/A",
        "thumbnail": data["thumbnail"],
        "uploader":  data.get("uploader", "N/A"),
        "quality":   best_quality,
        "formats":   data["formats"],
    }), 200
