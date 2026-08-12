"""Paper account with one explicit, net-of-costs accounting convention."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)

CLOSED_TRADE_RAM_LIMIT = 500


@dataclass
class PaperPosition:
    symbol: str = ""
    direction: str = "long"
    entry_price: float = 0.0  # actual fill price, including modeled price slippage
    entry_reference_price: float = 0.0  # VWAP before configured slippage
    quantity: float = 0.0
    notional: float = 0.0  # actual entry fill notional
    stop_loss_price: float = 0.0
    trail_activated: bool = False
    trail_peak: float = 0.0
    trail_level: float = 0.0
    trail_activation_pct: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    fees_paid: float = 0.0  # remaining entry fee on this position
    entry_slippage_cost: float = 0.0  # remaining embedded entry slippage
    is_open: bool = True
    strategy_id: str = ""
    opportunity_id: str = ""
    signal_id: str = ""
    signal_timestamp: datetime | None = None
    entry_confidence: float | None = None
    max_favorable_excursion_pct: float = 0.0
    max_adverse_excursion_pct: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClosedTrade:
    symbol: str = ""
    direction: str = "long"
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    gross_pnl: float = 0.0  # reference-price PnL before fees/configured slippage
    fees: float = 0.0  # compatibility alias for total_fee
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    exit_reason: str = ""
    target_stop: float = 0.0
    actual_exit: float = 0.0
    stop_slippage_pct: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exit_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    holding_seconds: float = 0.0
    strategy_id: str = ""
    signal_id: str = ""
    signal_timestamp: datetime | None = None
    entry_confidence: float | None = None
    max_favorable_excursion_pct: float = 0.0
    max_adverse_excursion_pct: float = 0.0
    trade_id: str = ""

    @property
    def total_fee(self) -> float:
        return self.entry_fee + self.exit_fee


@dataclass
class PaperAccountState:
    initial_balance: float = 10_000.0
    cash: float = 10_000.0
    allocated: float = 0.0
    reserved: float = 0.0
    unrealized_pnl: float = 0.0
    # Realized PnL is NET: reference gross less entry fee, exit fee, and
    # configured slippage.  Fees/slippage are never subtracted from it again.
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    peak_equity: float = 10_000.0
    max_drawdown_pct: float = 0.0
    daily_start_equity: float = 10_000.0
    daily_pnl: float = 0.0
    open_positions: dict[str, PaperPosition] = field(default_factory=dict)
    closed_trades: deque[ClosedTrade] = field(
        default_factory=lambda: deque(maxlen=CLOSED_TRADE_RAM_LIMIT)
    )
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def equity(self) -> float:
        return self.cash + self.allocated + self.unrealized_pnl

    @property
    def available(self) -> float:
        return self.cash


class PaperAccount:
    def __init__(self, initial_balance: float = 10_000.0) -> None:
        self.state = PaperAccountState(
            initial_balance=initial_balance,
            cash=initial_balance,
            peak_equity=initial_balance,
            daily_start_equity=initial_balance,
        )

    def open_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: float,
        fees: float = 0.0,
        stop_loss_price: float = 0.0,
        strategy_id: str = "",
        opportunity_id: str = "",
        *,
        entry_reference_price: float | None = None,
        entry_slippage_cost: float = 0.0,
        trail_activation_pct: float = 0.0,
        signal_id: str = "",
        signal_timestamp: datetime | None = None,
        entry_confidence: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PaperPosition | None:
        notional = entry_price * quantity
        if symbol in self.state.open_positions:
            logger.warning("paper_duplicate_blocked", symbol=symbol)
            return None
        cost = notional + fees
        if quantity <= 0 or entry_price <= 0 or cost > self.state.cash:
            logger.warning("paper_insufficient_cash", needed=cost, available=self.state.cash)
            return None

        self.state.cash -= cost
        self.state.allocated += notional
        self.state.total_fees += fees
        self.state.total_slippage += max(0.0, entry_slippage_cost)
        pos = PaperPosition(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            entry_reference_price=entry_reference_price or entry_price,
            quantity=quantity,
            notional=notional,
            stop_loss_price=stop_loss_price,
            fees_paid=fees,
            entry_slippage_cost=max(0.0, entry_slippage_cost),
            strategy_id=strategy_id,
            opportunity_id=opportunity_id,
            trail_peak=entry_price,
            trail_activation_pct=trail_activation_pct,
            signal_id=signal_id,
            signal_timestamp=signal_timestamp,
            entry_confidence=entry_confidence,
            metadata=dict(metadata or {}),
        )
        self.state.open_positions[symbol] = pos
        self._update_peak_and_drawdown()
        self.assert_invariants()
        logger.info(
            "paper_position_opened",
            symbol=symbol,
            notional=round(notional, 2),
            fees=round(fees, 4),
            signal_id=signal_id,
        )
        return pos

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        fees: float = 0.0,
        slippage: float = 0.0,
        exit_reason: str = "signal_exit",
        trail_peak: float = 0.0,
        trail_level: float = 0.0,
        *,
        exit_reference_price: float | None = None,
        embedded_slippage_cost: float = 0.0,
    ) -> ClosedTrade | None:
        pos = self.state.open_positions.pop(symbol, None)
        if pos is None:
            return None

        exit_reference = exit_reference_price or exit_price
        entry_reference = pos.entry_reference_price or pos.entry_price
        exit_notional = exit_price * pos.quantity
        self.state.allocated -= pos.notional
        if abs(self.state.allocated) < 1e-9:
            self.state.allocated = 0.0
        self.state.total_fees += fees
        exit_slippage = max(0.0, embedded_slippage_cost) + max(0.0, slippage)
        self.state.total_slippage += exit_slippage

        if pos.direction == "long":
            gross = (exit_reference - entry_reference) * pos.quantity
        else:
            gross = (entry_reference - exit_reference) * pos.quantity
        total_slippage = pos.entry_slippage_cost + exit_slippage
        net = gross - fees - pos.fees_paid - total_slippage

        # Actual fill prices already contain embedded slippage, so only an
        # explicitly separate slippage charge is deducted from cash here.
        self.state.cash += exit_notional - fees - max(0.0, slippage)
        self.state.realized_pnl += net
        self.state.trade_count += 1
        if net > 0:
            self.state.win_count += 1
        else:
            self.state.loss_count += 1

        return_pct = (net / pos.notional * 100) if pos.notional > 0 else 0.0
        stop_slip = 0.0
        if exit_reason in ("hard_stop", "stop_loss") and pos.stop_loss_price > 0:
            if pos.direction == "long":
                stop_slip = (exit_price - pos.stop_loss_price) / pos.stop_loss_price * 100
            else:
                stop_slip = (pos.stop_loss_price - exit_price) / pos.stop_loss_price * 100

        exit_time = datetime.now(UTC)
        trade = ClosedTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            gross_pnl=gross,
            fees=fees + pos.fees_paid,
            entry_fee=pos.fees_paid,
            exit_fee=fees,
            slippage_cost=total_slippage,
            net_pnl=net,
            return_pct=return_pct,
            exit_reason=exit_reason,
            target_stop=pos.stop_loss_price,
            actual_exit=exit_price,
            stop_slippage_pct=stop_slip,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            holding_seconds=max(0.0, (exit_time - pos.entry_time).total_seconds()),
            strategy_id=pos.strategy_id,
            signal_id=pos.signal_id,
            signal_timestamp=pos.signal_timestamp,
            entry_confidence=pos.entry_confidence,
            max_favorable_excursion_pct=pos.max_favorable_excursion_pct,
            max_adverse_excursion_pct=pos.max_adverse_excursion_pct,
        )
        self.state.closed_trades.append(trade)
        self._refresh_unrealized()
        self._update_peak_and_drawdown()
        self.assert_invariants()
        logger.info(
            "paper_position_closed",
            symbol=symbol,
            reason=exit_reason,
            net_pnl=round(net, 2),
            return_pct=round(return_pct, 4),
        )
        return trade

    def reduce_position(
        self,
        symbol: str,
        exit_price: float,
        sell_qty: float,
        fees: float = 0.0,
        slippage: float = 0.0,
        exit_reason: str = "partial_exit",
        *,
        exit_reference_price: float | None = None,
        embedded_slippage_cost: float = 0.0,
    ) -> ClosedTrade | None:
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return None
        actual_sell = min(sell_qty, pos.quantity)
        if actual_sell <= 0:
            return None

        old_quantity = pos.quantity
        ratio = actual_sell / old_quantity if old_quantity > 0 else 1.0
        exit_reference = exit_reference_price or exit_price
        entry_reference = pos.entry_reference_price or pos.entry_price
        exit_notional = exit_price * actual_sell
        if pos.direction == "long":
            gross = (exit_reference - entry_reference) * actual_sell
        else:
            gross = (entry_reference - exit_reference) * actual_sell

        allocated_entry_fee = pos.fees_paid * ratio
        allocated_entry_slippage = pos.entry_slippage_cost * ratio
        exit_slippage = max(0.0, embedded_slippage_cost) + max(0.0, slippage)
        total_slippage = allocated_entry_slippage + exit_slippage
        net = gross - fees - allocated_entry_fee - total_slippage

        pos.quantity -= actual_sell
        pos.notional = pos.entry_price * pos.quantity
        pos.fees_paid -= allocated_entry_fee
        pos.entry_slippage_cost -= allocated_entry_slippage
        self.state.allocated -= pos.entry_price * actual_sell
        if abs(self.state.allocated) < 1e-9:
            self.state.allocated = 0.0
        self.state.cash += exit_notional - fees - max(0.0, slippage)
        self.state.total_fees += fees
        self.state.total_slippage += exit_slippage
        self.state.realized_pnl += net
        self.state.trade_count += 1
        if net > 0:
            self.state.win_count += 1
        else:
            self.state.loss_count += 1

        exit_time = datetime.now(UTC)
        return_pct = (
            net / (pos.entry_price * actual_sell) * 100 if pos.entry_price > 0 else 0.0
        )
        trade = ClosedTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=actual_sell,
            gross_pnl=gross,
            fees=allocated_entry_fee + fees,
            entry_fee=allocated_entry_fee,
            exit_fee=fees,
            slippage_cost=total_slippage,
            net_pnl=net,
            return_pct=return_pct,
            exit_reason=exit_reason,
            target_stop=pos.stop_loss_price,
            actual_exit=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            holding_seconds=max(0.0, (exit_time - pos.entry_time).total_seconds()),
            strategy_id=pos.strategy_id,
            signal_id=pos.signal_id,
            signal_timestamp=pos.signal_timestamp,
            entry_confidence=pos.entry_confidence,
            max_favorable_excursion_pct=pos.max_favorable_excursion_pct,
            max_adverse_excursion_pct=pos.max_adverse_excursion_pct,
        )
        self.state.closed_trades.append(trade)
        if pos.quantity <= 1e-12:
            del self.state.open_positions[symbol]
        self._refresh_unrealized()
        self._update_peak_and_drawdown()
        self.assert_invariants()
        logger.info(
            "paper_position_reduced",
            symbol=symbol,
            sold=round(actual_sell, 6),
            remaining=round(pos.quantity, 6),
            pnl=round(net, 4),
        )
        return trade

    def _update_peak_and_drawdown(self) -> None:
        equity = self.state.equity
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
        if self.state.peak_equity > 0:
            drawdown = (self.state.peak_equity - equity) / self.state.peak_equity * 100
            if drawdown > self.state.max_drawdown_pct:
                self.state.max_drawdown_pct = drawdown

    def update_market_price(self, symbol: str, price: float) -> None:
        pos = self.state.open_positions.get(symbol)
        if pos is None or price <= 0:
            return
        pos.current_price = price
        if pos.direction == "long":
            pos.unrealized_pnl = (price - pos.entry_price) * pos.quantity
        else:
            pos.unrealized_pnl = (pos.entry_price - price) * pos.quantity
        pos.unrealized_pnl_pct = (
            pos.unrealized_pnl / pos.notional * 100 if pos.notional > 0 else 0.0
        )
        pos.max_favorable_excursion_pct = max(
            pos.max_favorable_excursion_pct, pos.unrealized_pnl_pct, 0.0
        )
        pos.max_adverse_excursion_pct = min(
            pos.max_adverse_excursion_pct, pos.unrealized_pnl_pct, 0.0
        )
        if (price > pos.trail_peak and pos.direction == "long") or (
            price < pos.trail_peak and pos.direction == "short"
        ):
            pos.trail_peak = price
        self._refresh_unrealized()
        self._update_peak_and_drawdown()

    def _refresh_unrealized(self) -> None:
        self.state.unrealized_pnl = sum(
            position.unrealized_pnl for position in self.state.open_positions.values()
        )

    def assert_invariants(self, tolerance: float = 1e-6) -> None:
        """Raise immediately if cash/exposure/equity accounting diverges."""
        state = self.state
        tol = max(tolerance, abs(state.initial_balance) * 1e-9)
        open_notional = sum(position.notional for position in state.open_positions.values())
        open_entry_fees = sum(position.fees_paid for position in state.open_positions.values())
        expected_cash = (
            state.initial_balance + state.realized_pnl - open_notional - open_entry_fees
        )
        expected_equity = state.cash + state.allocated + state.unrealized_pnl

        assert math.isfinite(expected_equity), "equity must be finite"
        assert state.cash >= -tol, f"cash is negative: {state.cash}"
        assert state.allocated >= -tol, f"allocated is negative: {state.allocated}"
        assert all(position.quantity >= -tol for position in state.open_positions.values())
        assert abs(state.allocated - open_notional) <= tol, (
            f"allocated mismatch: state={state.allocated}, positions={open_notional}"
        )
        assert abs(state.cash - expected_cash) <= tol, (
            f"cash mismatch: actual={state.cash}, expected={expected_cash}"
        )
        assert abs(state.equity - expected_equity) <= tol
        assert state.total_fees >= -tol
        assert state.total_slippage >= -tol

    def update_daily(self) -> None:
        self.state.daily_pnl = self.state.realized_pnl
        self.state.daily_start_equity = self.state.equity
        self.state.updated_at = datetime.now(UTC)

    def summary(self) -> dict[str, Any]:
        state = self.state
        return {
            "initial_balance": round(state.initial_balance, 2),
            "equity": round(state.equity, 2),
            "cash": round(state.cash, 2),
            "allocated": round(state.allocated, 2),
            "unrealized_pnl": round(state.unrealized_pnl, 2),
            "realized_pnl": round(state.realized_pnl, 2),
            "total_fees": round(state.total_fees, 4),
            "total_slippage": round(state.total_slippage, 4),
            "trade_count": state.trade_count,
            "win_count": state.win_count,
            "loss_count": state.loss_count,
            "win_rate": state.win_count / state.trade_count if state.trade_count > 0 else 0.0,
            "open_positions": len(state.open_positions),
            "max_drawdown_pct": round(state.max_drawdown_pct, 4),
        }
