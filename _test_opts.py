"""Quick test to verify yt-dlp options and JS runtime config."""
from downloader import get_video_info

try:
    info = get_video_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    print("SUCCESS! Title:", info["title"])
    print("Duration:", info["duration_str"])
    print("Formats:", len(info["formats"]))
except Exception as e:
    print("ERROR:", type(e).__name__, str(e)[:300])
