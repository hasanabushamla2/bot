"""Tests for the Capacity Estimator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.adapters.base import NormalizedOrderBook, NormalizedOrderBookLevel
from src.portfolio.capacity import CapacityEstimator, PositionCapacity
from src.strategies.base import SignalDirection, StrategySignal


def make_book(
    mid: float = 50000.0, spread_bps: float = 2.0, depth_qty: float = 10.0
) -> NormalizedOrderBook:
    half_spread = mid * spread_bps / 10000.0 / 2.0
    bids = [
        NormalizedOrderBookLevel(price=mid - half_spread - i * 5.0, quantity=depth_qty)
        for i in range(10)
    ]
    asks = [
        NormalizedOrderBookLevel(price=mid + half_spread + i * 5.0, quantity=depth_qty)
        for i in range(10)
    ]
    return NormalizedOrderBook(
        exchange="test",
        symbol="BTC-USD",
        bids=bids,
        asks=asks,
        timestamp=datetime.now(UTC),
    )


def make_signal(required_capital: float = 5000.0, estimated_return: float = 2.0) -> StrategySignal:
    return StrategySignal(
        strategy_id="test_strat",
        symbol="BTC-USD",
        exchange="test",
        direction=SignalDirection.LONG,
        confidence=0.9,
        estimated_return=estimated_return,
        required_capital=required_capital,
        timestamp=datetime.now(UTC),
        signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )


class TestCapacityEstimator:
    def test_viable_for_small_size(self) -> None:
        estimator = CapacityEstimator(min_net_edge_bps=1.0)
        signal = make_signal(required_capital=1000.0, estimated_return=2.0)
        book = make_book()
        capacity = estimator.estimate(signal, book, volume_24h=100_000_000.0)
        assert capacity.is_viable
        assert capacity.max_efficient_size >= 1000.0

    def test_not_viable_for_large_size(self) -> None:
        estimator = CapacityEstimator(min_net_edge_bps=10.0)  # High threshold
        signal = make_signal(required_capital=1_000_000.0, estimated_return=0.1)  # Tiny edge
        book = make_book()
        capacity = estimator.estimate(signal, book, volume_24h=1_000_000.0)  # Low volume
        assert not capacity.is_viable

    def test_edge_decreases_with_size(self) -> None:
        estimator = CapacityEstimator(min_net_edge_bps=0.0)  # Allow negative
        signal = make_signal(required_capital=5000.0, estimated_return=2.0)
        book = make_book()
        capacity = estimator.estimate(signal, book, volume_24h=10_000_000.0)
        # Edge at 1k should be higher than edge at 100k
        assert capacity.capacity_1k > capacity.capacity_100k

    def test_no_book_still_works(self) -> None:
        estimator = CapacityEstimator()
        signal = make_signal(required_capital=1000.0)
        capacity = estimator.estimate(signal, None, volume_24h=1_000_000.0)
        assert isinstance(capacity, PositionCapacity)
        assert capacity.symbol == "BTC-USD"

    def test_capacity_report_aggregates(self) -> None:
        estimator = CapacityEstimator()
        signal = make_signal()
        book = make_book()
        caps = [estimator.estimate(signal, book, volume_24h=10_000_000.0) for _ in range(5)]
        report = estimator.capacity_report(caps)
        assert report["total_opportunities"] == 5
        assert "by_strategy" in report
        assert report["by_strategy"]["test_strat"]["num_opportunities"] == 5

    def test_volume_limits_max_size(self) -> None:
        estimator = CapacityEstimator(max_participation_pct=1.0)
        signal = make_signal(required_capital=100_000.0, estimated_return=5.0)
        book = make_book()
        capacity = estimator.estimate(signal, book, volume_24h=50_000.0)  # Tiny ADV
        # Max size should be bounded by participation limit
        assert capacity.max_efficient_size <= 50_000.0 * 1.0 / 100.0 + 1000.0  # ~500
