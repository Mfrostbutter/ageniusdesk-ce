"""In-process token-bucket rate limiting.

Used by the machine-facing surfaces that have no session to throttle: the public
API (per key) and the unauthenticated ingest endpoints (per IP). A token bucket
rather than a fixed window so a caller that has been quiet for a minute can still
burst, which is the normal shape of an n8n error handler firing a batch.

Scope: this is per-process and in-memory. AgeniusDesk CE runs as a single
uvicorn process, so that is the whole app; behind multiple replicas each replica
carries its own budget. Documented rather than solved — a shared limiter would
mean a Redis dependency this edition deliberately does not have.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    """Fixed-rate refilling bucket keyed by an arbitrary string.

    ``rate_per_min`` tokens are added per minute up to a ceiling of ``burst``
    (defaults to one full minute's allowance). ``allow`` takes one token and
    reports whether the caller is under budget.
    """

    def __init__(self, rate_per_min: int, burst: int | None = None, max_keys: int = 10000):
        self.rate_per_sec = max(rate_per_min, 0) / 60.0
        self.burst = float(burst if burst is not None else max(rate_per_min, 1))
        self.max_keys = max_keys
        self._lock = threading.Lock()
        # key -> (tokens, last_refill_ts)
        self._buckets: dict[str, tuple[float, float]] = {}

    def _evict_if_needed(self, now: float) -> None:
        """Drop fully-refilled (idle) buckets when the table grows unbounded.

        A full bucket is indistinguishable from a never-seen key, so evicting it
        costs the caller nothing. Called only when over the cap, so the common
        path stays O(1).
        """
        if len(self._buckets) <= self.max_keys:
            return
        stale = [
            k for k, (tokens, last) in self._buckets.items()
            if tokens + (now - last) * self.rate_per_sec >= self.burst
        ]
        for k in stale:
            del self._buckets[k]
        # Still over cap (every bucket active): drop the least-recently-touched.
        if len(self._buckets) > self.max_keys:
            ordered = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(self._buckets) - self.max_keys]:
                del self._buckets[k]

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """Consume ``cost`` tokens for ``key``. False when the budget is spent."""
        if self.rate_per_sec <= 0:
            return True  # rate 0 = limiter disabled
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate_per_sec)
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - cost, now)
            self._evict_if_needed(now)
            return True

    def reset(self) -> None:
        """Drop all state. Test hook."""
        with self._lock:
            self._buckets.clear()


def client_ip(request) -> str:
    """Best-effort client address for per-IP limiting.

    X-Forwarded-For is honored only when AGD_TRUST_FORWARDED_FOR is set: an
    untrusted header would let a caller mint a fresh bucket per request and
    bypass the limit entirely.
    """
    from backend.config import settings

    if settings.agd_trust_forwarded_for:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if fwd:
            return fwd
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"
