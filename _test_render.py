"""Simulate what happens on Render: yt-dlp WITHOUT js_runtimes configured."""
import yt_dlp

# Simulate old code behavior (no js_runtimes passed)
opts = {
    "quiet": True,
    "skip_download": True,
    "ignoreerrors": False,
    "extractor_args": {
        "youtube": {
            "player_client": ["default"],
            "formats": ["missing_pot"],
        }
    },
}

print("Testing WITHOUT js_runtimes (simulating old deployed code)...")
try:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        print("SUCCESS:", info.get("title"))
except TypeError as e:
    print("TypeError:", e)
except Exception as e:
    print("Error:", type(e).__name__, str(e)[:200])
