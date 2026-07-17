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
  FFMPEG_PATH      — explicit path to ffmpeg binary
  MAX_VIDEO_SECONDS — max duration in seconds (default 3600)
  MAX_FILE_MB      — max output file size in MB (default 500)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import ssl
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
# Prevents "CERTIFICATE_VERIFY_FAILED" on macOS python.org builds.
# ─────────────────────────────────────────────────────────────────────────────
_CERT_FILE = certifi.where()
os.environ.setdefault("SSL_CERT_FILE",      _CERT_FILE)
os.environ.setdefault("REQUESTS_CA_BUNDLE", _CERT_FILE)
ssl._create_default_https_context = ssl.create_default_context  # noqa: SLF001

# ─────────────────────────────────────────────────────────────────────────────
# Config from environment variables
# ─────────────────────────────────────────────────────────────────────────────
MAX_HEIGHT:   int   = 1080
MAX_DURATION: int   = int(os.environ.get("MAX_VIDEO_SECONDS", 7200))   # 2 hours
MAX_FILE_MB:  float = float(os.environ.get("MAX_FILE_MB",     500))    # 500 MB

# FFmpeg binary — set FFMPEG_PATH env var on Render or non-PATH installs
FFMPEG_PATH: str | None = os.environ.get("FFMPEG_PATH") or None

# yt-dlp format selector: best video ≤ 1080p + best audio, merged to MP4
FORMAT_SELECTOR: str = (
    f"bestvideo[height<={MAX_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]/"
    f"bestvideo[height<={MAX_HEIGHT}]+bestaudio/"
    f"best[height<={MAX_HEIGHT}]/"
    "best"
)


# ─────────────────────────────────────────────────────────────────────────────
# Thread-safe progress store
# Keys: task_id (str)
# Values: dict with keys: status, percent, speed, eta, filesize,
#         filename, filepath, save_dir, os_type, error
# ─────────────────────────────────────────────────────────────────────────────
_progress: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def get_progress(task_id: str) -> dict[str, Any] | None:
    """Return progress dict for task_id, or None if not found."""
    with _lock:
        return dict(_progress[task_id]) if task_id in _progress else None


def cleanup_task(task_id: str) -> None:
    """Remove a completed/errored task from the progress store."""
    with _lock:
        _progress.pop(task_id, None)


def _set_progress(task_id: str, data: dict[str, Any]) -> None:
    """Overwrite the entire progress entry for task_id (thread-safe)."""
    with _lock:
        _progress[task_id] = data


# ─────────────────────────────────────────────────────────────────────────────
# Quality label helper
# ─────────────────────────────────────────────────────────────────────────────

def _quality_label(height: int | None) -> str:
    """Map pixel height → human-readable label (360p / 480p / 720p / 1080p)."""
    if not height:
        return "unknown"
    for limit, label in [(360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p")]:
        if height <= limit:
            return label
    return f"{height}p"


# ─────────────────────────────────────────────────────────────────────────────
# Base yt-dlp options (shared between info and download calls)
# ─────────────────────────────────────────────────────────────────────────────

def _base_ydl_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet":              True,
        "no_warnings":        True,
        "nocheckcertificate": False,      # keep SSL verification ON
        "ssl_certificate":    _CERT_FILE,
        "socket_timeout":     30,
        "retries":            5,
        "fragment_retries":   5,
        # ── YouTube "not available on this app" fix ────────────────────
        # YouTube now requires a PO token for WEB client requests that
        # don't originate from a real browser session.  Switching the
        # extractor to the TV_EMBED client (or WEB_EMBEDDED_PLAYER as
        # fallback) bypasses this check without needing cookies.
        # References:
        #   https://github.com/yt-dlp/yt-dlp/issues/10128
        #   https://github.com/yt-dlp/yt-dlp/wiki/Extractors
        "extractor_args": {
            "youtube": {
                "player_client": ["web_embedded", "tv_embedded", "web", "android"],
            }
        },
    }
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH
    return opts


# ─────────────────────────────────────────────────────────────────────────────
# get_video_info — metadata only, no download
# ─────────────────────────────────────────────────────────────────────────────

def get_video_info(url: str) -> dict[str, Any]:
    """
    Fetch video metadata without downloading.

    Args:
        url: validated video page URL

    Returns:
        {
          title, thumbnail, duration (int|None), duration_str (str|None),
          uploader, webpage_url,
          formats: [ { format_id, resolution, quality, ext, filesize, height } ]
        }

    Raises:
        ValueError: on yt-dlp error or validation failure
    """
    opts = {
        **_base_ydl_opts(),
        "skip_download": True,
        "ignoreerrors":  False,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        raise ValueError(msg) from exc
    except Exception as exc:
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

        if vcodec == "none":      continue   # audio-only
        if h and h > MAX_HEIGHT:  continue   # above 1080p

        res = f"{w}x{h}" if w and h else (fmt.get("resolution") or "unknown")
        if res in seen:           continue
        seen.add(res)

        filesize = fmt.get("filesize") or fmt.get("filesize_approx")
        if filesize and filesize > MAX_FILE_MB * 1024 * 1024:
            continue   # skip oversized formats

        formats.append({
            "format_id":  fmt.get("format_id", ""),
            "resolution": res,
            "quality":    _quality_label(h),
            "ext":        fmt.get("ext", "mp4"),
            "filesize":   int(filesize) if filesize else None,
            "height":     h or 0,
        })

    formats.sort(key=lambda f: f["height"], reverse=True)

    # Duration string
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

    Progress is continuously written to the _progress store under task_id.
    The SSE endpoint reads this to stream updates to the browser.

    Args:
        url:       validated video URL
        format_id: yt-dlp format_id to download (None = auto best ≤ 1080p)
        task_id:   progress tracking identifier (auto-generated if None)
        save_dir:  directory to save the final file (defaults to ~/Downloads)

    Returns:
        {
          "path":     str  — absolute path of saved file
          "filename": str  — filename only
          "save_dir": str  — directory the file was saved to
          "os_type":  str  — 'windows' | 'macos' | 'linux' | 'unknown'
        }

    Raises:
        ValueError: on any download or post-processing failure
    """
    from utils.file_manager import get_server_download_dir  # avoid circular at module level

    task_id  = task_id  or str(uuid.uuid4())
    save_dir = save_dir or get_server_download_dir()
    save_dir.mkdir(parents=True, exist_ok=True)
    os_type  = get_os_type()

    log.info("download_video start  task=%s  url=%s  format=%s  dir=%s",
             task_id, url, format_id, save_dir)

    # Initialize progress entry
    _set_progress(task_id, {
        "status":   "starting",
        "percent":  0,
        "speed":    "",
        "eta":      "",
        "filesize": "",
        "filename": "",
        "error":    None,
    })

    # Format selector
    fmt = (
        f"{format_id}+bestaudio/best[height<={MAX_HEIGHT}]/best"
        if format_id
        else FORMAT_SELECTOR
    )

    # Use a temp subdirectory so we can reliably locate the output file
    tmp_sub  = save_dir / f".vdl_{task_id}"
    tmp_sub.mkdir(exist_ok=True)
    out_tmpl = str(tmp_sub / "%(title)s [%(resolution)s].%(ext)s")

    # ── Progress hook — called by yt-dlp on each chunk ────────────────
    def _progress_hook(d: dict[str, Any]) -> None:
        st = d.get("status")

        if st == "downloading":
            total      = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes") or 0
            pct        = round(downloaded / total * 100, 1) if total > 0 else 0

            _set_progress(task_id, {
                "status":   "downloading",
                "percent":  pct,
                "speed":    (d.get("_speed_str")  or "").strip(),
                "eta":      (d.get("_eta_str")    or "").strip(),
                "filesize": (
                    d.get("_total_bytes_str") or
                    d.get("_total_bytes_estimate_str") or ""
                ).strip(),
                "total_bytes": total,
                "downloaded_bytes": downloaded,
                "filename": Path(d.get("filename", "")).name,
                "error":    None,
            })

        elif st == "finished":
            _set_progress(task_id, {
                "status":   "processing",
                "percent":  99,
                "speed":    "",
                "eta":      "",
                "filesize": "",
                "filename": Path(d.get("filename", "")).name,
                "error":    None,
            })

        elif st == "error":
            _set_progress(task_id, {
                "status":   "error",
                "percent":  0,
                "speed":    "",
                "eta":      "",
                "filesize": "",
                "filename": "",
                "error":    "Stream error during download",
            })

    # ── yt-dlp options ────────────────────────────────────────────────
    ydl_opts: dict[str, Any] = {
        **_base_ydl_opts(),
        "format":              fmt,
        "merge_output_format": "mp4",
        "outtmpl":             out_tmpl,
        "progress_hooks":      [_progress_hook],
        "ignoreerrors":        False,
        "windowsfilenames":    True,       # safe filenames on all OS
        # FFmpeg post-processor: copy video stream, encode audio to AAC
        "postprocessor_args": {
            "ffmpeg_i": ["-c:v", "copy", "-c:a", "aac"],
        },
    }

    # ── Run download ──────────────────────────────────────────────────
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if info is None:
            raise ValueError("yt-dlp returned no info after download.")

        # Locate the output file
        raw_path = Path(ydl.prepare_filename(info))
        if not raw_path.exists():
            raw_path = raw_path.with_suffix(".mp4")

        if not raw_path.exists():
            # Fallback: newest MP4 in temp subdir
            mp4s = sorted(
                tmp_sub.glob("*.mp4"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not mp4s:
                raise ValueError("Cannot locate the downloaded output file.")
            raw_path = mp4s[0]

        # File size guard
        size_mb = raw_path.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            raw_path.unlink(missing_ok=True)
            raise ValueError(
                f"Output file is too large ({size_mb:.0f} MB). "
                f"Maximum allowed: {MAX_FILE_MB:.0f} MB."
            )

        # Move to final save_dir with collision-safe name
        safe_name  = sanitize_filename(raw_path.name)
        final_path = unique_path(save_dir, safe_name)
        raw_path.rename(final_path)

        # Clean up temp subdir
        try:
            tmp_sub.rmdir()
        except OSError:
            pass

        log.info("download_video done  task=%s  file=%s  size=%.1f MB",
                 task_id, final_path.name, size_mb)

        result = {
            "status":   "done",
            "percent":  100,
            "speed":    "",
            "eta":      "",
            "filesize": f"{size_mb:.1f} MB",
            "filename": final_path.name,
            "filepath": str(final_path),
            "save_dir": str(save_dir),
            "os_type":  os_type,
            "error":    None,
        }
        _set_progress(task_id, result)

        return {
            "path":     str(final_path),
            "filename": final_path.name,
            "save_dir": str(save_dir),
            "os_type":  os_type,
        }

    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"^ERROR:\s*", "", str(exc)).strip()
        log.error("DownloadError  task=%s: %s", task_id, msg)
        _set_progress(task_id, {
            "status": "error", "percent": 0,
            "speed": "", "eta": "", "filesize": "",
            "filename": "", "filepath": "", "save_dir": "", "os_type": os_type,
            "error": msg,
        })
        shutil.rmtree(tmp_sub, ignore_errors=True)
        raise ValueError(msg) from exc

    except Exception as exc:
        msg = str(exc)
        log.error("Unexpected error  task=%s: %s", task_id, msg)
        _set_progress(task_id, {
            "status": "error", "percent": 0,
            "speed": "", "eta": "", "filesize": "",
            "filename": "", "filepath": "", "save_dir": "", "os_type": os_type,
            "error": msg,
        })
        shutil.rmtree(tmp_sub, ignore_errors=True)
        raise ValueError(msg) from exc
