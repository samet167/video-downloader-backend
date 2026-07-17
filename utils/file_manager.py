"""
utils/file_manager.py — File & Path Utilities
===============================================
Provides:
  get_server_download_dir(custom=None) → Path
      Returns the server-side download directory (~/Downloads by default).
      Creates the directory if it doesn't exist.

  get_temp_dir()           → Path   (for intermediate yt-dlp output)
  sanitize_filename(name)  → str    (safe on all OS)
  unique_path(dir, name)   → Path   (auto-rename on collision)
  schedule_delete(path, n) → None   (background file cleanup)
"""

from __future__ import annotations

import os
import platform
import re
import threading
import time
from pathlib import Path

from utils.logger import get_logger

log = get_logger(__name__)

# ── Temp directory (intermediate yt-dlp files) ─────────────────────────────
_TMP_DIR = Path(os.getenv("TMP_DIR", "/tmp/videodl"))


def get_temp_dir() -> Path:
    """Return temp directory, creating it if it doesn't exist."""
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    return _TMP_DIR


# ── Server-side download directory ─────────────────────────────────────────

def get_server_download_dir(custom: str | None = None) -> Path:
    """
    Return the directory where completed downloads are saved on the server.

    Priority:
      1. custom argument (user-supplied path from request)
      2. DOWNLOAD_DIR environment variable
      3. ~/Downloads (OS default)

    The directory is created automatically if it does not exist.

    Args:
        custom: optional path string from the API request body

    Returns:
        Resolved, existing Path object
    """
    if custom and custom.strip():
        path = Path(custom.strip()).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        log.debug("Using custom save dir: %s", path)
        return path

    env_dir = os.getenv("DOWNLOAD_DIR", "").strip()
    if env_dir:
        path = Path(env_dir).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        log.debug("Using DOWNLOAD_DIR env: %s", path)
        return path

    # OS-specific default
    path = _os_default_downloads()
    path.mkdir(parents=True, exist_ok=True)
    log.debug("Using OS default Downloads dir: %s", path)
    return path


def _os_default_downloads() -> Path:
    """
    Return the platform's default Downloads folder.

    - Windows : C:/Users/<user>/Downloads
    - macOS   : /Users/<user>/Downloads
    - Linux   : /home/<user>/Downloads  (or ~/Downloads fallback)
    - Other   : ~/Downloads
    """
    system = platform.system().lower()
    home   = Path.home()

    if system == "windows":
        # Prefer USERPROFILE env var, fall back to home
        userprofile = os.getenv("USERPROFILE", "")
        base = Path(userprofile) if userprofile else home
        return base / "Downloads"

    if system in ("darwin", "linux"):
        return home / "Downloads"

    # Android/other — best effort
    return home / "Downloads"


def get_os_type() -> str:
    """
    Return a normalized OS identifier string.

    Returns one of: 'windows' | 'macos' | 'linux' | 'unknown'
    """
    s = platform.system().lower()
    return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(s, "unknown")


# ── Filename utilities ──────────────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    """
    Strip characters that are illegal in filenames on Windows, macOS and Linux.

    Rules:
      - Replace \\ / : * ? " < > | with underscore
      - Remove control characters (0x00–0x1f)
      - Collapse multiple spaces; strip leading/trailing dots and spaces
      - Truncate to 180 characters

    Args:
        name: raw filename (may include extension)

    Returns:
        Safe filename string, never empty
    """
    name = re.sub(r'[\\/:*?"<>|]', "_", name)   # illegal chars
    name = re.sub(r"[\x00-\x1f]", "", name)       # control chars
    name = re.sub(r"\s+", " ", name).strip(". ")   # whitespace / dots
    return name[:180] or "video"


def unique_path(directory: Path, filename: str) -> Path:
    """
    Return a Path inside *directory* that does not already exist.

    Appends a counter in parentheses when needed:
        video.mp4 → video (1).mp4 → video (2).mp4 → …

    Args:
        directory: target directory (must exist)
        filename:  desired filename (with extension)

    Returns:
        Non-existing Path
    """
    stem   = Path(filename).stem
    suffix = Path(filename).suffix
    path   = directory / filename
    n = 1
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    return path


# ── Deferred file deletion ──────────────────────────────────────────────────

def schedule_delete(path: Path, delay_seconds: int = 60) -> None:
    """
    Delete *path* in a daemon thread after *delay_seconds*.
    Silently ignored if the file is already gone.

    Args:
        path:          file to delete
        delay_seconds: wait time before deletion (default 60 s)
    """
    def _run() -> None:
        time.sleep(delay_seconds)
        try:
            path.unlink(missing_ok=True)
            log.debug("Deleted temp file: %s", path.name)
        except Exception as exc:
            log.warning("Could not delete %s: %s", path, exc)

    threading.Thread(target=_run, daemon=True, name="file-cleanup").start()
    log.debug("Scheduled deletion of '%s' in %ds", path.name, delay_seconds)
