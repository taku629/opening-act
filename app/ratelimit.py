"""Tiny per-IP sliding-window rate limiter for the public demo (in-memory)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        if self.per_hour <= 0:
            return True, 0
        now = time.time()
        q = self.hits[key]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return False, int(3600 - (now - q[0])) + 1
        q.append(now)
        if len(self.hits) > 10000:
            for k in [k for k, v in self.hits.items() if not v][:5000]:
                self.hits.pop(k, None)
        return True, 0
