"""F-05/N-01/N-03/N-27: Rebuilt micro-live accounting with atomic reservation ledger.

Invariants:
- capital_in_positions >= 0  always
- reserved_notional >= 0
- pending_notional >= 0
- sum(active+reserved+pending) <= $50
- cash >= 0
- max_slots enforced
- each position tracks its own unrealized P&L
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class MicroPosition:
    position_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    entry_notional: float = 0.0
    cost_basis: float = 0.0
    entry_fee: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exit_price: float | None = None
    exit_fee: float = 0.0
    realized_pnl_net: float = 0.0
    exit_time: datetime | None = None
    is_open: bool = True
    unrealized_pnl: float = 0.0


@dataclass
class MicroLiveAccountState:
    micro_capital_cap: float = 50.0
    cash_available: float = 50.0
    capital_in_positions: float = 0.0
    reserved_notional: float = 0.0
    pending_notional: float = 0.0
    realized_pnl_net: float = 0.0
    total_fees_paid: float = 0.0
    total_slippage_cost: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    hard_stop_exits: int = 0
    trail_exits: int = 0
    order_rejections: int = 0
    partial_fills: int = 0
    daily_start_equity: float = 50.0
    daily_validation_loss_limit: float = 10.0
    validation_loss_reached: bool = False
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def effective_exposure(self) -> float:
        return self.capital_in_positions + self.reserved_notional + self.pending_notional

    @property
    def micro_equity(self) -> float:
        return self.cash_available + self.capital_in_positions

    @property
    def remaining_capital(self) -> float:
        return max(0.0, self.micro_capital_cap - self.effective_exposure)

    @property
    def available_cash(self) -> float:
        return max(0.0, self.cash_available - self.reserved_notional - self.pending_notional)

    @property
    def daily_loss(self) -> float:
        return max(0.0, self.daily_start_equity - self.micro_equity)


class MicroLiveAccount:
    def __init__(
        self, capital_cap: float = 50.0, slot_size: float = 5.0, max_slots: int = 10
    ) -> None:
        self.state = MicroLiveAccountState(
            micro_capital_cap=capital_cap, cash_available=capital_cap
        )
        self.slot_size = slot_size
        self.max_slots = max_slots
        self._positions: dict[str, MicroPosition] = {}
        self._closed: list[MicroPosition] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # N-03: Atomic reserve
    # ------------------------------------------------------------------
    async def atomic_reserve(self, notional: float) -> bool:
        """Atomic CHECK + RESERVE under lock."""
        async with self._lock:
            if self.state.effective_exposure + notional > self.state.micro_capital_cap:
                return False
            if self.open_slots() >= self.max_slots:
                return False
            if self.state.cash_available < notional:
                return False
            self.state.reserved_notional += notional
            return True

    async def atomic_release_reservation(self, notional: float) -> None:
        async with self._lock:
            self.state.reserved_notional = max(0.0, self.state.reserved_notional - notional)

    async def atomic_confirm_entry(self, notional: float) -> None:
        """Move from reserved → capital_in_positions."""
        async with self._lock:
            self.state.reserved_notional = max(0.0, self.state.reserved_notional - notional)
            self.state.capital_in_positions += notional

    async def atomic_confirm_exit(
        self, entry_notional: float, exit_notional: float, fee: float, net_pnl: float
    ) -> None:
        async with self._lock:
            self.state.capital_in_positions -= min(entry_notional, self.state.capital_in_positions)
            self.state.cash_available += exit_notional - fee
            self.state.realized_pnl_net += net_pnl
            self.state.total_fees_paid += abs(fee)

    # ------------------------------------------------------------------
    # N-01: Simple open/close (called by orchestrator via adapter)
    # ------------------------------------------------------------------
    def open_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: float,
        entry_fee: float = 0.0,
        position_id: str | None = None,
    ) -> MicroPosition | None:
        existing = [p for p in self._positions.values() if p.symbol == symbol and p.is_open]
        if existing:
            return None
        notional = entry_price * quantity
        total = notional + entry_fee
        if total > self.state.cash_available:
            return None
        if self.state.effective_exposure + notional > self.state.micro_capital_cap:
            return None
        if self.open_slots() >= self.max_slots:
            return None
        pos = MicroPosition(
            position_id=position_id or str(uuid4()),
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            entry_notional=notional,
            cost_basis=total,
            entry_fee=entry_fee,
        )
        self._positions[pos.position_id] = pos
        self.state.cash_available -= total
        self.state.capital_in_positions += notional
        self.state.total_fees_paid += entry_fee
        self.state.total_trades += 1
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_fee: float = 0.0,
        exit_reason: str = "signal",
    ) -> MicroPosition | None:
        pos = self._positions.get(position_id)
        if pos is None or not pos.is_open:
            return None
        exit_notional = exit_price * pos.quantity
        net_pnl = (exit_notional - pos.entry_notional) - pos.entry_fee - exit_fee
        self.state.capital_in_positions -= min(pos.entry_notional, self.state.capital_in_positions)
        self.state.cash_available += exit_notional - exit_fee
        self.state.total_fees_paid += exit_fee
        self.state.realized_pnl_net += net_pnl
        pos.exit_price = exit_price
        pos.exit_fee = exit_fee
        pos.realized_pnl_net = net_pnl
        pos.exit_time = datetime.now(UTC)
        pos.is_open = False
        self._closed.append(pos)
        if net_pnl > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1
        if exit_reason == "hard_stop":
            self.state.hard_stop_exits += 1
        elif exit_reason == "trail_hit":
            self.state.trail_exits += 1
        return pos

    # ------------------------------------------------------------------
    # N-27: Each position tracks own unrealized P&L
    # ------------------------------------------------------------------
    def update_unrealized(self, symbol: str, current_price: float) -> None:
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.is_open:
                pos.unrealized_pnl = (current_price - pos.entry_price) * pos.quantity
        self.state.cash_available = self.state.cash_available  # unchanged

    def portfolio_unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self._positions.values() if p.is_open)

    def open_slots(self) -> int:
        return len([p for p in self._positions.values() if p.is_open])

    def can_open_position(self, required_notional: float) -> bool:
        return (
            self.state.effective_exposure + required_notional <= self.state.micro_capital_cap
            and self.state.cash_available >= required_notional
            and self.open_slots() < self.max_slots
            and not self.state.validation_loss_reached
        )

    def check_validation_loss(self) -> bool:
        if self.state.daily_loss >= self.state.daily_validation_loss_limit:
            self.state.validation_loss_reached = True
            return True
        return False

    def _invariants_ok(self) -> list[str]:
        v: list[str] = []
        s = self.state
        if s.capital_in_positions < -0.01:
            v.append(f"capital_in_positions={s.capital_in_positions}")
        if s.cash_available < -0.01:
            v.append(f"cash={s.cash_available}")
        if s.reserved_notional < -0.01:
            v.append(f"reserved={s.reserved_notional}")
        if s.effective_exposure > s.micro_capital_cap + 0.01:
            v.append(f"exposure={s.effective_exposure} > cap={s.micro_capital_cap}")
        if self.open_slots() > self.max_slots:
            v.append(f"slots={self.open_slots()} > max={self.max_slots}")
        return v

    def summary(self) -> dict[str, Any]:
        return {
            "mode": "MICRO-LIVE",
            "capital_cap": self.state.micro_capital_cap,
            "micro_equity": round(self.state.micro_equity, 4),
            "cash": round(self.state.cash_available, 4),
            "capital_in_positions": round(self.state.capital_in_positions, 4),
            "reserved": round(self.state.reserved_notional, 4),
            "effective_exposure": round(self.state.effective_exposure, 4),
            "remaining": round(self.state.remaining_capital, 4),
            "realized_pnl_net": round(self.state.realized_pnl_net, 4),
            "total_fees": round(self.state.total_fees_paid, 4),
            "trades": self.state.total_trades,
            "open_positions": self.open_slots(),
            "max_slots": self.max_slots,
            "invariants_ok": len(self._invariants_ok()) == 0,
        }

    def daily_report(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": "MICRO-LIVE — REAL MONEY — MAX $50",
            "micro_capital_cap": s.micro_capital_cap,
            "starting_equity": s.daily_start_equity,
            "ending_equity": round(s.micro_equity, 4),
            "current_cash": round(s.cash_available, 4),
            "capital_in_positions": round(s.capital_in_positions, 4),
            "reserved_notional": round(s.reserved_notional, 4),
            "total_trades": s.total_trades,
            "wins": s.winning_trades,
            "losses": s.losing_trades,
            "realized_pnl_net": round(s.realized_pnl_net, 4),
            "total_fees": round(s.total_fees_paid, 4),
            "total_slippage": round(s.total_slippage_cost, 4),
            "hard_stop_exits": s.hard_stop_exits,
            "trail_exits": s.trail_exits,
            "validation_loss_reached": s.validation_loss_reached,
            "invariants_ok": len(self._invariants_ok()) == 0,
        }
