"""A small in-process sliding-window rate limiter.

Good enough for one web process (the Railway deployment runs one). With several processes
or replicas, move this to Redis or PostgreSQL; the interface stays the same.
"""

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Record one attempt. Returns False when the key is over its limit."""
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under abuse
                self._prune(now, window_seconds)
            return True

    def _prune(self, now: float, window: int) -> None:
        for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - window]:
            del self._hits[k]

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
