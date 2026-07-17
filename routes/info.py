"""
routes/info.py — POST /api/info
=================================
Validates and normalizes the video URL, then returns metadata:
  title, thumbnail, duration, uploader, quality list with filesizes.

No media is downloaded — this is a metadata-only endpoint.

Error handling:
  - In DEBUG mode (FLASK_ENV != "production"), returns detailed error info
    including exception type, yt-dlp message, and traceback.
  - In production mode, returns sanitized error messages only.
"""

from __future__ import annotations

import os
import re
import traceback
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from flask import Blueprint, Response, jsonify, request

from services.downloader_service import VideoServiceError, get_video_info
from utils.logger import get_logger

log = get_logger(__name__)
info_bp = Blueprint("info", __name__)

# Debug mode flag
_IS_DEBUG = os.environ.get("FLASK_ENV", "").lower() != "production"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _err(msg: str, code: int = 400, exc: BaseException | None = None) -> tuple[Response, int]:
    """
    Return a standard JSON error response.

    In DEBUG mode, includes:
      - exception: exception class name
      - yt_dlp_message: cleaned error string
      - traceback: full traceback string
    """
    log.warning("[/api/info] %d: %s", code, msg)

    payload: dict = {"success": False, "error": msg}

    if _IS_DEBUG and exc is not None:
        payload["exception"] = type(exc).__name__
        payload["yt_dlp_message"] = str(exc)
        payload["traceback"] = traceback.format_exc()

    return jsonify(payload), code


def _is_valid_url(url: str) -> bool:
    """Return True only if url is an http/https URL with a non-empty host."""
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def normalize_url(url: str) -> str:
    """
    Clean a video URL for yt-dlp compatibility.

    Transformations:
      1. youtu.be/<id>        → youtube.com/watch?v=<id>  (keep ?t= only)
      2. YouTube/Music        → strip ?si=, utm_*, fbclid, feature, pp, ab_channel
      3. TikTok               → strip all query params (path is enough)
      4. Generic              → strip common tracking params

    Args:
        url: raw URL from the user (already confirmed http/https)

    Returns:
        Cleaned URL safe to hand to yt-dlp
    """
    parsed = urlparse(url)
    host   = parsed.netloc.lower()

    # ── youtu.be short link ────────────────────────────────────────────
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

    # ── YouTube / YouTube Music ────────────────────────────────────────
    if re.search(r"(youtube\.com|music\.youtube\.com)", host):
        _REMOVE = {
            "si", "feature", "pp", "ab_channel",
            "utm_source", "utm_medium", "utm_campaign",
            "utm_term",   "utm_content",
            "fbclid",     "igshid",
        }
        qs      = parse_qs(parsed.query)
        cleaned = {k: v[0] for k, v in qs.items() if k not in _REMOVE}
        return urlunparse((
            parsed.scheme, parsed.netloc, parsed.path,
            parsed.params, urlencode(cleaned), ""
        ))

    # ── TikTok — path alone is sufficient ─────────────────────────────
    if "tiktok.com" in host:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

    # ── Generic tracking param removal ────────────────────────────────
    _GENERIC = {
        "utm_source", "utm_medium", "utm_campaign",
        "utm_term",   "utm_content",
        "fbclid",     "igshid",
    }
    qs      = parse_qs(parsed.query)
    cleaned = {k: v[0] for k, v in qs.items() if k not in _GENERIC}
    return urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, urlencode(cleaned), ""
    ))


# ── Route ────────────────────────────────────────────────────────────────────

@info_bp.route("/api/info", methods=["POST"])
def api_info() -> tuple[Response, int]:
    """
    Fetch video metadata without downloading.

    Request JSON:
        { "url": "https://www.youtube.com/watch?v=..." }

    Response 200:
        {
            "success":   true,
            "title":     "Video Title",
            "duration":  "4:05",
            "thumbnail": "https://...",
            "uploader":  "Channel Name",
            "quality":   "1080p",       ← best available quality label
            "formats": [
                {
                    "format_id":  "137+140",
                    "resolution": "1920x1080",
                    "quality":    "1080p",
                    "ext":        "mp4",
                    "filesize":   52428800   ← bytes, or null
                }
            ]
        }

    Error responses:
        400 — missing or invalid URL
        422 — yt-dlp cannot process URL (private, geo-blocked, …)
              In DEBUG mode includes: exception, yt_dlp_message, traceback
        500 — unexpected server error
    """
    body = request.get_json(silent=True)
    if not body:
        return _err("Request body must be JSON with a 'url' field.")

    url: str = (body.get("url") or "").strip()
    if not url:
        return _err("'url' field is required.")

    # Auto-prefix bare www. addresses
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
