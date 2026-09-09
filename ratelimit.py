"""
A small in-process rate limiter.

Deliberately not distributed. It lives in one process, so with more than one
replica each replica has its own allowance — the effective limit is
`limit x replicas`. For an invite-only tool running on a single replica that is
exact; if this ever scales out, move the counters to Postgres or Redis rather
than pretending these numbers still hold.

Its job is to stop one signed-in user from accidentally or casually hammering
Kartverket, not to withstand a determined attacker. The invite wall is what
does that.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class SlidingWindow:
    """Counts events per key over a rolling window."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, float]:
        """Record a hit. Returns (allowed, seconds_until_retry)."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= self.limit:
                return False, max(0.0, q[0] + self.window - now)
            q.append(now)

            # Opportunistic cleanup so idle keys do not accumulate forever.
            if len(self._hits) > 10_000:
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return True, 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
