"""
gunicorn.conf.py — Gunicorn Production Configuration
=====================================================
Used by Render's start command:
    gunicorn -c gunicorn.conf.py app:app
"""

import os

# ── Workers ───────────────────────────────────────────────────────────
# Use "gthread" worker (threaded) instead of "gevent" to avoid
# gevent monkey-patching breaking subprocess.Popen (which yt-dlp needs
# to spawn Node.js/Deno for YouTube JS challenges).
workers     = int(os.environ.get("WEB_CONCURRENCY", 1))
worker_class = "gthread"          # threaded worker — no monkey-patching issues
threads      = int(os.environ.get("GUNICORN_THREADS", 4))  # threads per worker

# ── Binding ───────────────────────────────────────────────────────────
# Render injects PORT automatically.
port = os.environ.get("PORT", "10000")
bind = f"0.0.0.0:{port}"

# ── Timeouts ─────────────────────────────────────────────────────────
# Download can take a while — set generous timeout.
timeout        = 300    # 5 minutes per request
keepalive      = 5
graceful_timeout = 30

# ── Logging ───────────────────────────────────────────────────────────
accesslog  = "-"    # stdout → captured by Render logs
errorlog   = "-"    # stderr → captured by Render logs
loglevel   = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Process name ─────────────────────────────────────────────────────
proc_name = "videodl"