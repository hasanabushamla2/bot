"""Paper Account — complete simulated portfolio accounting for paper trading."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PaperPosition:
    symbol: str = ""
    direction: str = "long"
    entry_price: float = 0.0
    quantity: float = 0.0
    notional: float = 0.0
    stop_loss_price: float = 0.0
    trail_activated: bool = False
    trail_peak: float = 0.0
    trail_level: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    fees_paid: float = 0.0
    is_open: bool = True
    strategy_id: str = ""
    opportunity_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClosedTrade:
    symbol: str = ""
    direction: str = "long"
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0
    net_pnl: float = 0.0
    return_pct: float = 0.0
    exit_reason: str = ""
    target_stop: float = 0.0
    actual_exit: float = 0.0
    stop_slippage_pct: float = 0.0
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exit_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    strategy_id: str = ""


@dataclass
class PaperAccountState:
    initial_balance: float = 10_000.0
    cash: float = 10_000.0
    allocated: float = 0.0
    reserved: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    peak_equity: float = 10_000.0
    max_drawdown_pct: float = 0.0
    daily_start_equity: float = 10_000.0
    daily_pnl: float = 0.0
    open_positions: dict[str, PaperPosition] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
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
    ) -> PaperPosition | None:
        notional = entry_price * quantity
        # N-12: Reject duplicate same-symbol position
        if symbol in self.state.open_positions:
            logger.warning("paper_duplicate_blocked", symbol=symbol)
            return None
        cost = notional + fees
        if cost > self.state.cash:
            logger.warning("paper_insufficient_cash", needed=cost, available=self.state.cash)
            return None
        self.state.cash -= cost
        self.state.allocated += notional
        self.state.total_fees += fees
        pos = PaperPosition(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            notional=notional,
            stop_loss_price=stop_loss_price,
            fees_paid=fees,
            strategy_id=strategy_id,
            opportunity_id=opportunity_id,
            trail_peak=entry_price,
        )
        self.state.open_positions[symbol] = pos
        logger.info(
            "paper_position_opened", symbol=symbol, notional=round(notional, 2), fees=round(fees, 4)
        )
        return pos

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        fees: float = 0.0,
        slippage: float = 0.0,
        exit_reason: str = "signal",
        trail_peak: float = 0.0,
        trail_level: float = 0.0,
    ) -> ClosedTrade | None:
        pos = self.state.open_positions.pop(symbol, None)
        if pos is None:
            return None
        exit_notional = exit_price * pos.quantity
        self.state.allocated -= pos.notional
        self.state.total_fees += fees
        self.state.total_slippage += slippage
        if pos.direction == "long":
            gross = (exit_price - pos.entry_price) * pos.quantity
        else:
            gross = (pos.entry_price - exit_price) * pos.quantity
        net = gross - fees - slippage - pos.fees_paid
        self.state.cash += exit_notional - fees
        self.state.realized_pnl += net
        self.state.trade_count += 1
        if net > 0:
            self.state.win_count += 1
        else:
            self.state.loss_count += 1
        return_pct = (net / pos.notional * 100) if pos.notional > 0 else 0.0
        stop_slip = 0.0
        if exit_reason == "hard_stop" and pos.stop_loss_price > 0:
            stop_slip = (exit_price - pos.stop_loss_price) / pos.stop_loss_price * 100
            if pos.direction == "long":
                stop_slip = -abs(stop_slip)
        trade = ClosedTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            gross_pnl=gross,
            fees=fees + pos.fees_paid,
            slippage_cost=slippage,
            net_pnl=net,
            return_pct=return_pct,
            exit_reason=exit_reason,
            target_stop=pos.stop_loss_price,
            actual_exit=exit_price,
            stop_slippage_pct=stop_slip,
            entry_time=pos.entry_time,
            exit_time=datetime.now(UTC),
            strategy_id=pos.strategy_id,
        )
        self.state.closed_trades.append(trade)
        # R11: Recompute unrealized PnL from remaining positions
        self.state.unrealized_pnl = sum(
            p.unrealized_pnl for p in self.state.open_positions.values()
        )
        eq = self.state.equity
        if eq > self.state.peak_equity:
            self.state.peak_equity = eq
        if self.state.peak_equity > 0:
            dd = (self.state.peak_equity - eq) / self.state.peak_equity * 100
            if dd > self.state.max_drawdown_pct:
                self.state.max_drawdown_pct = dd
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
        exit_reason: str = "partial",
    ) -> ClosedTrade | None:
        """Reduce position quantity without fully closing. BLOCKER 6 fix."""
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return None
        actual_sell = min(sell_qty, pos.quantity)
        if actual_sell <= 0:
            return None
        exit_notional = exit_price * actual_sell
        ratio = actual_sell / pos.quantity if pos.quantity > 0 else 1.0
        # P&L for the sold portion
        gross = (exit_price - pos.entry_price) * actual_sell
        partial_fees = fees + (pos.fees_paid * ratio)
        net = gross - partial_fees - slippage
        # Update position
        pos.quantity -= actual_sell
        pos.notional = pos.entry_price * pos.quantity
        pos.fees_paid *= 1.0 - ratio
        # Update account
        self.state.allocated -= pos.entry_price * actual_sell
        # R11: Recompute unrealized after reduction
        self.state.cash += exit_notional - fees
        self.state.total_fees += fees
        self.state.total_slippage += slippage
        self.state.realized_pnl += net
        self.state.trade_count += 1
        if net > 0:
            self.state.win_count += 1
        else:
            self.state.loss_count += 1
        return_pct = (net / (pos.entry_price * actual_sell) * 100) if pos.entry_price > 0 else 0.0
        trade = ClosedTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=actual_sell,
            gross_pnl=gross,
            fees=partial_fees,
            slippage_cost=slippage,
            net_pnl=net,
            return_pct=return_pct,
            exit_reason=exit_reason,
            target_stop=pos.stop_loss_price,
            actual_exit=exit_price,
            stop_slippage_pct=0.0,
            entry_time=pos.entry_time,
            exit_time=datetime.now(UTC),
            strategy_id=pos.strategy_id,
        )
        self.state.closed_trades.append(trade)
        # If fully closed, remove position
        if pos.quantity <= 0:
            del self.state.open_positions[symbol]
        # R11: Recompute unrealized PnL from remaining positions
        self.state.unrealized_pnl = sum(
            p.unrealized_pnl for p in self.state.open_positions.values()
        )
        # Update drawdown
        eq = self.state.equity
        if eq > self.state.peak_equity:
            self.state.peak_equity = eq
        if self.state.peak_equity > 0:
            dd = (self.state.peak_equity - eq) / self.state.peak_equity * 100
            if dd > self.state.max_drawdown_pct:
                self.state.max_drawdown_pct = dd
        logger.info(
            "paper_position_reduced",
            symbol=symbol,
            sold=round(actual_sell, 6),
            remaining=round(pos.quantity, 6),
            pnl=round(net, 4),
        )
        return trade

    def update_market_price(self, symbol: str, price: float) -> None:
        pos = self.state.open_positions.get(symbol)
        if pos is None:
            return
        pos.current_price = price
        if pos.direction == "long":
            pos.unrealized_pnl = (price - pos.entry_price) * pos.quantity
        else:
            pos.unrealized_pnl = (pos.entry_price - price) * pos.quantity
        pos.unrealized_pnl_pct = (
            (pos.unrealized_pnl / pos.notional * 100) if pos.notional > 0 else 0.0
        )
        if (price > pos.trail_peak and pos.direction == "long") or (
            price < pos.trail_peak and pos.direction == "short"
        ):
            pos.trail_peak = price
        total_unrealized = sum(p.unrealized_pnl for p in self.state.open_positions.values())
        self.state.unrealized_pnl = total_unrealized

    def update_daily(self) -> None:
        self.state.daily_pnl = self.state.realized_pnl
        self.state.daily_start_equity = self.state.equity
        self.state.updated_at = datetime.now(UTC)

    def summary(self) -> dict[str, Any]:
        s = self.state
        return {
            "equity": round(s.equity, 2),
            "cash": round(s.cash, 2),
            "allocated": round(s.allocated, 2),
            "unrealized_pnl": round(s.unrealized_pnl, 2),
            "realized_pnl": round(s.realized_pnl, 2),
            "total_fees": round(s.total_fees, 4),
            "total_slippage": round(s.total_slippage, 4),
            "trade_count": s.trade_count,
            "win_count": s.win_count,
            "loss_count": s.loss_count,
            "win_rate": s.win_count / s.trade_count if s.trade_count > 0 else 0.0,
            "open_positions": len(s.open_positions),
            "max_drawdown_pct": round(s.max_drawdown_pct, 2),
        }
