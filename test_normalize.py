"""Test normalize_url — python3 test_normalize.py"""
import sys
sys.path.insert(0, ".")
from routes.info import normalize_url

tests = [
    # (input, expected_to_contain, should_not_contain)
    ("https://youtu.be/9fwGEE46Vbc?si=qFBwOuVw5X7r8ei4",
     "youtube.com/watch?v=9fwGEE46Vbc", "si="),

    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=abc123&utm_source=share",
     "v=dQw4w9WgXcQ", "si="),

    ("https://youtu.be/abc123",
     "youtube.com/watch?v=abc123", None),

    ("https://www.tiktok.com/@user/video/123?utm_source=share&_r=app",
     "/video/123", "utm_source"),

    ("https://vimeo.com/123456789",
     "vimeo.com/123456789", None),
]

all_ok = True
for url, must_have, must_not_have in tests:
    result = normalize_url(url)
    ok1 = must_have in result
    ok2 = (must_not_have not in result) if must_not_have else True
    status = "✔" if (ok1 and ok2) else "✘"
    if not (ok1 and ok2):
        all_ok = False
    print(f"{status} {url[:55]:<55}")
    print(f"    → {result}")
    if not ok1:
        print(f"    MISSING: '{must_have}'")
    if not ok2:
        print(f"    STILL HAS: '{must_not_have}'")

print()
print("All tests passed ✔" if all_ok else "Some tests FAILED ✘")
sys.exit(0 if all_ok else 1)
