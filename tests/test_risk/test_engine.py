"""Tests for the Risk Engine — independent risk management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.opportunity.engine import EvaluatedOpportunity, OpportunityScore
from src.risk.engine import RejectionReason, RiskDecision, RiskEngine
from src.strategies.base import SignalDirection, StrategySignal


def make_evaluated_opportunity(
    strategy_id: str = "test_strategy",
    symbol: str = "BTC-USD",
    required_capital: float = 500.0,
    direction: SignalDirection = SignalDirection.LONG,
    confidence: float = 0.9,
) -> EvaluatedOpportunity:
    """Factory for test opportunities."""
    signal = StrategySignal(
        strategy_id=strategy_id,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        estimated_return=2.0,
        required_capital=required_capital,
        timestamp=datetime.now(UTC),
        signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        metadata={"entry_price": 50000.0, "stop_loss_pct": 0.3},
    )
    score = OpportunityScore(
        gross_return=2.0,
        fees=0.2,
        net_return=1.5,
        fill_probability=0.95,
        final_score=1.2,
    )
    return EvaluatedOpportunity(signal=signal, score=score)


class TestRiskEngine:
    """Tests for risk evaluation."""

    def test_kill_switch_blocks_all(self) -> None:
        engine = RiskEngine()
        engine.trip_kill_switch("test")
        opp = make_evaluated_opportunity()
        result = engine.assess(opp)
        assert result.decision == RiskDecision.REJECTED
        assert result.reason == RejectionReason.KILL_SWITCH

    def test_circuit_breaker_blocks_all(self) -> None:
        engine = RiskEngine()
        engine.trip_circuit_breaker("test")
        opp = make_evaluated_opportunity()
        result = engine.assess(opp)
        assert result.decision == RiskDecision.REJECTED
        assert result.reason == RejectionReason.CIRCUIT_BREAKER

    def test_max_exposure_blocked(self) -> None:
        engine = RiskEngine()
        engine.update_state(total_exposure=9900.0)
        opp = make_evaluated_opportunity(required_capital=500.0)
        result = engine.assess(opp)
        # 9900 + 500 = 10400 > 10000 limit
        assert result.decision == RiskDecision.REJECTED
        assert result.reason == RejectionReason.MAX_EXPOSURE

    def test_healthy_opportunity_approved(self) -> None:
        engine = RiskEngine()
        opp = make_evaluated_opportunity(required_capital=500.0)
        result = engine.assess(opp)
        assert result.decision == RiskDecision.APPROVED
        assert result.max_position_size > 0

    def test_per_strategy_limit(self) -> None:
        engine = RiskEngine()
        # Fill all 10 allowed positions for one strategy
        engine._strategy_position_counts["test_strategy"] = 10
        opp = make_evaluated_opportunity()
        result = engine.assess(opp)
        assert result.decision == RiskDecision.REJECTED
        assert result.reason == RejectionReason.PER_STRATEGY_EXPOSURE

    def test_stop_loss_calculation_long(self) -> None:
        engine = RiskEngine()
        opp = make_evaluated_opportunity(direction=SignalDirection.LONG)
        opp.signal.metadata["entry_price"] = 50000.0
        result = engine.assess(opp)
        # -0.3% stop: 50000 * 0.997 = 49850
        assert result.stop_loss_price is not None
        assert result.stop_loss_price == pytest.approx(49850.0, rel=0.01)

    def test_stop_loss_calculation_short(self) -> None:
        """Short positions are rejected per SPOT-ONLY policy.
        The hard stop math still works correctly for shorts,
        but the risk engine gate rejects them before computing it."""
        engine = RiskEngine()
        opp = make_evaluated_opportunity(direction=SignalDirection.SHORT)
        opp.signal.metadata["entry_price"] = 50000.0
        result = engine.assess(opp)
        # SPOT-ONLY: short signals are rejected
        assert result.decision == RiskDecision.REJECTED
        assert result.reason is not None
        assert "spot_only" in str(result.reason.value)

    def test_reset_kill_switch(self) -> None:
        engine = RiskEngine()
        engine.trip_kill_switch("test")
        assert engine.state.kill_switch_active is True
        engine.reset_kill_switch()
        assert engine.state.kill_switch_active is False

    def test_drawdown_trips_circuit_breaker(self) -> None:
        engine = RiskEngine()
        engine.update_state(
            total_exposure=0,
            current_equity=8000.0,  # 20% drawdown from 10k initial
        )
        engine.state.peak_equity = 10000.0
        engine.state.current_drawdown_pct = 20.0
        opp = make_evaluated_opportunity()
        result = engine.assess(opp)
        assert result.decision == RiskDecision.REJECTED
        assert result.reason == RejectionReason.DRAWDOWN
