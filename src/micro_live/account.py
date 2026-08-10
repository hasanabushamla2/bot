"""Micro-Live Account — tracks the $50 capital envelope separately from exchange wallet."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)

@dataclass
class MicroLiveAccountState:
    micro_capital_cap: float = 50.0
    initial_micro_capital: float = 50.0
    cash_available: float = 50.0
    capital_reserved: float = 0.0
    capital_in_positions: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_buy_fees: float = 0.0
    total_sell_fees: float = 0.0
    total_fees: float = 0.0
    total_slippage_cost: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    hard_stop_exits: int = 0
    trail_exits: int = 0
    order_rejections: int = 0
    partial_fills: int = 0
    balance_mismatches: int = 0
    circuit_breaker_events: int = 0
    daily_start_equity: float = 50.0
    daily_validation_loss_limit: float = 10.0
    validation_loss_reached: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def micro_equity(self) -> float:
        return self.cash_available + self.capital_in_positions + self.unrealized_pnl
    @property
    def remaining_capital(self) -> float:
        return max(0.0, self.micro_capital_cap - self.capital_reserved - self.capital_in_positions)
    @property
    def daily_loss(self) -> float:
        return max(0.0, self.daily_start_equity - self.micro_equity)

class MicroLiveAccount:
    def __init__(self, capital_cap: float = 50.0, slot_size: float = 5.0, max_slots: int = 10) -> None:
        self.state = MicroLiveAccountState(micro_capital_cap=capital_cap, initial_micro_capital=capital_cap, cash_available=capital_cap)
        self.slot_size = slot_size
        self.max_slots = max_slots

    def can_open_position(self, required_capital: float) -> bool:
        if self.state.validation_loss_reached:
            return False
        if self.state.remaining_capital < required_capital:
            return False
        return self.state.cash_available >= required_capital

    def reserve_capital(self, amount: float) -> bool:
        if amount > self.state.cash_available:
            return False
        if self.state.capital_reserved + amount > self.state.micro_capital_cap:
            return False
        self.state.cash_available -= amount
        self.state.capital_reserved += amount
        return True

    def execute_buy(self, notional: float, fee: float) -> None:
        self.state.capital_reserved -= notional
        self.state.capital_in_positions += notional
        self.state.total_buy_fees += fee
        self.state.total_fees += fee
        self.state.cash_available -= fee
        self.state.total_trades += 1

    def execute_sell(self, exit_notional: float, fee: float, pnl: float) -> None:
        self.state.capital_in_positions -= abs(exit_notional)
        self.state.cash_available += exit_notional - fee
        self.state.total_sell_fees += fee
        self.state.total_fees += fee
        self.state.realized_pnl += pnl
        if pnl > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1

    def add_slippage(self, cost: float) -> None:
        self.state.total_slippage_cost += cost
        self.state.cash_available -= cost

    def check_validation_loss(self) -> bool:
        if self.state.daily_loss >= self.state.daily_validation_loss_limit:
            self.state.validation_loss_reached = True
            logger.warning("micro_live_validation_loss_reached", loss=self.state.daily_loss)
            return True
        return False

    def summary(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": "MICRO-LIVE", "capital_cap": s.micro_capital_cap,
            "micro_equity": round(s.micro_equity, 4),
            "cash_available": round(s.cash_available, 4),
            "capital_reserved": round(s.capital_reserved, 4),
            "capital_in_positions": round(s.capital_in_positions, 4),
            "realized_pnl": round(s.realized_pnl, 4),
            "total_fees": round(s.total_fees, 4),
            "total_trades": s.total_trades,
            "wins": s.winning_trades, "losses": s.losing_trades,
            "hard_stop_exits": s.hard_stop_exits,
            "trail_exits": s.trail_exits,
            "remaining_capital": round(s.remaining_capital, 4),
            "validation_loss_reached": s.validation_loss_reached,
        }

    def daily_report(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": "MICRO-LIVE — REAL MONEY — MAX $50",
            "micro_capital_cap": s.micro_capital_cap,
            "starting_micro_equity": s.daily_start_equity,
            "ending_micro_equity": round(s.micro_equity, 4),
            "current_cash": round(s.cash_available, 4),
            "open_position_value": round(s.capital_in_positions, 4),
            "total_real_trades": s.total_trades,
            "winning_trades": s.winning_trades,
            "losing_trades": s.losing_trades,
            "gross_pnl": round(s.realized_pnl + s.unrealized_pnl, 4),
            "total_buy_fees": round(s.total_buy_fees, 4),
            "total_sell_fees": round(s.total_sell_fees, 4),
            "total_fees": round(s.total_fees, 4),
            "total_slippage_cost": round(s.total_slippage_cost, 4),
            "net_pnl": round(s.realized_pnl - s.total_fees - s.total_slippage_cost, 4),
            "order_rejections": s.order_rejections,
            "partial_fills": s.partial_fills,
            "balance_mismatches": s.balance_mismatches,
            "circuit_breaker_events": s.circuit_breaker_events,
            "validation_loss_limit": s.daily_validation_loss_limit,
            "validation_loss_reached": s.validation_loss_reached,
        }
