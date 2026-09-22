"""In-memory per-worker sliding-window rate limiter.

Keyed by API key uid. A single Python dict holds one deque of monotonic
timestamps per uid; each check trims entries older than 60s, rejects if
the window is full, otherwise appends.

Caveats:
  - Per-worker only. With N gunicorn/Odoo workers the effective limit is
    N * limit_per_minute. For single-worker dev this is the exact limit.
  - No cross-restart persistence (intentional — restart clears counters).
"""
import threading
import time
from collections import defaultdict, deque


_WINDOW = 60.0  # seconds
_LOCK = threading.Lock()
_BUCKETS: dict[int, deque] = defaultdict(deque)


def check_and_record(uid, limit_per_minute):
    """Return True if the call is allowed, False if rate-limited.

    limit_per_minute <= 0 disables the limiter (always allows).
    """
    if not limit_per_minute or limit_per_minute <= 0:
        return True
    now = time.monotonic()
    cutoff = now - _WINDOW
    with _LOCK:
        bucket = _BUCKETS[uid]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit_per_minute:
            return False
        bucket.append(now)
        return True


def reset():
    """Testing hook — clear all buckets."""
    with _LOCK:
        _BUCKETS.clear()
