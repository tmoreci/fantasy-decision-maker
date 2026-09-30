"""Just enough access control for a site shared with one fantasy league.

A single shared passcode, stored as an HMAC in a cookie so the passcode itself never
sits in the browser, plus a per-IP rate limit so a leaked link can't run up the
TypeSafe bill.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque

ACCESS_COOKIE = "fdm_access"


def access_token(passcode: str) -> str:
    """Changing the passcode changes the token, which signs everyone out."""
    return hmac.new(passcode.encode(), b"fantasy-decision-maker/access/v1", hashlib.sha256).hexdigest()


def token_is_valid(candidate: str | None, passcode: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, access_token(passcode))


def passcode_is_valid(candidate: str | None, passcode: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate.strip().encode(), passcode.encode())


class RateLimiter:
    """Sliding-window limit per key. In memory, so it resets on restart; fine for one small server."""

    def __init__(self, limit: int, window_seconds: float = 3600.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True
