"""Token-Bucket Rate Limiter — exchange-aware rate limiting.

Implements a thread-safe token bucket algorithm with:
- Burst support (initial tokens)
- Refill rate (tokens/sec)
- Blocking and non-blocking acquires
- Retry-after handling from 429 responses
- Per-endpoint and global counters
- Metrics export
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class RateLimitConfig:
    """Configuration for one rate-limit bucket."""

    name: str
    max_tokens: float  # Maximum tokens (burst capacity)
    refill_rate: float  # Tokens per second
    refill_interval: float = 0.1  # How often to add tokens (seconds)

    def __post_init__(self) -> None:
        if self.max_tokens <= 0:
            self.max_tokens = 1.0
        if self.refill_rate <= 0:
            self.refill_rate = 0.1


@dataclass
class RateLimitStats:
    """Exported statistics for one bucket."""

    name: str
    current_tokens: float = 0.0
    total_acquired: int = 0
    total_waited: float = 0.0  # Total seconds spent waiting
    total_rejected: int = 0
    max_wait_seconds: float = 0.0
    throttled_since: float | None = None  # Unix timestamp if throttled


class TokenBucket:
    """Single token bucket with async acquire."""

    def __init__(self, config: RateLimitConfig) -> None:
        self.config = config
        self._tokens = config.max_tokens
        self._last_refill = time.monotonic()
        self._stats = RateLimitStats(name=config.name)
        self._throttled_until: float | None = None

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    @property
    def stats(self) -> RateLimitStats:
        self._refill()
        self._stats.current_tokens = self._tokens
        return self._stats

    async def acquire(self, tokens: float = 1.0, timeout: float | None = 30.0) -> bool:
        """Acquire tokens, waiting if necessary.

        Returns True if acquired, False if timeout exceeded.
        """
        start = time.monotonic()

        while True:
            self._refill()

            # Check if throttled from 429
            if self._throttled_until and time.monotonic() < self._throttled_until:
                wait_remaining = self._throttled_until - time.monotonic()
                if timeout is not None and timeout <= 0:
                    self._stats.total_rejected += 1
                    return False
                sleep_time = min(wait_remaining, timeout or wait_remaining)
                await asyncio.sleep(sleep_time)
                if timeout is not None:
                    timeout -= sleep_time
                continue

            if self._tokens >= tokens:
                self._tokens -= tokens
                self._stats.total_acquired += 1
                elapsed = time.monotonic() - start
                self._stats.total_waited += elapsed
                if elapsed > self._stats.max_wait_seconds:
                    self._stats.max_wait_seconds = elapsed
                return True

            # Not enough tokens — wait
            needed = tokens - self._tokens
            wait_time = needed / max(self.config.refill_rate, 0.001)
            wait_time = min(wait_time, 5.0)  # Cap wait per iteration

            if timeout is not None:
                elapsed = time.monotonic() - start
                if elapsed + wait_time >= timeout:
                    self._stats.total_rejected += 1
                    return False
                # Don't reduce timeout here — outer loop handles it

            await asyncio.sleep(wait_time)
            if timeout is not None:
                timeout -= wait_time

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(
                self.config.max_tokens,
                self._tokens + elapsed * self.config.refill_rate,
            )
        self._last_refill = now

    def throttle(self, seconds: float) -> None:
        """Apply a manual throttle (e.g., after 429 response)."""
        self._throttled_until = time.monotonic() + seconds
        self._tokens = 0.0
        self._stats.throttled_since = time.monotonic()
        logger.warning("rate_limiter_throttled", name=self.config.name, seconds=seconds)


class RateLimiter:
    """Manages multiple token buckets for an exchange.

    Typical Binance public rate limits:
    - 1200 weight per minute = 20 req/s
    - 100 weight per second burst
    - Per-endpoint weights vary (1-50)
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._endpoint_weights: dict[str, dict[str, float]] = {}
        # endpoint_weights: bucket_name → {operation: weight}

    def add_bucket(self, config: RateLimitConfig) -> TokenBucket:
        bucket = TokenBucket(config)
        self._buckets[config.name] = bucket
        return bucket

    def get_bucket(self, name: str) -> TokenBucket | None:
        return self._buckets.get(name)

    def ensure_bucket(
        self, name: str, max_tokens: float = 100.0, refill_rate: float = 20.0
    ) -> TokenBucket:
        """Get or create a bucket."""
        if name not in self._buckets:
            return self.add_bucket(
                RateLimitConfig(
                    name=name,
                    max_tokens=max_tokens,
                    refill_rate=refill_rate,
                )
            )
        return self._buckets[name]

    def set_endpoint_weight(self, bucket_name: str, operation: str, weight: float) -> None:
        """Register the weight for an operation under a bucket."""
        if bucket_name not in self._endpoint_weights:
            self._endpoint_weights[bucket_name] = {}
        self._endpoint_weights[bucket_name][operation] = weight

    async def acquire(
        self, bucket_name: str, tokens: float = 1.0, timeout: float | None = 30.0
    ) -> bool:
        """Acquire tokens from a named bucket."""
        bucket = self._buckets.get(bucket_name)
        if bucket is None:
            bucket = self.add_bucket(
                RateLimitConfig(
                    name=bucket_name,
                    max_tokens=max(100.0, tokens * 5),
                    refill_rate=max(20.0, tokens),
                )
            )
        return await bucket.acquire(tokens, timeout)

    def handle_429(self, bucket_name: str, retry_after: float | None = None) -> None:
        """Handle a 429 response by throttling the bucket."""
        bucket = self._buckets.get(bucket_name)
        if bucket is None:
            return
        delay = retry_after if retry_after is not None and retry_after > 0 else 10.0
        bucket.throttle(delay)

    def get_all_stats(self) -> list[RateLimitStats]:
        return [b.stats for b in self._buckets.values()]
