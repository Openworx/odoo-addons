"""In-memory auth-failure throttle keyed by client IP.

After N failed auth attempts inside a rolling window, further requests from
that IP are rejected with 429 without even attempting auth. This defangs
credential-stuffing and scope-brute attempts.

Per-worker only (no cross-worker coordination): with N workers an attacker
gets at most N * THRESHOLD attempts before the first worker blocks. Good
enough for IP-level spam; combine with fail2ban on the reverse proxy for
real isolation.
"""
import threading
import time
from collections import defaultdict, deque


WINDOW_SECONDS = 60.0
THRESHOLD = 10
MAX_TRACKED_IPS = 10000

_LOCK = threading.Lock()
_FAILURES: dict[str, deque] = defaultdict(deque)


def _trim(bucket, cutoff):
    while bucket and bucket[0] < cutoff:
        bucket.popleft()


def is_throttled(client_ip):
    if not client_ip:
        return False
    now = time.monotonic()
    cutoff = now - WINDOW_SECONDS
    with _LOCK:
        bucket = _FAILURES.get(client_ip)
        if not bucket:
            return False
        _trim(bucket, cutoff)
        if not bucket:
            _FAILURES.pop(client_ip, None)
            return False
        return len(bucket) >= THRESHOLD


def register_failure(client_ip):
    if not client_ip:
        return
    now = time.monotonic()
    with _LOCK:
        # Bound memory: if we're at capacity, evict entries whose bucket is
        # fully expired before adding a new one.
        if len(_FAILURES) >= MAX_TRACKED_IPS and client_ip not in _FAILURES:
            cutoff = now - WINDOW_SECONDS
            for ip, bucket in list(_FAILURES.items()):
                _trim(bucket, cutoff)
                if not bucket:
                    _FAILURES.pop(ip, None)
        _FAILURES[client_ip].append(now)


def reset():
    """Testing hook — clear all buckets."""
    with _LOCK:
        _FAILURES.clear()
