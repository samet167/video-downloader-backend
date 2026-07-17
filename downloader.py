"""
downloader.py — yt-dlp Core Download Engine
=============================================
This module is the low-level engine that:
  - Manages a thread-safe progress store (task_id → progress dict)
  - Downloads video using yt-dlp + FFmpeg (≤ 1080p, merged to MP4)
  - Saves to a specified directory with duplicate-safe filenames
  - Reports rich progress data: percent, speed, ETA, filesize

Architecture:
  - Each download gets a unique task_id (UUID).
  - Progress is stored in a module-level dict protected by threading.Lock.
  - The Flask SSE route (/api/progress/<task_id>) polls this dict.

Environment variables:
  FFMPEG_PATH       — explicit path to ffmpeg binary
  MAX_VIDEO_SECONDS — max duration in seconds (default 3600)
  MAX_FILE_MB       — max output file size in MB (default 500)
  FLASK_ENV         — "production" disables verbose yt-dlp output

CRITICAL REQUIREMENT — JavaScript Runtime:
  yt-dlp 2025+ REQUIRES Node.js (or Deno/QuickJS) to solve YouTube's
  n-parameter throttle challenge. Without it:
    TypeError: 'NoneType' object is not callable
    at yt_dlp/utils/_jsruntime.py → yt_dlp/extractor/youtube/_video.py
  Node.js is installed via render.yaml buildCommand.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import ssl
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import certifi
import yt_dlp

from utils.file_manager import get_os_type, sanitize_filename, unique_path

log = logging.getLogger("videodl.downloader")

# ─────────────────────────────────────────────────────────────────────────────
# SSL Fix — certifi CA bundle injection
# ─────────────────────────────────────────────────────────────────────────────
_CERT_FILE = certifi.where()
os.environ.setdefault("SSL_CERT_FILE",      _CERT_FILE)
os.environ.setdefault("REQUESTS_CA_BUNDLE", _CERT_FILE)
ssl._create_default_https_context = ssl.create_default_context  # noqa: SLF001

# ─────────────────────────────────────────────────────────────────────────────
# Config from environment variables
# ─────────────────────────────────────────────────────────────────────────────
MAX_HEIGHT:   int   = 1080
MAX_DURATION: int   = int(os.environ.get("MAX_VIDEO_SECONDS", 7200))
MAX_FILE_MB:  float = float(os.environ.get("MAX_FILE_MB",     500))
IS_DEBUG:     bool  = os.environ.get("FLASK_ENV", "").lower() != "production"

FFMPEG_PATH: str | None = os.environ.get("FFMPEG_PATH") or None


# ─────────────────────────────────────────────────────────────────────────────
# Runtime detection helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_ffmpeg() -> str | None:
    """Return the FFmpeg path if it exists, else try to find it in PATH."""
    if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
        return FFMPEG_PATH
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    try:
        result = subprocess.run(
            ["which", "ffmpeg"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _find_nodejs() -> str | None:
    """Return path to Node.js binary, or None if not available."""
    try:
        result = subprocess.run(
            ["which", "node"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # Common paths (including Render persistent path)
    for p in [
        "/opt/render/project/src/.node/bin/node",
        "/usr/local/bin/node",
        "/usr/bin/node",
    ]:
        if Path(p).exists():
            return p
    return None


def _find_deno() -> str | None:
    """Return path to Deno binary, or None if not available."""
    try:
        result = subprocess.run(
            ["which", "deno"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # Common paths (including Render persistent path)
    for p in [
        "/opt/render/project/src/.deno/deno",
        "/usr/local/bin/deno",
        "/usr/bin/deno",
    ]:
        if Path(p).exists():
            return p
    return None


def _get_deno_version() -> str:
    """Return Deno version string or 'NOT FOUND'."""
    try:
        result = subprocess.run(
            ["deno", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip().split("\n")[0]
    except Exception:
        pass
    return "NOT FOUND"


def _get_nodejs_version() -> str:
    """Return Node.js version string or 'NOT FOUND'."""
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "NOT FOUND"


RESOLVED_FFMPEG: str | None = _find_ffmpeg()
RESOLVED_NODEJS: str | None = _find_nodejs()
RESOLVED_DENO:   str | None = _find_deno()

if RESOLVED_FFMPEG:
    log.info("FFmpeg found at: %s", RESOLVED_FFMPEG)
else:
    log.warning("FFmpeg NOT found! Video merging will fail.")

if RESOLVED_DENO:
    log.info("Deno found at: %s (%s)", RESOLVED_DENO, _get_deno_version())
elif RESOLVED_NODEJS:
    log.info("Node.js found at: %s (%s)", RESOLVED_NODEJS, _get_nodejs_version())
else:
    log.warning(
        "No JS runtime (Deno/Node.js) found! yt-dlp will fail on YouTube. "
        "Install deno or nodejs >= 22."
    )

# yt-dlp format selector: best video ≤ 1080p + best audio, merged to MP4
FORMAT_SELECTOR: str = (
    f"bestvideo[height<={MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
    f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/"
    f"best[height<={MAX_HEIGHT}]/"
    "best"
)


# ─────────────────────────────────────────────────────────────────────────────
# Thread-safe progress store
# ─────────────────────────────────────────────────────────────────────────────
_progress: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def get_progress(task_id: str) -> dict[str, Any] | None:
    with _lock:
        return dict(_progress[task_id]) if task_id in _progress else None


def cleanup_task(task_id: str) -> None:
    with _lock:
        _progress.pop(task_id, None)


def _set_progress(task_id: str, data: dict[str, Any]) -> None:
    with _lock:
        _progress[task_id] = data


# ─────────────────────────────────────────────────────────────────────────────
# Quality label helper
# ─────────────────────────────────────────────────────────────────────────────

def _quality_label(height: int | None) -> str:
    if not height:
        return "unknown"
    for limit, label in [(360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p")]:
        if height <= limit:
            return label
    return f"{height}p"


# ─────────────────────────────────────────────────────────────────────────────
# Base yt-dlp options (shared between info and download calls)
# ─────────────────────────────────────────────────────────────────────────────

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _base_ydl_opts() -> dict[str, Any]:
    """
    Base yt-dlp options for headless server (Render).

    Key configuration:
      - player_client=default: lets yt-dlp auto-pick the best YouTube client.
        This triggers the JS runtime (Deno/Node.js) for n-parameter solving.
      - formats=missing_pot: skip formats requiring PO token instead of failing.
      - Realistic User-Agent to reduce bot detection.
      - High retries + timeout for Render's shared network.
      - geo_bypass for region-restricted content.
      - remote_components: download EJS scripts from npm (Deno) or GitHub.
    """
    opts: dict[str, Any] = {
        # ── Logging ───────────────────────────────────────────────────────
        "quiet":              not IS_DEBUG,
        "no_warnings":        not IS_DEBUG,
        "verbose":            IS_DEBUG,

        # ── SSL ───────────────────────────────────────────────────────────
        "nocheckcertificate": False,
        "ssl_certificate":    _CERT_FILE,

        # ── Network resilience ────────────────────────────────────────────
        "socket_timeout":     45,
        "retries":            10,
        "fragment_retries":   10,
        "file_access_retries": 5,
        "extractor_retries":  5,

        # ── HTTP headers (mimic real browser) ─────────────────────────────
        "http_headers": {
            "User-Agent":      _USER_AGENT,
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Sec-Fetch-Mode":  "navigate",
        },

        # ── YouTube extractor ─────────────────────────────────────────────
        # 'default' = auto-negotiate (uses JS runtime for n-param).
        # 'formats=missing_pot' = gracefully skip PO-token formats.
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
                "formats":       ["missing_pot"],
            }
        },

        # ── Misc ──────────────────────────────────────────────────────────
        "geo_bypass":         True,
        "no_check_formats":   True,
    }

    if RESOLVED_FFMPEG:
        opts["ffmpeg_location"] = RESOLVED_FFMPEG

    # ── YouTube Cookie file (bypass bot detection on datacenter IPs) ───────
    # Set YOUTUBE_COOKIE_FILE env var to path of Netscape-format cookies.txt
    cookie_file = os.environ.get("YOUTUBE_COOKIE_FILE", "")
    if cookie_file and Path(cookie_file).is_file():
        opts["cookiefile"] = cookie_file
        log.info("Using cookie file: %s", cookie_file)

    # ── PO Token provider (bgutil) ────────────────────────────────────────
    # bgutil-ytdlp-pot-provider script mode: auto-generates PO tokens
    # to bypass "Sign in to confirm you're not a bot" on datacenter IPs.
    pot_server_home = os.environ.get(
        "POT_SERVER_HOME",
        "/opt/render/project/src/bgutil-ytdlp-pot-provider/server"
    )
    if Path(pot_server_home).is_dir():
        # Tell the plugin where the server scripts are located
        ea = opts.get("extractor_args", {})
        ea.setdefault("youtubepot-bgutilscript", {})
        ea["youtubepot-bgutilscript"]["server_home"] = [pot_server_home]
        opts["extractor_args"] = ea
        log.info("POT provider (bgutil script) configured: %s", pot_server_home)

    # ── JS Runtime configuration ──────────────────────────────────────────
    # Deno is the recommended runtime (enabled by default in yt-dlp).
    # Format: dict of {runtime_name: {config_dict}}
    if RESOLVED_DENO:
        opts["js_runtimes"] = {"deno": {"path": RESOLVED_DENO}}
        # Allow yt-dlp to download EJS challenge scripts from npm via Deno
        opts["remote_components"] = ["ejs:npm"]
    elif RESOLVED_NODEJS:
        opts["js_runtimes"] = {"node": {"path": RESOLVED_NODEJS}}
        # Fallback: download EJS scripts from GitHub for Node.js
        opts["remote_components"] = ["ejs:github"]

    return opts


# ─────────────────────────────────────────────────────────────────────────────
# get_video_info — metadata only, no download
# ─────────────────────────────────────────────────────────────────────────────

def get_video_info(url: str) -> dict[str, Any]:
    """
    Fetch video metadata without downloading.

    Raises:
        ValueError: on yt-dlp error or validation failure
    """
    opts = {
        **_base_ydl_opts(),
        "skip_download": True,
        "ignoreerrors":  False,
    }

    log.info("get_video_info: url=%s", url)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        log.error("get_video_info DownloadError: %s", msg)
        raise ValueError(msg) from exc
    except TypeError as exc:
        # Capture full traceback for debugging
        import traceback
        tb = traceback.format_exc()
        log.error("get_video_info TypeError: %s\n%s", exc, tb)
        raise ValueError(
            f"TypeError during video info extraction. "
            f"Node.js: {RESOLVED_NODEJS}, Deno: {RESOLVED_DENO}. "
            f"Error: {exc}. "
            f"Traceback: {tb[-500:]}"
        ) from exc
    except Exception as exc:
        log.exception("get_video_info unexpected error for %s", url)
        raise ValueError(f"Cannot fetch video info: {exc}") from exc

    if info is None:
        raise ValueError("No information returned for this URL.")

    # Duration limit
    duration = info.get("duration")
    if duration and duration > MAX_DURATION:
        raise ValueError(
            f"Video is too long ({duration // 60} min). "
            f"Maximum allowed: {MAX_DURATION // 60} min."
        )

    # Build deduplicated format list
    raw_formats: list[dict] = info.get("formats") or []
    seen:    set[str]       = set()
    formats: list[dict]     = []

    for fmt in raw_formats:
        h      = fmt.get("height")
        w      = fmt.get("width")
        vcodec = fmt.get("vcodec") or "none"

        if vcodec == "none":      continue
        if h and h > MAX_HEIGHT:  continue

        res = f"{w}x{h}" if w and h else (fmt.get("resolution") or "unknown")
        if res in seen:           continue
        seen.add(res)

        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        if filesize and filesize > MAX_FILE_MB * 1024 * 1024:
            continue

        formats.append({
            "format_id":  fmt.get("format_id", ""),
            "resolution": res,
            "quality":    _quality_label(h),
            "ext":        fmt.get("ext", "mp4"),
            "filesize":   int(filesize) if filesize else None,
            "height":     h or 0,
        })

    formats.sort(key=lambda f: f["height"], reverse=True)

    dur_str: str | None = None
    if duration:
        h2, rem = divmod(int(duration), 3600)
        m2, s2  = divmod(rem, 60)
        dur_str = f"{h2}:{m2:02d}:{s2:02d}" if h2 else f"{m2}:{s2:02d}"

    return {
        "title":        info.get("title")    or "N/A",
        "thumbnail":    info.get("thumbnail"),
        "duration":     duration,
        "duration_str": dur_str,
        "uploader":     info.get("uploader") or info.get("channel") or "N/A",
        "webpage_url":  info.get("webpage_url") or url,
        "formats":      formats,
    }


# ─────────────────────────────────────────────────────────────────────────────
# download_video — full download with progress tracking
# ─────────────────────────────────────────────────────────────────────────────

def download_video(
    url:       str,
    format_id: str | None = None,
    task_id:   str | None = None,
    save_dir:  Path | None = None,
) -> dict[str, Any]:
    """
    Download a video, merge to MP4 via FFmpeg, save to save_dir.
    """
    from utils.file_manager import get_server_download_dir

    task_id  = task_id  or str(uuid.uuid4())
    save_dir = save_dir or get_server_download_dir()
    save_dir.mkdir(parents=True, exist_ok=True)
    os_type  = get_os_type()

    log.info("download_video start  task=%s  url=%s  format=%s  dir=%s",
             task_id, url, format_id, save_dir)

    _set_progress(task_id, {
        "status": "starting", "percent": 0,
        "speed": "", "eta": "", "filesize": "",
        "filename": "", "error": None,
    })

    fmt = (
        f"{format_id}+bestaudio/best[height<={MAX_HEIGHT}]/best"
        if format_id
        else FORMAT_SELECTOR
    )

    tmp_sub  = save_dir / f".vdl_{task_id}"
    tmp_sub.mkdir(exist_ok=True)
    out_tmpl = str(tmp_sub / "%(title)s [%(resolution)s].%(ext)s")

    def _progress_hook(d: dict[str, Any]) -> None:
        st = d.get("status")
        if st == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct        = round(downloaded / total * 100, 1) if total > 0 else 0
            _set_progress(task_id, {
                "status": "downloading", "percent": pct,
                "speed": (d.get("_speed_str") or "").strip(),
                "eta": (d.get("_eta_str") or "").strip(),
                "filesize": (d.get("_total_bytes_str") or d.get("_total_bytes_estimate_str") or "").strip(),
                "total_bytes": total, "downloaded_bytes": downloaded,
                "filename": Path(d.get("filename", "")).name,
                "error": None,
            })
        elif st == "finished":
            _set_progress(task_id, {
                "status": "processing", "percent": 99,
                "speed": "", "eta": "", "filesize": "",
                "filename": Path(d.get("filename", "")).name, "error": None,
            })
        elif st == "error":
            _set_progress(task_id, {
                "status": "error", "percent": 0,
                "speed": "", "eta": "", "filesize": "",
                "filename": "", "error": "Stream error during download",
            })

    ydl_opts: dict[str, Any] = {
        **_base_ydl_opts(),
        "format":              fmt,
        "merge_output_format": "mp4",
        "outtmpl":             out_tmpl,
        "progress_hooks":      [_progress_hook],
        "ignoreerrors":        False,
        "windowsfilenames":    True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if info is None:
            raise ValueError("yt-dlp returned no info after download.")

        raw_path = Path(ydl.prepare_filename(info))
        if not raw_path.exists():
            raw_path = raw_path.with_suffix(".mp4")
        if not raw_path.exists():
            mp4s = sorted(tmp_sub.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if not mp4s:
                raise ValueError("Cannot locate the downloaded output file.")
            raw_path = mp4s[0]

        size_mb = raw_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            raw_path.unlink(missing_ok=True)
            raise ValueError(f"Output file too large ({size_mb:.0f} MB). Max: {MAX_FILE_MB:.0f} MB.")

        safe_name  = sanitize_filename(raw_path.name)
        final_path = unique_path(save_dir, safe_name)
        raw_path.rename(final_path)

        try:
            tmp_sub.rmdir()
        except OSError:
            pass

        log.info("download_video done  task=%s  file=%s  size=%.1f MB", task_id, final_path.name, size_mb)

        result = {
            "status": "done", "percent": 100,
            "speed": "", "eta": "", "filesize": f"{size_mb:.1f} MB",
            "filename": final_path.name, "filepath": str(final_path),
            "save_dir": str(save_dir), "os_type": os_type, "error": None,
        }
        _set_progress(task_id, result)
        return {"path": str(final_path), "filename": final_path.name, "save_dir": str(save_dir), "os_type": os_type}

    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        log.error("DownloadError  task=%s: %s", task_id, msg)
        _set_progress(task_id, {"status": "error", "percent": 0, "speed": "", "eta": "", "filesize": "", "filename": "", "filepath": "", "save_dir": "", "os_type": os_type, "error": msg})
        shutil.rmtree(tmp_sub, ignore_errors=True)
        raise ValueError(msg) from exc

    except TypeError as exc:
        msg = f"JS Runtime error (Node.js required): {exc}"
        log.error("TypeError task=%s: %s", task_id, msg)
        _set_progress(task_id, {"status": "error", "percent": 0, "speed": "", "eta": "", "filesize": "", "filename": "", "filepath": "", "save_dir": "", "os_type": os_type, "error": msg})
        shutil.rmtree(tmp_sub, ignore_errors=True)
        raise ValueError(msg) from exc

    except Exception as exc:
        msg = str(exc)
        log.error("Unexpected error  task=%s: %s", task_id, msg)
        _set_progress(task_id, {"status": "error", "percent": 0, "speed": "", "eta": "", "filesize": "", "filename": "", "filepath": "", "save_dir": "", "os_type": os_type, "error": msg})
        shutil.rmtree(tmp_sub, ignore_errors=True)
        raise ValueError(msg) from exc
