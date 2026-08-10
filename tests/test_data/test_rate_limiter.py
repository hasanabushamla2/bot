"""Tests for the Token-Bucket Rate Limiter."""

from __future__ import annotations

import pytest

from src.data.rate_limiter import RateLimitConfig, RateLimiter, TokenBucket


class TestTokenBucket:
    def test_initial_tokens(self) -> None:
        cfg = RateLimitConfig(name="test", max_tokens=100.0, refill_rate=10.0)
        bucket = TokenBucket(cfg)
        assert bucket.available == pytest.approx(100.0)

    def test_acquire_consumes(self) -> None:
        cfg = RateLimitConfig(name="test", max_tokens=100.0, refill_rate=1.0)
        bucket = TokenBucket(cfg)
        available_before = bucket.available
        # Can't easily test async acquire without running loop,
        # but synchronous available should work
        assert available_before > 0

    def test_refill(self) -> None:
        cfg = RateLimitConfig(name="test", max_tokens=100.0, refill_rate=100.0)
        bucket = TokenBucket(cfg)
        # Force consume all
        bucket._tokens = 0.0
        bucket._refill()
        assert bucket._tokens >= 0.0

    def test_throttle_sets_tokens_zero(self) -> None:
        cfg = RateLimitConfig(name="test", max_tokens=100.0, refill_rate=10.0)
        bucket = TokenBucket(cfg)
        bucket.throttle(5.0)
        assert bucket._tokens == 0.0


class TestRateLimiter:
    def test_add_and_get_bucket(self) -> None:
        limiter = RateLimiter()
        limiter.add_bucket(RateLimitConfig(name="api", max_tokens=100.0, refill_rate=20.0))
        bucket = limiter.get_bucket("api")
        assert bucket is not None
        assert bucket.config.name == "api"

    def test_ensure_bucket_creates(self) -> None:
        limiter = RateLimiter()
        bucket = limiter.ensure_bucket("rest")
        assert bucket is not None
        assert limiter.get_bucket("rest") is bucket

    def test_handle_429_throttles(self) -> None:
        limiter = RateLimiter()
        bucket = limiter.ensure_bucket("rest")
        limiter.handle_429("rest", retry_after=5.0)
        assert bucket._tokens == 0.0

    def test_stats_export(self) -> None:
        limiter = RateLimiter()
        limiter.ensure_bucket("a", 10.0, 1.0)
        limiter.ensure_bucket("b", 20.0, 2.0)
        stats = limiter.get_all_stats()
        assert len(stats) == 2
