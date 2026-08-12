"""Pre-Trade Execution Estimator — depth-walk simulation & execution-aware position sizing.

Performs:
1. BUY Entry Simulation (VWAP, slippage, levels, participation).
2. Hypothetical SELL Emergency Exit Simulation (exit VWAP, exit slippage, effective stop loss).
3. Execution-Aware Position Sizing (walks the order book to ensure sizing never exceeds safe depth).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.logging_config import get_logger
from src.data.order_book import OrderBookState
from src.execution.liquidity_gate import LiquidityRejectionReason

logger = get_logger(__name__)


@dataclass
class EntryExecutionEstimate:
    symbol: str = ""
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    expected_vwap: float = 0.0
    expected_slippage_bps: float = 0.0
    levels_consumed: int = 0
    available_depth_qty: float = 0.0
    participation_pct: float = 0.0
    passed: bool = False
    rejection_reason: LiquidityRejectionReason | None = None
    message: str = ""


@dataclass
class ExitExecutionEstimate:
    symbol: str = ""
    position_qty: float = 0.0
    exit_vwap: float = 0.0
    exit_slippage_bps: float = 0.0
    levels_consumed: int = 0
    effective_stop_loss_pct: float = 0.0
    passed: bool = False
    rejection_reason: LiquidityRejectionReason | None = None
    message: str = ""


class ExecutionEstimator:
    """Simulates realistic book-walk execution for entries and hypothetical exits."""

    def __init__(
        self,
        max_entry_slippage_bps: float = 25.0,
        max_exit_slippage_bps: float = 35.0,
        max_levels_consumed: int = 8,
        max_depth_participation_pct: float = 0.10,
        max_effective_stop_loss_pct: float = 0.80,
    ) -> None:
        self.max_entry_slippage_bps = max_entry_slippage_bps
        self.max_exit_slippage_bps = max_exit_slippage_bps
        self.max_levels_consumed = max_levels_consumed
        self.max_depth_participation_pct = max_depth_participation_pct
        self.max_effective_stop_loss_pct = max_effective_stop_loss_pct

    def simulate_buy_entry(
        self,
        symbol: str,
        book: OrderBookState,
        requested_qty: float,
        max_slippage_bps: float | None = None,
        max_levels: int | None = None,
        max_participation_pct: float | None = None,
    ) -> EntryExecutionEstimate:
        """Simulate buying `requested_qty` by walking the order book asks."""
        max_slip = max_slippage_bps if max_slippage_bps is not None else self.max_entry_slippage_bps
        max_lev = max_levels if max_levels is not None else self.max_levels_consumed
        max_part = max_participation_pct if max_participation_pct is not None else self.max_depth_participation_pct

        if requested_qty <= 0:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                message="Requested quantity must be positive",
            )

        asks = book.asks.levels
        if not asks:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_DATA_MISSING,
                message="No asks in order book",
            )

        best_ask = asks[0][0]
        if best_ask <= 0:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.MALFORMED_BOOK,
                message="Best ask <= 0",
            )

        total_visible_qty = sum(q for _, q in asks)
        remaining = requested_qty
        total_cost = 0.0
        total_qty = 0.0
        levels_used = 0

        for price, qty in asks:
            if remaining <= 0:
                break
            fill = min(remaining, qty)
            total_cost += fill * price
            total_qty += fill
            remaining -= fill
            levels_used += 1

        if total_qty <= 0:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                message="Zero fill possible from asks",
            )

        vwap = total_cost / total_qty
        slippage_bps = (vwap - best_ask) / best_ask * 10000.0
        participation_pct = (total_qty / total_visible_qty) if total_visible_qty > 0 else 1.0

        # Check depth exhaustion (insufficient depth for full requested quantity)
        if remaining > 0.0001:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, filled_qty=total_qty,
                remaining_qty=remaining, expected_vwap=vwap, expected_slippage_bps=slippage_bps,
                levels_consumed=levels_used, available_depth_qty=total_visible_qty,
                participation_pct=participation_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                message=f"Visible book depth ({total_qty:.4f}) insufficient for requested {requested_qty:.4f}",
            )

        # Check slippage
        if slippage_bps > max_slip:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, filled_qty=total_qty,
                remaining_qty=remaining, expected_vwap=vwap, expected_slippage_bps=slippage_bps,
                levels_consumed=levels_used, available_depth_qty=total_visible_qty,
                participation_pct=participation_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.ENTRY_SLIPPAGE_TOO_HIGH,
                message=f"Entry slippage {slippage_bps:.1f} bps > max {max_slip:.1f} bps",
            )

        # Check levels consumed
        if levels_used > max_lev:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, filled_qty=total_qty,
                remaining_qty=remaining, expected_vwap=vwap, expected_slippage_bps=slippage_bps,
                levels_consumed=levels_used, available_depth_qty=total_visible_qty,
                participation_pct=participation_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                message=f"Entry consumes {levels_used} book levels > max {max_lev}",
            )

        # Check participation rate
        if participation_pct > max_part:
            return EntryExecutionEstimate(
                symbol=symbol, requested_qty=requested_qty, filled_qty=total_qty,
                remaining_qty=remaining, expected_vwap=vwap, expected_slippage_bps=slippage_bps,
                levels_consumed=levels_used, available_depth_qty=total_visible_qty,
                participation_pct=participation_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.PARTICIPATION_TOO_HIGH,
                message=f"Participation rate {participation_pct*100:.1f}% > max {max_part*100:.1f}%",
            )

        return EntryExecutionEstimate(
            symbol=symbol, requested_qty=requested_qty, filled_qty=total_qty,
            remaining_qty=0.0, expected_vwap=vwap, expected_slippage_bps=slippage_bps,
            levels_consumed=levels_used, available_depth_qty=total_visible_qty,
            participation_pct=participation_pct, passed=True,
            message="Entry execution estimate safe",
        )

    def simulate_sell_exit(
        self,
        symbol: str,
        book: OrderBookState,
        position_qty: float,
        stop_loss_pct: float = 0.30,
        max_exit_slippage_bps: float | None = None,
        max_effective_stop_loss_pct: float | None = None,
    ) -> ExitExecutionEstimate:
        """Simulate selling `position_qty` to estimate emergency exit liquidity and effective stop loss."""
        max_slip = max_exit_slippage_bps if max_exit_slippage_bps is not None else self.max_exit_slippage_bps
        max_eff_stop = (
            max_effective_stop_loss_pct
            if max_effective_stop_loss_pct is not None
            else self.max_effective_stop_loss_pct
        )

        if position_qty <= 0:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                message="Position quantity must be positive",
            )

        bids = book.bids.levels
        if not bids:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.BOOK_DATA_MISSING,
                message="No bids in order book for exit",
            )

        best_bid = bids[0][0]
        if best_bid <= 0:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, passed=False,
                rejection_reason=LiquidityRejectionReason.MALFORMED_BOOK,
                message="Best bid <= 0",
            )

        remaining = position_qty
        total_revenue = 0.0
        total_qty = 0.0
        levels_used = 0

        for price, qty in bids:
            if remaining <= 0:
                break
            fill = min(remaining, qty)
            total_revenue += fill * price
            total_qty += fill
            remaining -= fill
            levels_used += 1

        if remaining > 0.0001:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, exit_vwap=0.0,
                exit_slippage_bps=9999.0, levels_consumed=levels_used,
                effective_stop_loss_pct=99.0, passed=False,
                rejection_reason=LiquidityRejectionReason.EXIT_SLIPPAGE_TOO_HIGH,
                message=f"Exit depth exhausted ({total_qty:.4f} < {position_qty:.4f})",
            )

        exit_vwap = total_revenue / total_qty
        exit_slippage_bps = (best_bid - exit_vwap) / best_bid * 10000.0
        effective_stop_loss_pct = stop_loss_pct + (exit_slippage_bps / 100.0)

        if exit_slippage_bps > max_slip:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, exit_vwap=exit_vwap,
                exit_slippage_bps=exit_slippage_bps, levels_consumed=levels_used,
                effective_stop_loss_pct=effective_stop_loss_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.EXIT_SLIPPAGE_TOO_HIGH,
                message=f"Exit slippage {exit_slippage_bps:.1f} bps > max {max_slip:.1f} bps",
            )

        if effective_stop_loss_pct > max_eff_stop:
            return ExitExecutionEstimate(
                symbol=symbol, position_qty=position_qty, exit_vwap=exit_vwap,
                exit_slippage_bps=exit_slippage_bps, levels_consumed=levels_used,
                effective_stop_loss_pct=effective_stop_loss_pct, passed=False,
                rejection_reason=LiquidityRejectionReason.EFFECTIVE_STOP_LOSS_TOO_HIGH,
                message=f"Effective stop loss {effective_stop_loss_pct:.2f}% > max {max_eff_stop:.2f}%",
            )

        return ExitExecutionEstimate(
            symbol=symbol, position_qty=position_qty, exit_vwap=exit_vwap,
            exit_slippage_bps=exit_slippage_bps, levels_consumed=levels_used,
            effective_stop_loss_pct=effective_stop_loss_pct, passed=True,
            message="Exit execution estimate safe",
        )

    def compute_max_safe_quantity(
        self,
        symbol: str,
        book: OrderBookState,
        risk_qty: float,
        capital_qty: float,
        strategy_qty: float = float("inf"),
        stop_loss_pct: float = 0.30,
    ) -> float:
        """Compute execution-aware maximum quantity:
        min(risk_qty, capital_qty, liquidity_qty, participation_qty, executable_depth_qty, strategy_qty).
        """
        asks = book.asks.levels
        bids = book.bids.levels
        if not asks or not bids:
            return 0.0

        best_ask = asks[0][0]
        if best_ask <= 0:
            return 0.0

        total_ask_qty = sum(q for _, q in asks)
        total_bid_qty = sum(q for _, q in bids)

        # 1. Participation based quantity limit (10% of visible depth)
        participation_based_qty = min(total_ask_qty, total_bid_qty) * self.max_depth_participation_pct

        # 2. Depth based quantity limit within max levels
        depth_within_levels_asks = sum(q for _, q in asks[: self.max_levels_consumed])
        depth_within_levels_bids = sum(q for _, q in bids[: self.max_levels_consumed])
        executable_depth_qty = min(depth_within_levels_asks, depth_within_levels_bids)

        # 3. Liquidity based quantity (walk asks until slippage exceeds max_entry_slippage_bps)
        max_slip_price = best_ask * (1.0 + self.max_entry_slippage_bps / 10000.0)
        liquidity_based_qty = 0.0
        for p, q in asks:
            if p <= max_slip_price:
                liquidity_based_qty += q
            else:
                break

        # Max allowed quantity is the minimum across all constraints
        max_allowed_qty = min(
            risk_qty,
            capital_qty,
            liquidity_based_qty,
            participation_based_qty,
            executable_depth_qty,
            strategy_qty,
        )

        return max(0.0, max_allowed_qty)
