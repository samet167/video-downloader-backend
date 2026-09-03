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
    found = shutil.which("ffmpeg")
    if found:
        return found
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if Path(p).exists():
            return p
    return None


def _find_nodejs() -> str | None:
    """Return path to Node.js binary, or None if not available."""
    found = shutil.which("node")
    if found:
        return found
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
    found = shutil.which("deno")
    if found:
        return found
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
# Mobile-friendly: prioritize H.264 baseline/main profile for better compatibility
FORMAT_SELECTOR: str = (
    f"bestvideo[height<={MAX_HEIGHT}][ext=mp4][vcodec~='avc1']+bestaudio[ext=m4a]/"
    f"bestvideo[height<={MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
    f"bestvideo[height<={MAX_HEIGHT}]+bestaudio[ext=m4a]/"
    f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/"
    f"best[height<={MAX_HEIGHT}][ext=mp4]/"
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


def _base_ydl_opts(url: str = "") -> dict[str, Any]:
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

        # ── Misc ──────────────────────────────────────────────────────────
        "geo_bypass":         True,
        "no_check_formats":   True,
        "noplaylist":         True,
    }

    if RESOLVED_FFMPEG:
        opts["ffmpeg_location"] = RESOLVED_FFMPEG

    # ── Proxy (Residential Proxy bypasses Datacenter bot detection) ─────────
    proxy_url = os.environ.get("YTDL_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_url and not any(bad in proxy_url for bad in ["host:port", "webshare.io"]):
        opts["proxy"] = proxy_url
        clean_proxy = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
        log.info("Using Proxy for yt-dlp: %s", clean_proxy)

    # ── YouTube Cookie file (bypass bot detection on datacenter IPs) ───────
    # Only attach cookies if YouTube to prevent breaking TikTok, Instagram, etc.
    is_youtube = not url or any(yt in url for yt in ["youtube.com", "youtu.be"])
    if is_youtube:
        cookie_file = os.environ.get("YOUTUBE_COOKIE_FILE")
        if not cookie_file or not Path(cookie_file).is_file():
            cookie_file = str(Path(__file__).parent / "cookies.txt")
            
        has_cookie = bool(cookie_file and Path(cookie_file).is_file())
        if has_cookie:
            opts["cookiefile"] = cookie_file
            log.info("Using cookie file for YouTube: %s", cookie_file)
        else:
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["android", "ios"],
                    "player_skip":   ["web", "configs"],
                }
            }
            log.info("Running anonymous mobile extraction for YouTube.")
    else:
        log.info("Skipping YouTube cookies for non-YouTube platform.")

    # ── JS Runtime configuration ──────────────────────────────────────────
    # yt-dlp automatically discovers Node.js / Deno from system PATH.
    if RESOLVED_DENO:
        opts["js_runtimes"] = {"deno": {"path": RESOLVED_DENO}}
    elif RESOLVED_NODEJS:
        opts["js_runtimes"] = {"node": {"path": RESOLVED_NODEJS}}

    return opts


# ─────────────────────────────────────────────────────────────────────────────
# get_video_info — metadata only, no download
# ─────────────────────────────────────────────────────────────────────────────
# TikTok Extractor Helper
# ─────────────────────────────────────────────────────────────────────────────

import hashlib

def _url_cache_path(url: str) -> Path:
    clean = re.sub(r"\?.*$", "", url).strip()
    h = hashlib.sha256(clean.encode()).hexdigest()[:16]
    return Path("/tmp/videodl") / f"tt_cache_{h}.mp4"


def _get_tiktok_api_fallback(clean_url: str, cache_path: Path) -> dict[str, Any] | None:
    """Fallback TikTok downloader using direct mobile stream API."""
    try:
        import urllib.request
        import json
        req_url = f"https://www.tikwm.com/api/?url={clean_url}"
        req = urllib.request.Request(req_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == 0 and data.get("data"):
            d = data["data"]
            play_url = d.get("play") or d.get("wmplay")
            if play_url:
                v_req = urllib.request.Request(play_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
                with urllib.request.urlopen(v_req, timeout=25) as vr, open(cache_path, "wb") as f:
                    f.write(vr.read())
            file_size = cache_path.stat().st_size if cache_path.is_file() else (d.get("size") or 0)
            duration = int(d.get("duration") or 0)
            dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else None
            return {
                "title": d.get("title") or "TikTok Video",
                "thumbnail": d.get("cover") or "",
                "duration": duration,
                "duration_str": dur_str,
                "uploader": d.get("author", {}).get("nickname") or "TikTok",
                "webpage_url": clean_url,
                "formats": [
                    {
                        "format_id": "best",
                        "resolution": "720x1280",
                        "quality": "HD Original",
                        "ext": "mp4",
                        "filesize": file_size,
                        "height": 1080,
                    }
                ],
                "cached_file": str(cache_path) if cache_path.is_file() else None,
            }
    except Exception as exc:
        log.warning("_get_tiktok_api_fallback failed: %s", exc)
        return None


def _get_tiktok_info(url: str) -> dict[str, Any] | None:
    """Extract TikTok video metadata & pre-cache video using yt-dlp chrome impersonation with API fallback."""
    try:
        import subprocess
        import json
        import shutil
        import sys

        clean_url = re.sub(r"\?.*$", "", url)
        # Find yt-dlp binary (virtualenv or system)
        ytdlp_bin = sys.executable.replace("python3", "yt-dlp").replace("python", "yt-dlp")
        if not Path(ytdlp_bin).is_file():
            ytdlp_bin = shutil.which("yt-dlp") or "yt-dlp"

        cache_path = _url_cache_path(clean_url)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out_tmpl = str(cache_path.parent / f"{cache_path.stem}.%(ext)s")

        cmd = [
            ytdlp_bin,
            "--impersonate", "chrome",
            "--print-json",
            "-o", out_tmpl,
            "--no-playlist",
            clean_url
        ]
        proxy_url = os.environ.get("YTDL_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        log.info("_get_tiktok_info: fetching & caching via yt-dlp chrome impersonation: %s", clean_url)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0 or not res.stdout.strip():
            log.warning("_get_tiktok_info yt-dlp failed with code %s. Trying API fallback...", res.returncode)
            return _get_tiktok_api_fallback(clean_url, cache_path)

        lines = [l for l in res.stdout.split("\n") if l.strip().startswith("{")]
        if not lines:
            return _get_tiktok_api_fallback(clean_url, cache_path)
        d = json.loads(lines[0])

        title = d.get("title") or "TikTok Video"
        uploader = d.get("uploader") or d.get("creator") or "TikTok"
        duration = int(d.get("duration") or 0)
        dur_str = f"{duration // 60}:{duration % 60:02d}" if duration else None
        thumbnail = d.get("thumbnail") or ""

        # Normalize cached file name if needed
        generated = list(cache_path.parent.glob(f"{cache_path.stem}.*"))
        if generated and generated[0] != cache_path:
            generated[0].rename(cache_path)

        file_size = cache_path.stat().st_size if cache_path.is_file() else (d.get("filesize") or 0)

        formats = [
            {
                "format_id": "best",
                "resolution": f"{d.get('width', 720)}x{d.get('height', 1280)}",
                "quality": "HD Original",
                "ext": "mp4",
                "filesize": file_size,
                "height": d.get("height", 1080),
            }
        ]

        return {
            "title": title,
            "thumbnail": thumbnail,
            "duration": duration,
            "duration_str": dur_str,
            "uploader": uploader,
            "webpage_url": clean_url,
            "formats": formats,
            "cached_file": str(cache_path) if cache_path.is_file() else None,
        }
    except Exception as exc:
        log.warning("_get_tiktok_info error: %s", exc)
        return _get_tiktok_api_fallback(clean_url, cache_path)


def get_video_info(url: str) -> dict[str, Any]:
    """
    Fetch video metadata without downloading.

    Raises:
        ValueError: on yt-dlp error or validation failure
    """
    if "tiktok.com" in url:
        tt_info = _get_tiktok_info(url)
        if tt_info:
            log.info("get_video_info: successfully resolved TikTok URL: %s", tt_info["title"])
            return tt_info
        raise ValueError("TikTok is temporarily rate limiting this video. Please try again in 5 seconds.")

    opts = {
        **_base_ydl_opts(url),
        "skip_download": True,
        "ignoreerrors":  False,
    }

    log.info("get_video_info: url=%s", url)

    info = None
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        log.warning("get_video_info initial attempt failed (%s). Retrying with mobile stream...", msg)
        fallback_opts = dict(opts)
        fallback_opts.pop("cookiefile", None)
        fallback_opts.pop("proxy", None)
        fallback_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios"],
                "player_skip":   ["web", "configs"],
            }
        }
        try:
            with yt_dlp.YoutubeDL(fallback_opts) as ydl_fb:
                info = ydl_fb.extract_info(url, download=False)
        except Exception as fb_exc:
            log.error("Fallback extraction also failed: %s", fb_exc)
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

    if "tiktok.com" in url or "instagram.com" in url:
        fmt = "best"
    elif format_id:
        fmt = f"{format_id}+bestaudio/best[height<={MAX_HEIGHT}]/best"
    else:
        fmt = FORMAT_SELECTOR

    if "tiktok.com" in url:
        clean_url = re.sub(r"\?.*$", "", url)
        tmp_sub = save_dir / f".vdl_{task_id}"
        tmp_sub.mkdir(exist_ok=True)
        out_tmpl = str(tmp_sub / "%(title)s [%(resolution)s].%(ext)s")
        
        _set_progress(task_id, {
            "status": "downloading", "percent": 30,
            "speed": "2 MB/s", "eta": "1s", "filesize": "",
            "filename": "", "filepath": "",
            "save_dir": str(save_dir), "os_type": os_type, "error": None,
        })
        
        import subprocess, sys, shutil
        ytdlp_bin = sys.executable.replace("python3", "yt-dlp").replace("python", "yt-dlp")
        if not Path(ytdlp_bin).is_file():
            ytdlp_bin = shutil.which("yt-dlp") or "yt-dlp"

        cmd = [
            ytdlp_bin,
            "--impersonate", "chrome",
            "-f", "best",
            "-o", out_tmpl,
            "--no-playlist",
            clean_url
        ]
        log.info("download_video: downloading TikTok via yt-dlp chrome impersonation: %s", clean_url)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            log.error("TikTok download subprocess error: %s", res.stderr)
            raise ValueError(f"TikTok download failed: {res.stderr[:200]}")
            
        mp4s = sorted(tmp_sub.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mp4s:
            raise ValueError("No video file generated for TikTok download.")
            
        raw_path = mp4s[0]
        final_path = unique_path(save_dir, raw_path.name)
        shutil.move(str(raw_path), str(final_path))
        shutil.rmtree(tmp_sub, ignore_errors=True)
        
        size_mb = final_path.stat().st_size / (1024 * 1024)
        result = {
            "status": "done", "percent": 100,
            "speed": "", "eta": "", "filesize": f"{size_mb:.1f} MB",
            "filename": final_path.name, "filepath": str(final_path),
            "save_dir": str(save_dir), "os_type": os_type, "error": None,
        }
        _set_progress(task_id, result)
        return {"path": str(final_path), "filename": final_path.name, "save_dir": str(save_dir), "os_type": os_type}

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
        **_base_ydl_opts(url),
        "format":              fmt,
        "merge_output_format": "mp4",
        "outtmpl":             out_tmpl,
        "progress_hooks":      [_progress_hook],
        "ignoreerrors":        False,
        "windowsfilenames":    True,
        # Mobile-friendly FFmpeg options
        "postprocessors": [
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            },
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        log.warning("download_video encountered issue (%s). Retrying with pure mobile stream...", msg)
        fb_opts = dict(ydl_opts)
        fb_opts.pop("cookiefile", None)
        fb_opts.pop("proxy", None)
        fb_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios"],
                "player_skip":   ["web", "configs"],
            }
        }
        try:
            with yt_dlp.YoutubeDL(fb_opts) as ydl_fb:
                info = ydl_fb.extract_info(url, download=True)
        except Exception as fb_exc:
            log.error("Fallback download also failed: %s", fb_exc)
            raise ValueError(msg) from exc

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
