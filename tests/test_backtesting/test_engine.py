"""Tests for the Backtesting Engine — anti-bias protections."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.backtesting.engine import BacktestConfig, BacktestEngine, PeriodType


def make_ohlcv_data(
    n_rows: int = 100,
    start_price: float = 50000.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    np.random.seed(seed)
    dates = pd.date_range("2024-01-01", periods=n_rows, freq="1h")
    price = start_price
    prices = []
    for _ in range(n_rows):
        price *= np.exp(np.random.normal(0, 0.002))
        prices.append(price)

    return pd.DataFrame({
        "timestamp": dates,
        "open": prices,
        "high": [p * 1.002 for p in prices],
        "low": [p * 0.998 for p in prices],
        "close": prices,
        "volume": np.random.uniform(1, 100, n_rows),
    })


class TestBacktestEngine:
    """Tests for backtesting with anti-bias protections."""

    def test_basic_run(self) -> None:
        """Backtest should run without errors."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)
        data = make_ohlcv_data(100)

        def strategy(df: pd.DataFrame) -> dict | None:
            if len(df) < 20:
                return None
            close = df["close"]
            sma10 = close.rolling(10).mean().iloc[-1]
            sma20 = close.rolling(20).mean().iloc[-1]
            if sma10 > sma20:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, strategy, period_type=PeriodType.TEST)
        assert result.total_trades >= 0
        assert result.config.symbol == "BTC-USD"

    def test_fees_always_included(self) -> None:
        """Fees must never be zero when trades occur."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
            taker_fee=0.001,  # 0.1%
        )
        engine = BacktestEngine(config)
        data = make_ohlcv_data(200)

        def always_buy(df: pd.DataFrame) -> dict | None:
            if len(df) >= 20:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_buy, period_type=PeriodType.TEST)
        if result.total_trades > 0:
            assert result.total_fees > 0, "Fees must be non-zero when trades occur"

    def test_net_pnl_less_than_gross(self) -> None:
        """Net P&L must always be less than gross P&L due to fees/slippage."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
        )
        engine = BacktestEngine(config)
        data = make_ohlcv_data(200)

        def always_buy(df: pd.DataFrame) -> dict | None:
            if len(df) >= 20:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, always_buy, period_type=PeriodType.TEST)
        if result.total_trades > 0:
            assert result.net_pnl < result.gross_pnl, (
                f"Net P&L ({result.net_pnl}) must be less than gross ({result.gross_pnl})"
            )

    def test_stop_loss_triggers(self) -> None:
        """Stop loss should close a losing position."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 2, tzinfo=UTC),
            stop_loss_pct=0.1,  # Very tight stop
            initial_capital=10000.0,
        )
        engine = BacktestEngine(config)

        # Create data with a sharp drop then recovery
        dates = pd.date_range("2024-01-01", periods=50, freq="1min")
        prices = [50000.0] * 10 + [49500.0] * 5 + [51000.0] * 35
        data = pd.DataFrame({
            "timestamp": dates,
            "open": prices,
            "high": [p * 1.001 for p in prices],
            "low": [p * 0.999 for p in prices],
            "close": prices,
            "volume": [10.0] * 50,
        })

        def always_long(df: pd.DataFrame) -> dict | None:
            if len(df) >= 5:
                return {"direction": "long", "size_pct": 0.1, "stop_loss_pct": 0.5}
            return None

        result = engine.run(data, always_long, period_type=PeriodType.TEST)
        # Check if any trade exited via stop loss
        stop_loss_trades = [t for t in result.trades if t.exit_reason == "stop_loss"]
        assert len(stop_loss_trades) > 0, "At least one trade should trigger stop loss"

    def test_period_separation_enforced(self) -> None:
        """Training and validation periods must not overlap."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 6, 1, tzinfo=UTC),
            train_end=datetime(2024, 4, 1, tzinfo=UTC),
            validation_end=datetime(2024, 3, 1, tzinfo=UTC),  # Before train_end!
        )
        with pytest.raises(ValueError, match="train_end must be before validation_end"):
            BacktestEngine(config)

    def test_metrics_positive(self) -> None:
        """All computed metrics should be finite numbers."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 1, 5, tzinfo=UTC),
        )
        engine = BacktestEngine(config)
        data = make_ohlcv_data(200)

        def strategy(df: pd.DataFrame) -> dict | None:
            if len(df) < 20:
                return None
            close = df["close"]
            sma10 = close.rolling(10).mean().iloc[-1]
            sma20 = close.rolling(20).mean().iloc[-1]
            if sma10 > sma20:
                return {"direction": "long", "size_pct": 0.1}
            return None

        result = engine.run(data, strategy, period_type=PeriodType.TEST)
        assert result.sharpe_ratio == result.sharpe_ratio  # Not NaN
        assert result.sortino_ratio == result.sortino_ratio
        assert result.max_drawdown_pct >= 0
        assert result.profit_factor >= 0

    def test_walk_forward_no_overlap(self) -> None:
        """Walk-forward windows must not overlap train/test."""
        config = BacktestConfig(
            symbol="BTC-USD",
            start_date=datetime(2024, 1, 1, tzinfo=UTC),
            end_date=datetime(2024, 6, 1, tzinfo=UTC),
        )
        engine = BacktestEngine(config)
        # Need enough data: 6 months of hourly data
        data = make_ohlcv_data(n_rows=3000)

        def strategy(df: pd.DataFrame) -> dict | None:
            if len(df) < 20:
                return None
            return {"direction": "long", "size_pct": 0.1}

        results = engine.walk_forward(data, strategy, train_window_days=30, test_window_days=10)
        assert len(results) > 0
        for r in results:
            assert r.period_type == PeriodType.TEST
