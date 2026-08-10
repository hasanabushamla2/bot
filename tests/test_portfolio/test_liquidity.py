"""Tests for the Liquidity Analyzer."""

from __future__ import annotations

from datetime import UTC

import pytest

from src.adapters.base import NormalizedOrderBook, NormalizedOrderBookLevel
from src.portfolio.liquidity import LiquidityAnalyzer


def make_book(
    mid: float = 50000.0,
    spread_bps: float = 2.0,
    depth_qty: float = 10.0,
    num_levels: int = 10,
) -> NormalizedOrderBook:
    """Build a synthetic order book."""
    half_spread = mid * spread_bps / 10000.0 / 2.0
    bids = [
        NormalizedOrderBookLevel(price=mid - half_spread - i * 5.0, quantity=depth_qty)
        for i in range(num_levels)
    ]
    asks = [
        NormalizedOrderBookLevel(price=mid + half_spread + i * 5.0, quantity=depth_qty)
        for i in range(num_levels)
    ]
    from datetime import datetime

    return NormalizedOrderBook(
        exchange="test",
        symbol="BTC-USD",
        bids=bids,
        asks=asks,
        timestamp=datetime.now(UTC),
    )


class TestLiquidityAnalyzer:
    def test_analyze_tight_spread(self) -> None:
        analyzer = LiquidityAnalyzer()
        book = make_book(mid=50000.0, spread_bps=2.0)
        metrics = analyzer.analyze(book, volume_24h=50_000_000.0)
        # 2bps spread → ~0.02% spread_pct
        assert metrics.spread_pct == pytest.approx(0.02, rel=0.15)
        assert metrics.liquidity_score > 0.5

    def test_analyze_wide_spread(self) -> None:
        analyzer = LiquidityAnalyzer()
        book = make_book(mid=50000.0, spread_bps=50.0)
        metrics = analyzer.analyze(book, volume_24h=50_000_000.0)
        # 50bps spread → ~0.5% spread_pct
        assert metrics.spread_pct == pytest.approx(0.5, rel=0.15)
        assert metrics.liquidity_score < 0.5

    def test_analyze_high_volume(self) -> None:
        analyzer = LiquidityAnalyzer()
        book = make_book(mid=50000.0, spread_bps=2.0)
        metrics = analyzer.analyze(book, volume_24h=1_000_000_000.0)
        # High volume → high liquidity score
        assert metrics.liquidity_score > 0.7

    def test_no_order_book_returns_zero(self) -> None:
        analyzer = LiquidityAnalyzer()
        metrics = analyzer.analyze(None, volume_24h=0.0)
        assert metrics.liquidity_score == 0.0
        assert metrics.max_efficient_notional == 0.0

    def test_empty_book_returns_zero(self) -> None:
        from datetime import datetime

        analyzer = LiquidityAnalyzer()
        empty_book = NormalizedOrderBook(
            exchange="test",
            symbol="BTC-USD",
            bids=[],
            asks=[],
            timestamp=datetime.now(UTC),
        )
        metrics = analyzer.analyze(empty_book, volume_24h=0.0)
        assert metrics.liquidity_score == 0.0

    def test_max_efficient_notional_grows_with_volume(self) -> None:
        analyzer = LiquidityAnalyzer(max_impact_threshold_bps=10.0)
        book = make_book(mid=50000.0, spread_bps=2.0)

        low_vol = analyzer.analyze(book, volume_24h=1_000_000.0)
        high_vol = analyzer.analyze(book, volume_24h=100_000_000.0)

        assert high_vol.max_efficient_notional > low_vol.max_efficient_notional

    def test_impact_estimates_increase_with_size(self) -> None:
        analyzer = LiquidityAnalyzer()
        book = make_book(mid=50000.0, spread_bps=2.0)
        metrics = analyzer.analyze(book, volume_24h=10_000_000.0)
        # Larger notionals → higher impact
        assert metrics.impact_1k <= metrics.impact_10k
        assert metrics.impact_10k <= metrics.impact_50k

    def test_depth_computation(self) -> None:
        analyzer = LiquidityAnalyzer()
        book = make_book(mid=50000.0, spread_bps=2.0, depth_qty=5.0, num_levels=20)
        metrics = analyzer.analyze(book, volume_24h=10_000_000.0)
        # Depth at 10bps should accumulate multiple levels
        assert metrics.depth_10bps > 0
        # Wider depth should be larger
        assert metrics.depth_10bps >= metrics.depth_1bps

    def test_spread_zero_handled(self) -> None:
        """Zero or negative spread should not crash."""
        analyzer = LiquidityAnalyzer()

        # Bid > Ask = invalid book
        from datetime import datetime

        bad_book = NormalizedOrderBook(
            exchange="test",
            symbol="BTC-USD",
            bids=[NormalizedOrderBookLevel(price=50001.0, quantity=1.0)],
            asks=[NormalizedOrderBookLevel(price=50000.0, quantity=1.0)],
            timestamp=datetime.now(UTC),
        )
        metrics = analyzer.analyze(bad_book, volume_24h=0.0)
        assert metrics.liquidity_score == 0.0
