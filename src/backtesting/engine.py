"""Backtesting Engine — realistic historical simulation.

FINAL POLICY (v1.0):
- SPOT only, no leverage, no margin
- HARD STOP: -0.30% per position
- NO FIXED TAKE PROFIT
- TRAILING STOP for profit protection
- 100+ altcoin universe
- Opportunity-driven trade count
- Dynamic capital allocation

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
    """Configuration for a backtest run.

    FINAL TRADING POLICY CONFIGURATION:
    - stop_loss_pct: 0.30 (HARD stop at -0.30%)
    - enable_take_profit: False (NO fixed profit ceiling)
    - enable_trailing_stop: True (trailing profit protection)
    - trail_pct: 0.15 (trail distance from peak)
    - trail_activation_pct: 0.15 (profit needed to activate trail)
    """

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

    # --- FINAL STOP LOSS: -0.30% ---
    stop_loss_pct: float = 0.30  # -0.30% hard stop (FINAL)

    # --- NO FIXED TAKE PROFIT ---
    enable_take_profit: bool = False  # MUST be False per policy

    # --- TRAILING STOP ---
    enable_trailing_stop: bool = True
    trail_pct: float = 0.15  # Trailing distance from peak
    trail_activation_pct: float = 0.15  # Profit required to activate trail

    # --- SPOT ONLY ---
    allow_short: bool = False  # MUST be False per spot-only policy

    # Required anti-bias controls
    train_end: datetime | None = None
    validation_end: datetime | None = None
    test_start: datetime | None = None


@dataclass
class BacktestTrade:
    """A single simulated trade with complete audit trail."""

    entry_time: datetime
    exit_time: datetime
    symbol: str
    direction: str  # "long" or "short"
    entry_price: float
    exit_price: float
    quantity: float

    # P&L components
    gross_pnl: float
    fees: float
    slippage_cost: float
    net_pnl: float
    return_pct: float

    # Exit tracking
    exit_reason: str = "signal"
    # "hard_stop", "trail_hit", "signal", "eod"

    # Stop loss audit
    target_stop_price: float = 0.0  # Where the hard stop was set
    actual_exit_price: float = 0.0  # Where the exit actually happened
    stop_slippage_pct: float = 0.0  # Difference between target and actual

    # Trailing stop audit
    trail_peak_price: float = 0.0  # Peak price achieved during position
    trail_exit_level: float = 0.0  # Price at which trail triggered exit
    trail_captured_pct: float = 0.0  # % return captured by trail

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

    # Stop-loss audit
    avg_stop_slippage_pct: float = 0.0
    hard_stop_exits: int = 0
    trail_exits: int = 0

    # Timing
    avg_holding_time_minutes: float = 0.0

    metadata: dict[str, Any] = field(default_factory=dict)


class BacktestEngine:
    """Realistic backtesting with anti-bias protections and trailing stops.

    FINAL CONFIGURATION: SPOT, -0.30% stop, trailing stop, NO fixed TP.
    """

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self._ensure_period_separation()

    def _ensure_period_separation(self) -> None:
        if (
            self.config.train_end
            and self.config.validation_end
            and self.config.train_end >= self.config.validation_end
        ):
            raise ValueError("train_end must be before validation_end")
        if (
            self.config.validation_end
            and self.config.test_start
            and self.config.validation_end >= self.config.test_start
        ):
            raise ValueError("validation_end must be before test_start")

    def run(
        self,
        data: pd.DataFrame,
        strategy_fn: Callable[..., Any],
        period_type: PeriodType = PeriodType.TEST,
    ) -> BacktestResult:
        """Run backtest over historical OHLCV data.

        Logic per bar:
        1. Check existing position: hard stop? trailing stop?
        2. Strategy signal? → open new position if no position.
        3. Always record equity.
        """
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(data.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")
        data = data.sort_values("timestamp").reset_index(drop=True)

        trades: list[BacktestTrade] = []
        equity = self.config.initial_capital
        equity_curve: list[float] = [equity]
        position: dict[str, Any] | None = None
        trail_peak: float = 0.0
        trail_activated: bool = False

        for i in range(len(data)):
            row = data.iloc[i]
            current_time = row["timestamp"]
            current_price = row["close"]

            # --- Check existing position ---
            if position is not None:
                exit_reason: str | None = None
                exit_price_actual: float | None = None

                # Update trailing peak
                trail_level = position.get("trail_level", 0.0)
                if position["direction"] == "long" and current_price > trail_peak:
                    trail_peak = current_price
                    # Activate trail after reaching activation threshold
                    if not trail_activated:
                        trail_activation_price = position["entry_price"] * (
                            1.0 + self.config.trail_activation_pct / 100.0
                        )
                        if current_price >= trail_activation_price:
                            trail_activated = True
                    if trail_activated:
                        trail_level = trail_peak * (1.0 - self.config.trail_pct / 100.0)
                        position["trail_level"] = trail_level
                        position["trail_peak"] = trail_peak

                # 1. Hard stop check (-0.30%)
                stop_price = position["stop_loss"]
                tp_price = position.get("take_profit") if self.config.enable_take_profit else None

                if position["direction"] == "long":
                    if current_price <= stop_price:
                        exit_reason = "hard_stop"
                        exit_price_actual = current_price
                    elif tp_price and current_price >= tp_price:
                        exit_reason = "take_profit"
                        exit_price_actual = current_price
                    elif trail_activated and current_price <= trail_level:
                        exit_reason = "trail_hit"
                        exit_price_actual = current_price
                else:
                    if current_price >= stop_price:
                        exit_reason = "hard_stop"
                        exit_price_actual = current_price
                    elif tp_price and current_price <= tp_price:
                        exit_reason = "take_profit"
                        exit_price_actual = current_price
                    elif trail_activated and current_price >= trail_level:
                        exit_reason = "trail_hit"
                        exit_price_actual = current_price
                    elif trail_activated and trail_level > 0:
                        pass  # Short trail not fully implemented

                if exit_reason:
                    trade = self._close_position(
                        position,
                        exit_price_actual or current_price,
                        current_time,
                        exit_reason,
                        trail_peak=trail_peak,
                        trail_level=trail_level,
                    )
                    trades.append(trade)
                    equity += trade.net_pnl
                    equity_curve.append(equity)
                    position = None
                    trail_peak = 0.0
                    trail_activated = False

            # --- Strategy signal (only if no position) ---
            if position is None:
                market_snapshot = data.iloc[: i + 1].copy()
                try:
                    signal = strategy_fn(market_snapshot)
                except Exception as e:
                    logger.warning("strategy_error", index=i, error=str(e))
                    continue

                if signal is not None:
                    # SPOT-ONLY: reject short signals
                    direction = signal.get("direction", "long")
                    if direction == "short" and not self.config.allow_short:
                        continue

                    entry_price = self._apply_slippage(current_price, direction)
                    position = self._open_position(signal, entry_price, current_time, equity)
                    trail_peak = entry_price
                    trail_activated = False

        # Close remaining position at last price
        if position is not None:
            last_price = data.iloc[-1]["close"]
            if len(data) > 0:
                last_row = data.iloc[-1]
                last_price = last_row["close"]

            trade = self._close_position(
                position,
                last_price,
                data.iloc[-1]["timestamp"],
                "eod",
                trail_peak=trail_peak,
                trail_level=position.get("trail_level", 0.0),
            )
            trades.append(trade)
            equity += trade.net_pnl
            equity_curve.append(equity)

        return self._compute_metrics(trades, equity_curve, period_type)

    def walk_forward(
        self,
        data: pd.DataFrame,
        strategy_fn: Callable[..., Any],
        train_window_days: int = 90,
        test_window_days: int = 30,
    ) -> list[BacktestResult]:
        results: list[BacktestResult] = []
        data = data.sort_values("timestamp").reset_index(drop=True)
        min_time = data["timestamp"].min()
        max_time = data["timestamp"].max()

        current_start = min_time
        window_idx = 0
        while current_start < max_time:
            train_end = current_start + pd.Timedelta(days=train_window_days)
            test_end = train_end + pd.Timedelta(days=test_window_days)

            _train_data = data[
                (data["timestamp"] >= current_start) & (data["timestamp"] < train_end)
            ]
            test_data = data[(data["timestamp"] >= train_end) & (data["timestamp"] < test_end)]

            if len(test_data) < 10:
                break

            result = self.run(test_data, strategy_fn, PeriodType.TEST)
            result.metadata["walk_forward_window"] = window_idx
            result.metadata["test_period"] = f"{train_end.date()} → {test_end.date()}"
            results.append(result)

            current_start = train_end
            window_idx += 1

        return results

    # --- Position Management ---

    def _open_position(
        self, signal: dict[str, Any], entry_price: float, entry_time: datetime, equity: float
    ) -> dict[str, Any]:
        direction = signal.get("direction", "long")
        capital_pct = min(
            signal.get("size_pct", self.config.max_position_size_pct),
            self.config.max_position_size_pct,
        )
        capital = equity * capital_pct
        quantity = capital / entry_price

        # Hard stop: -0.30% (FINAL)
        stop_loss_pct = self.config.stop_loss_pct
        signal_stop = signal.get("stop_loss_pct")
        if signal_stop is not None:
            stop_loss_pct = float(signal_stop)
        pct = stop_loss_pct / 100.0
        stop_loss = entry_price * (1.0 - pct) if direction == "long" else entry_price * (1.0 + pct)

        return {
            "direction": direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "entry_time": entry_time,
            "capital": capital,
            # Hard stop
            "stop_loss": stop_loss,
            "stop_loss_pct": stop_loss_pct,
            # NO fixed take profit
            "take_profit": None,
            # Trailing
            "trail_level": 0.0,
            "trail_peak": 0.0,
            "trail_activated": False,
        }

    def _close_position(
        self,
        position: dict[str, Any],
        exit_price: float,
        exit_time: datetime,
        reason: str,
        trail_peak: float = 0.0,
        trail_level: float = 0.0,
    ) -> BacktestTrade:
        direction = position["direction"]
        entry_price = position["entry_price"]
        quantity = position["quantity"]

        exit_price = self._apply_slippage(exit_price, "long")

        if direction == "long":
            gross_pnl = (exit_price - entry_price) * quantity
        else:
            gross_pnl = (entry_price - exit_price) * quantity

        notional = entry_price * quantity + exit_price * quantity
        fees = notional * self.config.taker_fee
        slippage_cost = notional * (self.config.slippage_bps / 10000.0)
        net_pnl = gross_pnl - fees - slippage_cost
        return_pct = (net_pnl / position["capital"]) * 100 if position["capital"] > 0 else 0.0

        # --- Stop-loss audit ---
        target_stop = position["stop_loss"]
        actual_exit = exit_price
        if reason == "hard_stop":
            stop_slip = (actual_exit - target_stop) / target_stop * 100.0
            if direction == "long":
                stop_slip = -abs(stop_slip)
            else:
                stop_slip = abs(stop_slip)
        else:
            stop_slip = 0.0

        # --- Trail audit ---
        captured_pct = 0.0
        if trail_level > 0 and trail_level > entry_price and "long" in direction:
            captured_pct = (trail_level - entry_price) / entry_price * 100.0

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
            target_stop_price=target_stop,
            actual_exit_price=actual_exit,
            stop_slippage_pct=stop_slip,
            trail_peak_price=trail_peak,
            trail_exit_level=trail_level,
            trail_captured_pct=captured_pct,
        )

    # --- Metrics ---

    def _compute_metrics(
        self, trades: list[BacktestTrade], equity_curve: list[float], period_type: PeriodType
    ) -> BacktestResult:
        result = BacktestResult(config=self.config, period_type=period_type)
        result.trades = trades

        if not trades:
            return result

        result.total_trades = len(trades)
        result.winning_trades = sum(1 for t in trades if t.net_pnl > 0)
        result.losing_trades = sum(1 for t in trades if t.net_pnl <= 0)
        result.win_rate = (
            result.winning_trades / result.total_trades if result.total_trades > 0 else 0.0
        )
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
        result.avg_trade_return_pct = (
            float(np.mean([t.return_pct for t in trades])) if trades else 0.0
        )
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

        # Stop-loss audit
        hard_stops = [t for t in trades if t.exit_reason == "hard_stop"]
        result.hard_stop_exits = len(hard_stops)
        result.avg_stop_slippage_pct = (
            float(np.mean([abs(t.stop_slippage_pct) for t in hard_stops])) if hard_stops else 0.0
        )

        # Trail audit
        result.trail_exits = len([t for t in trades if t.exit_reason == "trail_hit"])

        # Holding time
        if trades:
            durations = [(t.exit_time - t.entry_time).total_seconds() / 60.0 for t in trades]
            result.avg_holding_time_minutes = float(np.mean(durations))

        return result

    def _apply_slippage(self, price: float, direction: str) -> float:
        bps = self.config.slippage_bps / 10000.0
        if direction == "long":
            return price * (1 + bps)
        else:
            return price * (1 - bps)

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
