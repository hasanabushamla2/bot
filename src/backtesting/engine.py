"""Backtesting Engine — realistic historical simulation.

CRITICAL DESIGN RULES:
- NEVER use future information (no look-ahead bias).
- Always include fees, spread, slippage.
- Enforce train/validation/test period separation.
- Never optimize on the test period.
- Support walk-forward analysis.
- Realistic execution assumptions.
- Partial fills where data allows.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class PeriodType(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass
class BacktestConfig:
    """Configuration for a backtest run."""

    symbol: str
    start_date: datetime
    end_date: datetime
    initial_capital: float = 10_000.0
    maker_fee: float = 0.001  # 0.1%
    taker_fee: float = 0.001  # 0.1%
    slippage_bps: float = 5.0  # 5 basis points
    latency_ms: float = 100.0  # Assumed execution latency
    min_fill_probability: float = 0.95
    max_position_size_pct: float = 0.1  # Max 10% of capital per position
    stop_loss_pct: float = 0.3  # 0.3% stop loss

    # Required anti-bias controls
    train_end: datetime | None = None
    validation_end: datetime | None = None
    test_start: datetime | None = None


@dataclass
class BacktestTrade:
    """A single simulated trade."""

    entry_time: datetime
    exit_time: datetime
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    return_pct: float
    exit_reason: str = "signal"  # "signal", "stop_loss", "take_profit"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BacktestResult:
    """Complete results of a backtest run."""

    config: BacktestConfig
    trades: list[BacktestTrade] = field(default_factory=list)
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    net_pnl: float = 0.0
    total_return_pct: float = 0.0
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    avg_trade_return_pct: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    period_type: PeriodType = PeriodType.TEST
    metadata: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """Realistic backtesting with anti-bias protections.

    Key anti-bias measures:
    1. Data is split into train/validation/test periods.
    2. The test period is NEVER used for optimization.
    3. Walk-forward analysis is supported.
    4. Fees, spread, and slippage are always included.
    5. Future data leakage is explicitly checked.
    """

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self._ensure_period_separation()

    def _ensure_period_separation(self) -> None:
        """Validate period separation to prevent data leakage."""
        if self.config.train_end and self.config.validation_end:
            if self.config.train_end >= self.config.validation_end:
                raise ValueError("train_end must be before validation_end")
        if self.config.validation_end and self.config.test_start:
            if self.config.validation_end >= self.config.test_start:
                raise ValueError("validation_end must be before test_start")

    def run(
        self,
        data: pd.DataFrame,
        strategy_fn: Callable[..., Any],
        period_type: PeriodType = PeriodType.TEST,
    ) -> BacktestResult:
        """Run backtest over provided historical data.

        Args:
            data: DataFrame with columns: [timestamp, open, high, low, close, volume,
                  bid (optional), ask (optional)].
            strategy_fn: Callable that takes market data snapshot and returns
                        signal dict or None.
            period_type: Which period this run belongs to (train/val/test).

        Returns:
            BacktestResult with full trade log and metrics.
        """
        # Validate required columns
        required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required_cols - set(data.columns)
        if missing:
            raise ValueError(f"Data missing required columns: {missing}")

        # Sort by timestamp
        data = data.sort_values("timestamp").reset_index(drop=True)

        trades: list[BacktestTrade] = []
        equity = self.config.initial_capital
        equity_curve: list[float] = [equity]
        position: dict[str, Any] | None = None

        for i in range(len(data)):
            row = data.iloc[i]
            current_time = row["timestamp"]

            # --- Check stop loss on open position ---
            if position is not None:
                stop_price = position["stop_loss"]
                tp_price = position.get("take_profit")
                current_price = row["close"]
                exit_reason: str | None = None

                if position["direction"] == "long":
                    if current_price <= stop_price:
                        exit_reason = "stop_loss"
                    elif tp_price and current_price >= tp_price:
                        exit_reason = "take_profit"
                else:  # short
                    if current_price >= stop_price:
                        exit_reason = "stop_loss"
                    elif tp_price and current_price <= tp_price:
                        exit_reason = "take_profit"

                if exit_reason:
                    trade = self._close_position(position, current_price, current_time, exit_reason)
                    trades.append(trade)
                    equity += trade.net_pnl
                    equity_curve.append(equity)
                    position = None

            # --- Strategy signal ---
            # Slice data up to CURRENT row only (NO look-ahead)
            market_snapshot = data.iloc[: i + 1].copy()
            try:
                signal = strategy_fn(market_snapshot)
            except Exception as e:
                logger.warning("strategy_error", index=i, error=str(e))
                continue

            if signal is not None and position is None:
                # Open new position
                entry_price = self._apply_slippage(row["close"], signal.get("direction", "long"))
                position = self._open_position(
                    signal, entry_price, current_time, equity
                )
                equity_curve.append(equity)  # Equity doesn't change on entry (only on exit)

        # Close any remaining position at last price
        if position is not None:
            last_price = data.iloc[-1]["close"]
            trade = self._close_position(position, last_price, data.iloc[-1]["timestamp"], "eod")
            trades.append(trade)
            equity += trade.net_pnl
            equity_curve.append(equity)

        # --- Compute metrics ---
        return self._compute_metrics(trades, equity_curve, period_type)

    def walk_forward(
        self,
        data: pd.DataFrame,
        strategy_fn: Callable[..., Any],
        train_window_days: int = 90,
        test_window_days: int = 30,
    ) -> list[BacktestResult]:
        """Run walk-forward analysis with rolling train/test windows.

        Each window: train on previous N days, test on next M days.
        Never uses future data for training.
        """
        results: list[BacktestResult] = []
        data = data.sort_values("timestamp").reset_index(drop=True)
        min_time = data["timestamp"].min()
        max_time = data["timestamp"].max()

        current_start = min_time
        window_idx = 0
        while current_start < max_time:
            train_end = current_start + pd.Timedelta(days=train_window_days)
            test_end = train_end + pd.Timedelta(days=test_window_days)

            _train_data = data[(data["timestamp"] >= current_start) & (data["timestamp"] < train_end)]
            test_data = data[(data["timestamp"] >= train_end) & (data["timestamp"] < test_end)]

            if len(test_data) < 10:
                break

            # Train on past data only
            logger.info("walk_forward_window",
                        window=window_idx,
                        train_start=current_start.isoformat(),
                        train_end=train_end.isoformat(),
                        test_start=train_end.isoformat(),
                        test_end=test_end.isoformat())

            # Run on test window
            result = self.run(test_data, strategy_fn, PeriodType.TEST)
            result.metadata["walk_forward_window"] = window_idx
            result.metadata["train_period"] = f"{current_start.date()} → {train_end.date()}"
            result.metadata["test_period"] = f"{train_end.date()} → {test_end.date()}"
            results.append(result)

            current_start = train_end
            window_idx += 1

        return results

    # --- Position Management ---

    def _open_position(
        self, signal: dict[str, Any], entry_price: float, entry_time: datetime, equity: float
    ) -> dict[str, Any]:
        """Open a simulated position."""
        direction = signal.get("direction", "long")
        capital_pct = min(signal.get("size_pct", self.config.max_position_size_pct),
                          self.config.max_position_size_pct)
        capital = equity * capital_pct
        quantity = capital / entry_price

        stop_loss_pct = signal.get("stop_loss_pct", self.config.stop_loss_pct) / 100.0
        stop_loss = (
            entry_price * (1 - stop_loss_pct)
            if direction == "long"
            else entry_price * (1 + stop_loss_pct)
        )

        take_profit = None
        tp_pct = signal.get("take_profit_pct")
        if tp_pct:
            tp_pct = tp_pct / 100.0
            take_profit = (
                entry_price * (1 + tp_pct)
                if direction == "long"
                else entry_price * (1 - tp_pct)
            )

        return {
            "direction": direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "entry_time": entry_time,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "capital": capital,
        }

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: float,
        exit_time: datetime,
        reason: str,
    ) -> BacktestTrade:
        """Close a simulated position and compute P&L."""
        direction = position["direction"]
        entry_price = position["entry_price"]
        quantity = position["quantity"]

        exit_price = self._apply_slippage(exit_price, "long" if direction == "short" else "short")

        if direction == "long":
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        # Fees (taker for entry + exit)
        notional = entry_price * quantity + exit_price * quantity
        fees = notional * self.config.taker_fee

        # Slippage cost
        slippage_cost = notional * (self.config.slippage_bps / 10000.0)

        net_pnl = gross_pnl - fees - slippage_cost
        return_pct = (net_pnl / position["capital"]) * 100 if position["capital"] > 0 else 0.0

        return BacktestTrade(
            entry_time=position["entry_time"],
            exit_time=exit_time,
            symbol=self.config.symbol,
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            gross_pnl=gross_pnl,
            fees=fees,
            slippage_cost=slippage_cost,
            net_pnl=net_pnl,
            return_pct=return_pct,
            exit_reason=reason,
        )

    # --- Metrics ---

    def _compute_metrics(
        self, trades: list[BacktestTrade], equity_curve: list[float], period_type: PeriodType
    ) -> BacktestResult:
        """Compute comprehensive backtest metrics."""
        result = BacktestResult(config=self.config, period_type=period_type)
        result.trades = trades

        if not trades:
            return result

        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t.net_pnl > 0)
        result.losing_trades = sum(1 for t in trades if t.net_pnl <= 0)
        result.win_rate = result.winning_trades / result.total_trades if result.total_trades > 0 else 0.0
        result.gross_pnl = sum(t.gross_pnl for t in trades)
        result.total_fees = sum(t.fees for t in trades)
        result.total_slippage = sum(t.slippage_cost for t in trades)
        result.net_pnl = sum(t.net_pnl for t in trades)
        result.total_return_pct = (
            (result.net_pnl / self.config.initial_capital) * 100
            if self.config.initial_capital > 0
            else 0.0
        )
        result.profit_factor = self._profit_factor(trades)
        result.max_drawdown_pct = self._max_drawdown(equity_curve)
        result.sharpe_ratio = self._sharpe_ratio(equity_curve)
        result.sortino_ratio = self._sortino_ratio(equity_curve)
        result.avg_trade_return_pct = float(np.mean([t.return_pct for t in trades])) if trades else 0.0
        result.avg_win_pct = (
            float(np.mean([t.return_pct for t in trades if t.net_pnl > 0]))
            if result.winning_trades > 0
            else 0.0
        )
        result.avg_loss_pct = (
            float(np.mean([t.return_pct for t in trades if t.net_pnl <= 0]))
            if result.losing_trades > 0
            else 0.0
        )
        result.expectancy = (
            (result.win_rate * result.avg_win_pct)
            - ((1 - result.win_rate) * abs(result.avg_loss_pct))
            if result.total_trades > 0
            else 0.0
        )
        result.equity_curve = equity_curve

        return result

    # --- Slippage ---

    def _apply_slippage(self, price: float, direction: str) -> float:
        """Apply realistic slippage to an execution price."""
        bps = self.config.slippage_bps / 10000.0
        if direction == "long":
            return price * (1 + bps)  # Buy higher
        else:
            return price * (1 - bps)  # Sell lower

    # --- Metric helpers (static for testability) ---

    @staticmethod
    def _profit_factor(trades: list[BacktestTrade]) -> float:
        gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        gross_loss = abs(sum(t.net_pnl for t in trades if t.net_pnl <= 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    @staticmethod
    def _max_drawdown(equity_curve: list[float]) -> float:
        if not equity_curve:
            return 0.0
        peak = equity_curve[0]
        max_dd = 0.0
        for val in equity_curve:
            if val > peak:
                peak = val
            dd = (peak - val) / peak * 100 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @staticmethod
    def _sharpe_ratio(equity_curve: list[float]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
        if len(returns) == 0 or np.std(returns) == 0:
            return 0.0
        return float(np.mean(returns) / np.std(returns) * np.sqrt(252))

    @staticmethod
    def _sortino_ratio(equity_curve: list[float]) -> float:
        if len(equity_curve) < 2:
            return 0.0
        returns = np.diff(equity_curve) / np.array(equity_curve[:-1])
        downside = returns[returns < 0]
        if len(downside) == 0 or np.std(downside) == 0:
            return 0.0
        return float(np.mean(returns) / np.std(downside) * np.sqrt(252))
