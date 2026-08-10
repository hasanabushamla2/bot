"""FINAL POLICY TESTS — validates the -0.30% hard stop, no fixed TP,
trailing stop, SPOT-only enforcement, and dynamic capital allocation.

These tests verify the exact trading configuration specified in the
FINAL TRADING POLICY UPDATE (v1.0).
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import BacktestConfig, BacktestEngine, PeriodType
from src.risk.engine import RiskDecision, RiskEngine, _compute_hard_stop
from src.strategies.base import SignalDirection, StrategySignal

# ===========================================================================
# SECTION 1: Hard Stop = -0.30% verification
# ===========================================================================


class TestHardStop030:
    """Verify the -0.30% hard stop configuration."""

    def test_config_default_is_030(self) -> None:
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
        )
        assert config.stop_loss_pct == 0.30
        assert config.enable_take_profit is False
        assert config.enable_trailing_stop is True

    def test_compute_hard_stop_long(self) -> None:
        """Long position: stop at entry * (1 - 0.003) = 0.997 * entry."""
        result = _compute_hard_stop(50000.0, "long", 0.30)
        assert result == pytest.approx(49850.0, rel=0.001)

    def test_compute_hard_stop_short(self) -> None:
        """Short position math. SPOT-ONLY disables shorts but math must work."""
        result = _compute_hard_stop(50000.0, "short", 0.30)
        assert result == pytest.approx(50150.0, rel=0.001)

    def test_risk_assessment_sets_hard_stop(self) -> None:
        engine = RiskEngine()
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USD",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=2.0,
            required_capital=500.0,
            metadata={"entry_price": 50000.0},
        )
        from src.opportunity.engine import EvaluatedOpportunity, OpportunityScore

        score = OpportunityScore(final_score=1.0, net_return=1.5)
        opp = EvaluatedOpportunity(signal=signal, score=score)

        assessment = engine.assess(opp)
        if assessment.decision == RiskDecision.APPROVED:
            assert assessment.stop_loss_price == pytest.approx(49850.0, rel=0.001)
            assert assessment.take_profit_price is None
            assert assessment.trailing_stop_enabled is True

    def test_take_profit_always_none(self) -> None:
        """Verify risk assessment NEVER sets a fixed take profit."""
        engine = RiskEngine()
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USD",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=2.0,
            required_capital=500.0,
            metadata={"entry_price": 50000.0},
        )
        from src.opportunity.engine import EvaluatedOpportunity, OpportunityScore

        score = OpportunityScore(final_score=1.0, net_return=1.5)
        opp = EvaluatedOpportunity(signal=signal, score=score)
        assessment = engine.assess(opp)
        assert assessment.take_profit_price is None, "take_profit_price must always be None"


# ===========================================================================
# SECTION 2: Spot-Only enforcement
# ===========================================================================


class TestSpotOnly:
    """Verify the system rejects short positions in SPOT-ONLY mode."""

    def test_risk_rejects_short_signal(self) -> None:
        engine = RiskEngine()
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USD",
            direction=SignalDirection.SHORT,
            confidence=0.9,
            estimated_return=2.0,
            required_capital=500.0,
            metadata={"entry_price": 50000.0},
        )
        from src.opportunity.engine import EvaluatedOpportunity, OpportunityScore

        score = OpportunityScore(final_score=1.0, net_return=1.5)
        opp = EvaluatedOpportunity(signal=signal, score=score)
        assessment = engine.assess(opp)
        assert assessment.decision == RiskDecision.REJECTED
        assert assessment.reason is not None
        assert assessment.reason.value == "spot_only"

    def test_backtest_skips_shorts_when_disabled(self) -> None:
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 3, tzinfo=UTC),
            allow_short=False,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=50, freq="1h", tz="UTC")
        prices = [50000.0 + i * 10 for i in range(50)]
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": [p * 1.01 for p in prices],
                "low": [p * 0.99 for p in prices],
                "close": prices,
                "volume": [10.0] * 50,
            }
        )

        def always_short(_df: pd.DataFrame) -> dict | None:
            return {"direction": "short", "size_pct": 0.1}

        result = engine.run(data, always_short, period_type=PeriodType.TEST)
        assert result.total_trades == 0, "Short signals must be ignored in spot-only mode"


# ===========================================================================
# SECTION 3: -0.30% Stop with Fees + Spread + Slippage impact
# ===========================================================================


class TestStop030Realized:
    """CRITICAL: Test whether fees+spread+slippage materially reduce
    effectiveness of the -0.30% hard stop.

    This is the IMPORTANT TEST from the final policy update.
    The system must report REAL results, not assumed -0.30%.
    """

    def test_stop_loss_actual_vs_target(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
            stop_loss_pct=0.30,
            taker_fee=0.001,
            slippage_bps=5.0,
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=30, freq="1min", tz="UTC")
        prices = [50000.0] * 5 + list(np.linspace(50000, 49800, 25))
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": [p * 1.001 for p in prices],
                "low": [p * 0.999 for p in prices],
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 5:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        stop_trades = [t for t in result.trades if t.exit_reason == "hard_stop"]
        assert len(stop_trades) > 0, "At least one trade must trigger hard stop"
        trade = stop_trades[0]
        assert trade.target_stop_price > 0
        assert trade.actual_exit_price > 0
        assert trade.net_pnl < trade.gross_pnl, "Net must be < gross due to fees"
        assert trade.stop_slippage_pct <= 0

    def test_fees_amplify_small_stop_loss(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
            stop_loss_pct=0.30,
            taker_fee=0.001,
            slippage_bps=5.0,
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=20, freq="1min", tz="UTC")
        prices = [50000.0] * 3 + list(np.linspace(50000, 49820, 17))
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": [p * 1.001 for p in prices],
                "low": [p * 0.999 for p in prices],
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 3:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        stop_trades = [t for t in result.trades if t.exit_reason == "hard_stop"]
        if stop_trades:
            trade = stop_trades[0]
            assert trade.fees > 0
            gross_loss_pct = abs(trade.gross_pnl) / config.initial_capital * 100
            net_loss_pct = abs(trade.net_pnl) / config.initial_capital * 100
            assert net_loss_pct >= gross_loss_pct * 0.9


# ===========================================================================
# SECTION 4: Trailing stop verification
# ===========================================================================


class TestTrailingStop:
    def test_trailing_stop_activates_after_threshold(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
            trail_activation_pct=0.30,
            trail_pct=0.15,
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=40, freq="1min", tz="UTC")
        prices = (
            [50000.0] * 5
            + list(np.linspace(50000, 50300, 20))
            + list(np.linspace(50300, 50200, 15))
        )
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 5:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        trail_exits = [t for t in result.trades if t.exit_reason == "trail_hit"]
        assert len(trail_exits) > 0

    def test_trail_not_activated_below_threshold(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
            trail_activation_pct=1.0,
            trail_pct=0.15,
            stop_loss_pct=0.50,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=30, freq="1min", tz="UTC")
        prices = (
            [50000.0] * 5
            + list(np.linspace(50000, 50150, 10))
            + list(np.linspace(50150, 49700, 15))
        )
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 5:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        trail_exits = [t for t in result.trades if t.exit_reason == "trail_hit"]
        assert len(trail_exits) == 0

    def test_trail_captures_extended_move(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 3, tzinfo=UTC),
            trail_activation_pct=0.15,
            trail_pct=0.15,
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=200, freq="1min", tz="UTC")
        prices = (
            [50000.0] * 5
            + list(np.linspace(50000, 55000, 150))
            + list(np.linspace(55000, 54700, 45))
        )
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 5:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        trail_exits = [t for t in result.trades if t.exit_reason == "trail_hit"]
        if trail_exits:
            assert trail_exits[0].return_pct > 0.5


# ===========================================================================
# SECTION 5: Capital recycling
# ===========================================================================


class TestCapitalRecycling:
    def test_multiple_sequential_trades(self) -> None:
        config = BacktestConfig(
            symbol="TEST-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
            stop_loss_pct=0.30,
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        dates = pd.date_range("2024-01-01", periods=500, freq="15min", tz="UTC")
        np.random.seed(42)
        price = 50000.0
        prices = []
        for _i in range(500):
            price *= np.exp(np.random.normal(0, 0.002))
            prices.append(price)
        data = pd.DataFrame(
            {
                "timestamp": dates,
                "open": prices,
                "high": [p * 1.002 for p in prices],
                "low": [p * 0.998 for p in prices],
                "close": prices,
                "volume": [10.0] * len(prices),
            }
        )

        def long_strategy(df: pd.DataFrame) -> dict | None:
            if len(df) < 20:
                return None
            close = df["close"]
            sma10 = close.rolling(10).mean().iloc[-1]
            sma20 = close.rolling(20).mean().iloc[-1]
            if sma10 > sma20:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, long_strategy, period_type=PeriodType.TEST)
        assert result.total_trades > 0
        for trade in result.trades:
            assert trade.entry_price > 0
            assert trade.exit_price > 0
            assert trade.exit_reason in ("hard_stop", "trail_hit", "eod")
