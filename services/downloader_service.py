"""
services/downloader_service.py — Service Layer (thin wrapper)
==============================================================
Wraps the core downloader.py engine with:
  - A custom VideoServiceError exception type
  - Task lifecycle management (start in background thread, track IDs)
  - Public API used by the route handlers

Public functions:
  get_video_info(url)                       → metadata dict
  start_download(url, format_id, save_dir)  → task_id (str)
  get_task_progress(task_id)                → progress dict | None
  remove_task(task_id)                      → None

The download runs in a daemon thread so Flask can respond immediately
with the task_id while yt-dlp works in the background.
The SSE endpoint (/api/progress/<task_id>) polls get_task_progress()
and streams updates to the browser.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
import uuid

from downloader import (
    cleanup_task,
    download_video as _download_video,
    get_progress,
    get_video_info as _get_video_info,
)
from utils.logger import get_logger

log = get_logger(__name__)


# ── Custom exception ────────────────────────────────────────────────────────

class VideoServiceError(Exception):
    """
    Raised for expected, user-facing errors:
    bad URL, geo-blocked video, duration/size limits, yt-dlp failure.
    """


# ════════════════════════════════════════════════════════════════════════════
# get_video_info
# ════════════════════════════════════════════════════════════════════════════

def get_video_info(url: str) -> dict[str, Any]:
    """
    Fetch video metadata without downloading anything.

    Args:
        url: validated video page URL (already normalized by route)

    Returns:
        {
          "title":        str,
          "thumbnail":    str | None,
          "duration":     int | None,     # seconds
          "duration_str": str | None,     # "m:ss" or "h:mm:ss"
          "uploader":     str,
          "webpage_url":  str,
          "formats": [
              {
                "format_id":  str,
                "resolution": str,        # "1280x720"
                "quality":    str,        # "720p"
                "ext":        str,
                "filesize":   int | None, # bytes
                "height":     int
              }
          ]
        }

    Raises:
        VideoServiceError: on any failure
    """
    try:
        return _get_video_info(url)
    except ValueError as exc:
        raise VideoServiceError(str(exc)) from exc
    except Exception as exc:
        log.exception("get_video_info unexpected error for %s", url)
        raise VideoServiceError(f"Unexpected error: {exc}") from exc


# ════════════════════════════════════════════════════════════════════════════
# start_download  (non-blocking — runs in background thread)
# ════════════════════════════════════════════════════════════════════════════

def start_download(
    url:       str,
    format_id: str | None = None,
    save_dir:  str | None = None,
) -> str:
    """
    Launch a download in a background thread and return its task_id immediately.

    The caller should then open an SSE stream to /api/progress/<task_id>
    to receive live updates.

    Args:
        url:       validated video URL
        format_id: yt-dlp format_id string, or None for auto-best
        save_dir:  absolute path string for save location, or None for default

    Returns:
        task_id (str) — UUID string identifying this download job
    """
    from utils.file_manager import get_server_download_dir

    task_id   = str(uuid.uuid4())
    save_path = get_server_download_dir(save_dir)

    log.info("start_download  task=%s  url=%s  format=%s  dir=%s",
             task_id, url, format_id, save_path)

    def _run() -> None:
        try:
            _download_video(
                url=url,
                format_id=format_id,
                task_id=task_id,
                save_dir=save_path,
            )
        except ValueError:
            # Progress already set to "error" inside _download_video
            pass
        except Exception as exc:
            log.exception("Unhandled error in download thread task=%s", task_id)
            # Ensure progress reflects the failure
            from downloader import _set_progress
            _set_progress(task_id, {
                "status":   "error",
                "percent":  0,
                "speed":    "",
                "eta":      "",
                "filesize": "",
                "filename": "",
                "filepath": "",
                "save_dir": str(save_path),
                "os_type":  "",
                "error":    str(exc),
            })

    thread = threading.Thread(
        target=_run,
        daemon=True,
        name=f"download-{task_id[:8]}",
    )
    thread.start()
    log.debug("Download thread started: %s", thread.name)

    return task_id


# ════════════════════════════════════════════════════════════════════════════
# Progress accessors
# ════════════════════════════════════════════════════════════════════════════

def get_task_progress(task_id: str) -> dict[str, Any] | None:
    """
    Return a copy of the current progress dict for the given task_id.

    Returns None if the task is unknown (may have been cleaned up).

    Progress dict keys:
        status    — "starting" | "downloading" | "processing" | "done" | "error"
        percent   — float 0-100
        speed     — str  e.g. "2.3 MB/s"
        eta       — str  e.g. "00:42"
        filesize  — str  e.g. "45.2 MB"
        filename  — str  (current file being written)
        filepath  — str  (final path, only when status=="done")
        save_dir  — str  (final directory, only when status=="done")
        os_type   — str  "windows"|"macos"|"linux"|"unknown"
        error     — str | None
    """
    return get_progress(task_id)


def remove_task(task_id: str) -> None:
    """
    Remove a finished/errored task from the progress store.
    Safe to call even if the task_id doesn't exist.
    """
    cleanup_task(task_id)
    log.debug("Task removed: %s", task_id)
