"""Tests for the Correlation Tracker."""

from __future__ import annotations

import numpy as np

from src.portfolio.correlation import CorrelationTracker


class TestCorrelationTracker:
    def test_empty_tracker(self) -> None:
        tracker = CorrelationTracker()
        matrix = tracker.get_matrix()
        assert len(matrix.symbols) == 0

    def test_single_symbol(self) -> None:
        tracker = CorrelationTracker()
        tracker.record_price("BTC-USD", 50000.0)
        tracker.record_price("BTC-USD", 50100.0)
        tracker.record_price("BTC-USD", 50200.0)
        matrix = tracker.recompute()
        # Single symbol → 1x1 matrix with 1.0
        assert len(matrix.symbols) == 1
        assert matrix.matrix[0, 0] == 1.0

    def test_two_uncorrelated_series(self) -> None:
        tracker = CorrelationTracker(min_samples=5)
        np.random.seed(42)
        base = 50000.0
        # Two independent random walks
        for i in range(50):
            tracker.record_price(
                "BTC-USD", base * np.exp(np.random.normal(0, 0.01) * (i + 1) * 0.1)
            )
            tracker.record_price(
                "ETH-USD", 3000.0 * np.exp(np.random.normal(0, 0.01) * (i + 1) * 0.1)
            )

        matrix = tracker.recompute()
        assert len(matrix.symbols) == 2
        corr = matrix.get_correlation("BTC-USD", "ETH-USD")
        assert -1.0 <= corr <= 1.0

    def test_two_perfectly_correlated(self) -> None:
        tracker = CorrelationTracker(min_samples=5)
        for i in range(30):
            price = 50000.0 + i * 100.0
            tracker.record_price("SYM-A", price)
            tracker.record_price("SYM-B", price * 0.1)  # Perfect linear

        matrix = tracker.recompute()
        corr = matrix.get_correlation("SYM-A", "SYM-B")
        assert corr > 0.95  # Nearly perfect

    def test_diversification_score(self) -> None:
        tracker = CorrelationTracker()
        # Two identical assets → low diversification score
        for i in range(30):
            price = 50000.0 + i * 100.0
            tracker.record_price("SYM-A", price)
            tracker.record_price("SYM-B", price * 0.1)

        tracker.recompute()
        score = tracker.diversification_score({"SYM-A"}, "SYM-B")
        # Highly correlated → low diversification benefit
        assert score < 0.3

    def test_diversification_score_uncorrelated(self) -> None:
        tracker = CorrelationTracker(min_samples=5)
        np.random.seed(99)
        for _i in range(30):
            tracker.record_price("SYM-A", 50000.0 + np.random.normal(0, 500))
            tracker.record_price("SYM-B", 3000.0 + np.random.normal(0, 30))

        tracker.recompute()
        score = tracker.diversification_score({"SYM-A"}, "SYM-B")
        # Uncorrelated → high diversification score
        assert score > 0.5

    def test_correlation_penalty_high(self) -> None:
        tracker = CorrelationTracker(max_correlation_threshold=0.5)
        for i in range(30):
            price = 50000.0 + i * 100.0
            tracker.record_price("SYM-A", price)
            tracker.record_price("SYM-B", price * 0.1)

        tracker.recompute()
        penalty = tracker.correlation_penalty("SYM-B", {"SYM-A"})
        # High correlation → significant penalty
        assert penalty > 0.5

    def test_correlation_penalty_low(self) -> None:
        tracker = CorrelationTracker(max_correlation_threshold=0.7)
        np.random.seed(77)
        for _i in range(30):
            tracker.record_price("SYM-A", 50000.0 + np.random.normal(0, 500))
            tracker.record_price("SYM-B", 3000.0 + np.random.normal(0, 30))

        tracker.recompute()
        penalty = tracker.correlation_penalty("SYM-B", {"SYM-A"})
        # Below threshold → no penalty
        assert penalty == 0.0

    def test_get_correlation_unknown_returns_zero(self) -> None:
        tracker = CorrelationTracker()
        corr = tracker.get_matrix().get_correlation("UNKNOWN", "ALSO_UNKNOWN")
        assert corr == 0.0
