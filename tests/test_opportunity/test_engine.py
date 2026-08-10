"""Tests for the Opportunity Engine — scoring and ranking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.opportunity.engine import OpportunityEngine
from src.strategies.base import SignalDirection, StrategySignal


def make_signal(
    strategy_id: str = "test_strategy",
    confidence: float = 0.8,
    estimated_return: float = 0.5,
    estimated_risk: float = 0.2,
    direction: SignalDirection = SignalDirection.LONG,
    signal_expires_at: datetime | None = None,
    **kwargs: float | str | dict | None,
) -> StrategySignal:
    """Factory for test signals."""
    if signal_expires_at is None:
        signal_expires_at = datetime.now(UTC) + timedelta(seconds=30)
    return StrategySignal(
        strategy_id=strategy_id,
        symbol="BTC-USD",
        exchange="test_exchange",
        direction=direction,
        confidence=confidence,
        estimated_return=estimated_return,
        estimated_risk=estimated_risk,
        timestamp=datetime.now(UTC),
        signal_expires_at=signal_expires_at,
        **kwargs,
    )


class TestOpportunityEvaluation:
    """Unit tests for opportunity evaluation."""

    def test_low_confidence_rejected(self) -> None:
        engine = OpportunityEngine(min_confidence=0.5)
        signal = make_signal(confidence=0.3)
        opp = engine.evaluate(signal)
        assert opp.status.value == "rejected"

    def test_high_confidence_passes_gate(self) -> None:
        engine = OpportunityEngine(min_confidence=0.5)
        signal = make_signal(confidence=0.9)
        opp = engine.evaluate(signal)
        assert opp.status.value == "ranked"

    def test_expired_signal_rejected(self) -> None:
        engine = OpportunityEngine()
        signal = make_signal(signal_expires_at=datetime.now(UTC) - timedelta(seconds=10))
        opp = engine.evaluate(signal)
        assert opp.status.value == "rejected"

    def test_net_return_below_minimum_rejected(self) -> None:
        engine = OpportunityEngine(min_net_return=1.0)
        signal = make_signal(estimated_return=0.1)  # 0.1% gross, less after fees
        opp = engine.evaluate(signal)
        assert opp.status.value == "rejected"

    def test_score_components_positive(self) -> None:
        engine = OpportunityEngine()
        signal = make_signal(estimated_return=2.0, confidence=0.9)
        opp = engine.evaluate(signal)
        # Net return should be less than gross (fees deducted)
        assert opp.score.net_return < opp.score.gross_return
        # With good signal, net should still be positive
        # (Fees ~0.2% + spread ~0.05% + slippage ~0.05% = ~0.3%)
        assert opp.score.net_return > 0

    def test_strategy_expectancy_bonus(self) -> None:
        engine = OpportunityEngine()
        engine.update_strategy_performance("test_strategy", 1.5)
        signal = make_signal(estimated_return=2.0)
        opp = engine.evaluate(signal)
        assert opp.score.strategy_expectancy_bonus > 0

    def test_batch_ranking(self) -> None:
        engine = OpportunityEngine()
        signals = [
            make_signal("s1", estimated_return=1.0, confidence=0.9),
            make_signal("s2", estimated_return=3.0, confidence=0.9),
            make_signal("s3", estimated_return=2.0, confidence=0.9),
        ]
        results = engine.evaluate_batch(signals)
        # Should be sorted by score descending
        assert len(results) == 3
        assert results[0].signal.strategy_id == "s2"  # highest return
        assert results[-1].signal.strategy_id == "s1"  # lowest return
