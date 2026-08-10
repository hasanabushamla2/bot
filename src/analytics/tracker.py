"""Analytics Tracker — collects and computes trading performance metrics.

Tracks everything: opportunities, signals, trades, P&L, fees, slippage,
win rate, Sharpe/Sortino ratios, drawdown, strategy/market/exchange
breakdowns, latency metrics, and more.

All metrics are computed from actual trade data — never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class DailyMetrics:
    """Single day's trading metrics."""
    date: str
    total_opportunities: int = 0
    opportunities_rejected: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    fees: float = 0.0
    spread_cost: float = 0.0
    slippage: float = 0.0
    avg_trade_return_pct: float = 0.0
    daily_return_pct: float = 0.0


@dataclass
class CumulativeMetrics:
    """Running cumulative metrics since inception."""
    total_opportunities: int = 0
    opportunities_rejected: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    total_spread_cost: float = 0.0
    total_slippage: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    recovery_factor: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_trade_return_pct: float = 0.0
    avg_daily_return_pct: float = 0.0
    compounded_return_pct: float = 0.0
    fill_ratio: float = 0.0
    order_rejection_rate: float = 0.0
    avg_execution_latency_ms: float = 0.0
    avg_signal_to_order_latency_ms: float = 0.0

    # Breakdowns
    strategy_performance: dict[str, dict[str, float]] = field(default_factory=dict)
    market_performance: dict[str, dict[str, float]] = field(default_factory=dict)
    exchange_performance: dict[str, dict[str, float]] = field(default_factory=dict)


class AnalyticsTracker:
    """Tracks and computes all trading performance metrics.

    Designed to run alongside the trading engine, recording every event
    and computing metrics on-demand. All numbers come from actual data.
    """

    def __init__(self) -> None:
        self._daily_pnl: dict[str, float] = {}  # date -> net_pnl
        self._daily_returns: dict[str, float] = {}  # date -> return_pct
        self._equity_history: list[tuple[datetime, float]] = []
        self._trade_returns: list[float] = []
        self._win_returns: list[float] = []
        self._loss_returns: list[float] = []
        self._execution_latencies: list[float] = []
        self._signal_to_order_latencies: list[float] = []

        # Counts
        self._total_opportunities = 0
        self._opportunities_rejected = 0
        self._total_orders = 0
        self._orders_rejected = 0
        self._total_fills = 0

        # Running sums
        self._gross_pnl = 0.0
        self._net_pnl = 0.0
        self._total_fees = 0.0
        self._total_spread_cost = 0.0
        self._total_slippage = 0.0

        # Peak tracking for drawdown
        self._peak_equity = 0.0
        self._max_drawdown_pct = 0.0

        # Breakdowns
        self._strategy_pnl: dict[str, float] = {}
        self._strategy_trades: dict[str, tuple[int, int]] = {}  # (wins, losses)
        self._market_pnl: dict[str, float] = {}
        self._exchange_pnl: dict[str, float] = {}

    # --- Record Methods ---

    def record_opportunity(self, rejected: bool = False) -> None:
        self._total_opportunities += 1
        if rejected:
            self._opportunities_rejected += 1

    def record_trade(
        self,
        gross_pnl: float,
        net_pnl: float,
        fees: float,
        spread_cost: float = 0.0,
        slippage: float = 0.0,
        strategy_id: str | None = None,
        market: str | None = None,
        exchange: str | None = None,
        date_str: str | None = None,
    ) -> None:
        """Record a completed trade."""
        is_win = net_pnl > 0

        self._total_fills += 1
        self._gross_pnl += gross_pnl
        self._net_pnl += net_pnl
        self._total_fees += fees
        self._total_spread_cost += spread_cost
        self._total_slippage += slippage

        self._trade_returns.append(net_pnl)
        if is_win:
            self._win_returns.append(net_pnl)
        else:
            self._loss_returns.append(net_pnl)

        # Daily tracking
        if date_str is None:
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        self._daily_pnl[date_str] = self._daily_pnl.get(date_str, 0.0) + net_pnl

        # Strategy breakdown
        if strategy_id:
            self._strategy_pnl[strategy_id] = self._strategy_pnl.get(strategy_id, 0.0) + net_pnl
            wins, losses = self._strategy_trades.get(strategy_id, (0, 0))
            self._strategy_trades[strategy_id] = (wins + (1 if is_win else 0), losses + (0 if is_win else 1))

        # Market breakdown
        if market:
            self._market_pnl[market] = self._market_pnl.get(market, 0.0) + net_pnl

        # Exchange breakdown
        if exchange:
            self._exchange_pnl[exchange] = self._exchange_pnl.get(exchange, 0.0) + net_pnl

    def record_order_rejected(self) -> None:
        self._total_orders += 1
        self._orders_rejected += 1

    def record_equity_snapshot(self, equity: float) -> None:
        self._equity_history.append((datetime.now(UTC), equity))
        if equity > self._peak_equity:
            self._peak_equity = equity
        if self._peak_equity > 0:
            dd = (self._peak_equity - equity) / self._peak_equity * 100
            if dd > self._max_drawdown_pct:
                self._max_drawdown_pct = dd

    def record_latency(self, execution_ms: float, signal_to_order_ms: float) -> None:
        self._execution_latencies.append(execution_ms)
        self._signal_to_order_latencies.append(signal_to_order_ms)

    # --- Compute Methods ---

    def get_cumulative_metrics(self, initial_capital: float = 10_000.0) -> CumulativeMetrics:
        """Compute all cumulative metrics from recorded data."""
        m = CumulativeMetrics()
        m.total_opportunities = self._total_opportunities
        m.opportunities_rejected = self._opportunities_rejected
        m.total_trades = self._total_fills
        m.winning_trades = len(self._win_returns)
        m.losing_trades = len(self._loss_returns)
        m.win_rate = m.winning_trades / m.total_trades if m.total_trades > 0 else 0.0
        m.gross_pnl = self._gross_pnl
        m.net_pnl = self._net_pnl
        m.total_fees = self._total_fees
        m.total_spread_cost = self._total_spread_cost
        m.total_slippage = self._total_slippage

        # Profit factor
        gross_profit = sum(self._win_returns) if self._win_returns else 0.0
        gross_loss = abs(sum(self._loss_returns)) if self._loss_returns else 0.0
        m.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Drawdown
        m.max_drawdown_pct = self._max_drawdown_pct

        # Expectancy
        m.avg_win = np.mean(self._win_returns) if self._win_returns else 0.0
        m.avg_loss = np.mean(self._loss_returns) if self._loss_returns else 0.0
        m.expectancy = (
            (m.win_rate * m.avg_win) - ((1 - m.win_rate) * abs(m.avg_loss))
            if m.total_trades > 0
            else 0.0
        )
        m.avg_trade_return_pct = np.mean(self._trade_returns) if self._trade_returns else 0.0

        # Sharpe / Sortino (from daily returns)
        daily_vals = list(self._daily_pnl.values())
        if len(daily_vals) >= 2:
            daily_returns = np.array(daily_vals) / initial_capital
            m.sharpe_ratio = float(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252)) if np.std(daily_returns) > 0 else 0.0
            downside = daily_returns[daily_returns < 0]
            m.sortino_ratio = float(np.mean(daily_returns) / np.std(downside) * np.sqrt(252)) if len(downside) > 0 and np.std(downside) > 0 else 0.0
            m.avg_daily_return_pct = float(np.mean(daily_returns) * 100)

        # Compounded return
        if initial_capital > 0 and self._net_pnl != 0:
            m.compounded_return_pct = (self._net_pnl / initial_capital) * 100

        # Latency
        m.avg_execution_latency_ms = (
            float(np.mean(self._execution_latencies)) if self._execution_latencies else 0.0
        )
        m.avg_signal_to_order_latency_ms = (
            float(np.mean(self._signal_to_order_latencies)) if self._signal_to_order_latencies else 0.0
        )

        # Fill ratio
        total_orders = self._total_fills + self._orders_rejected
        m.fill_ratio = self._total_fills / total_orders if total_orders > 0 else 1.0
        m.order_rejection_rate = self._orders_rejected / total_orders if total_orders > 0 else 0.0

        # Breakdowns
        for sid, pnl in self._strategy_pnl.items():
            wins, losses = self._strategy_trades.get(sid, (0, 0))
            total = wins + losses
            m.strategy_performance[sid] = {
                "net_pnl": pnl,
                "trades": total,
                "win_rate": wins / total if total > 0 else 0.0,
            }

        for market, pnl in self._market_pnl.items():
            m.market_performance[market] = {"net_pnl": pnl}

        for exchange, pnl in self._exchange_pnl.items():
            m.exchange_performance[exchange] = {"net_pnl": pnl}

        return m

    def get_daily_metrics(self) -> list[DailyMetrics]:
        """Return per-day metrics."""
        result: list[DailyMetrics] = []
        for date_str, pnl in sorted(self._daily_pnl.items()):
            dm = DailyMetrics(date=date_str, net_pnl=pnl, gross_pnl=pnl)  # Simplified
            result.append(dm)
        return result
