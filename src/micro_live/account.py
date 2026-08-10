"""F-05: Rebuilt micro-live accounting with per-position cost basis tracking.

Invariants:
- capital_in_positions >= 0  always
- cash >= 0                  always
- remaining_capital <= cap   always
- no position → capital_in_positions == 0
- no duplicate fee subtraction
- realized_pnl is NET of all fees (consistent model A)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class MicroPosition:
    """F-05: Full per-position cost-basis tracking."""

    position_id: str = field(default_factory=lambda: str(uuid4()))
    symbol: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    entry_notional: float = 0.0
    cost_basis: float = 0.0  # entry_notional + entry_fee
    entry_fee: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Exit
    exit_price: float | None = None
    exit_fee: float = 0.0
    realized_pnl_net: float = 0.0  # NET of all fees
    exit_time: datetime | None = None
    is_open: bool = True


@dataclass
class MicroLiveAccountState:
    micro_capital_cap: float = 50.0
    initial_micro_capital: float = 50.0
    cash_available: float = 50.0
    capital_in_positions: float = 0.0
    realized_pnl_net: float = 0.0  # Model A: already net of fees
    unrealized_pnl: float = 0.0
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
    def micro_equity(self) -> float:
        return self.cash_available + self.capital_in_positions + self.unrealized_pnl

    @property
    def remaining_capital(self) -> float:
        return max(0.0, self.micro_capital_cap - self.capital_in_positions)

    @property
    def daily_loss(self) -> float:
        return max(0.0, self.daily_start_equity - self.micro_equity)


class MicroLiveAccount:
    """F-05: Rebuilt with per-position ledger."""

    def __init__(
        self, capital_cap: float = 50.0, slot_size: float = 5.0, max_slots: int = 10
    ) -> None:
        self.state = MicroLiveAccountState(
            micro_capital_cap=capital_cap,
            initial_micro_capital=capital_cap,
            cash_available=capital_cap,
        )
        self.slot_size = slot_size
        self.max_slots = max_slots
        self._positions: dict[str, MicroPosition] = {}
        self._closed: list[MicroPosition] = []

    # ------------------------------------------------------------------
    # F-05 invariant checks
    # ------------------------------------------------------------------

    def verify_invariants(self) -> list[str]:
        """Return list of violated invariants (empty = OK)."""
        violations: list[str] = []
        s = self.state
        if s.capital_in_positions < 0:
            violations.append(f"capital_in_positions={s.capital_in_positions} < 0")
        if s.cash_available < -0.0001:
            violations.append(f"cash={s.cash_available} < 0")
        if s.remaining_capital > s.micro_capital_cap + 0.0001:
            violations.append(f"remaining={s.remaining_capital} > cap={s.micro_capital_cap}")
        open_notional = sum(p.entry_notional for p in self._positions.values() if p.is_open)
        if abs(s.capital_in_positions - open_notional) > 0.01:
            violations.append(
                f"capital_in_positions={s.capital_in_positions} != sum(entry_notional)={open_notional}"
            )
        recomputed = s.cash_available + s.capital_in_positions
        if abs(recomputed - s.micro_equity) > 0.01:
            violations.append(
                f"equity mismatch: cash+positions={recomputed} != equity={s.micro_equity}"
            )
        return violations

    # ------------------------------------------------------------------
    # Open position
    # ------------------------------------------------------------------

    def open_position(
        self,
        symbol: str,
        entry_price: float,
        quantity: float,
        entry_fee: float = 0.0,
        position_id: str | None = None,
    ) -> MicroPosition | None:
        """F-04: Reject duplicate same-symbol positions by default."""
        # Reject duplicate
        existing = [p for p in self._positions.values() if p.symbol == symbol and p.is_open]
        if existing:
            logger.warning("duplicate_position_blocked", symbol=symbol)
            return None

        notional = entry_price * quantity
        total_needed = notional + entry_fee

        if total_needed > self.state.cash_available:
            logger.warning(
                "micro_live_insufficient_cash",
                needed=total_needed,
                available=self.state.cash_available,
            )
            return None

        if self.state.capital_in_positions + notional > self.state.micro_capital_cap:
            logger.warning("micro_live_cap_exceeded")
            return None

        pos = MicroPosition(
            position_id=position_id or str(uuid4()),
            symbol=symbol,
            quantity=quantity,
            entry_price=entry_price,
            entry_notional=notional,
            cost_basis=notional + entry_fee,
            entry_fee=entry_fee,
        )
        self._positions[pos.position_id] = pos
        self.state.cash_available -= notional + entry_fee
        self.state.capital_in_positions += notional
        self.state.total_fees_paid += entry_fee
        self.state.total_trades += 1
        logger.info(
            "micro_live_open", symbol=symbol, notional=round(notional, 2), fee=round(entry_fee, 4)
        )
        return pos

    # ------------------------------------------------------------------
    # Close position — F-05: decrease by COST BASIS, not exit notional
    # ------------------------------------------------------------------

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_fee: float = 0.0,
        exit_reason: str = "signal",
    ) -> MicroPosition | None:
        """F-05: Decrease capital_in_positions by the CLOSED position's cost basis."""
        pos = self._positions.get(position_id)
        if pos is None or not pos.is_open:
            return None

        exit_notional = exit_price * pos.quantity
        # F-05: Gross P&L, then subtract fees
        gross_pnl = exit_notional - pos.entry_notional
        net_pnl = gross_pnl - pos.entry_fee - exit_fee
        cash_in = exit_notional - exit_fee

        # F-05: Decrease capital_in_positions by cost_basis, NOT exit notional
        self.state.capital_in_positions -= pos.entry_notional
        if self.state.capital_in_positions < 0:
            self.state.capital_in_positions = 0.0  # safety clamp

        self.state.cash_available += cash_in
        self.state.total_fees_paid += exit_fee
        self.state.realized_pnl_net += net_pnl  # Model A: net of fees

        if net_pnl > 0:
            self.state.winning_trades += 1
        else:
            self.state.losing_trades += 1

        if exit_reason == "hard_stop":
            self.state.hard_stop_exits += 1
        elif exit_reason == "trail_hit":
            self.state.trail_exits += 1

        pos.exit_price = exit_price
        pos.exit_fee = exit_fee
        pos.realized_pnl_net = net_pnl
        pos.exit_time = datetime.now(UTC)
        pos.is_open = False
        self._closed.append(pos)

        logger.info(
            "micro_live_close", symbol=pos.symbol, reason=exit_reason, net_pnl=round(net_pnl, 4)
        )
        return pos

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def can_open_position(self, required_notional: float) -> bool:
        if self.state.validation_loss_reached:
            return False
        total = self.state.capital_in_positions + required_notional
        return (
            total <= self.state.micro_capital_cap and self.state.cash_available >= required_notional
        )

    def open_slots(self) -> int:
        return len([p for p in self._positions.values() if p.is_open])

    def check_validation_loss(self) -> bool:
        if self.state.daily_loss >= self.state.daily_validation_loss_limit:
            self.state.validation_loss_reached = True
            logger.warning("micro_live_validation_loss", loss=self.state.daily_loss)
            return True
        return False

    def update_unrealized(self, symbol: str, current_price: float) -> None:
        total = 0.0
        for pos in self._positions.values():
            if pos.symbol == symbol and pos.is_open:
                total += (current_price - pos.entry_price) * pos.quantity
        self.state.unrealized_pnl = total

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": "MICRO-LIVE",
            "capital_cap": s.micro_capital_cap,
            "micro_equity": round(s.micro_equity, 4),
            "cash_available": round(s.cash_available, 4),
            "capital_in_positions": round(s.capital_in_positions, 4),
            "realized_pnl_net": round(s.realized_pnl_net, 4),
            "total_fees_paid": round(s.total_fees_paid, 4),
            "total_trades": s.total_trades,
            "wins": s.winning_trades,
            "losses": s.losing_trades,
            "open_positions": len([p for p in self._positions.values() if p.is_open]),
            "remaining_capital": round(s.remaining_capital, 4),
            "invariants_ok": len(self.verify_invariants()) == 0,
        }

    def daily_report(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": "MICRO-LIVE — REAL MONEY — MAX $50",
            "micro_capital_cap": s.micro_capital_cap,
            "starting_micro_equity": s.daily_start_equity,
            "ending_micro_equity": round(s.micro_equity, 4),
            "current_cash": round(s.cash_available, 4),
            "capital_in_positions": round(s.capital_in_positions, 4),
            "total_real_trades": s.total_trades,
            "winning_trades": s.winning_trades,
            "losing_trades": s.losing_trades,
            "gross_pnl_net_of_fees": round(s.realized_pnl_net, 4),
            "total_fees_paid": round(s.total_fees_paid, 4),
            "total_slippage_cost": round(s.total_slippage_cost, 4),
            "order_rejections": s.order_rejections,
            "partial_fills": s.partial_fills,
            "hard_stop_exits": s.hard_stop_exits,
            "trail_exits": s.trail_exits,
            "validation_loss_limit": s.daily_validation_loss_limit,
            "validation_loss_reached": s.validation_loss_reached,
            "invariants_ok": len(self.verify_invariants()) == 0,
        }
