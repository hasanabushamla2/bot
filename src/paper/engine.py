"""PaperExecutionEngine — R5: depth-walk execution with VWAP, partial fills."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class RoundTripCostEstimate:
    """Pre-entry round-trip cost estimate in both USD and return-fraction units."""

    notional: float = 0.0
    estimated_entry_fee: float = 0.0
    estimated_exit_fee: float = 0.0
    estimated_spread_cost: float = 0.0
    estimated_slippage: float = 0.0
    estimated_round_trip_cost: float = 0.0
    estimated_round_trip_cost_fraction: float = 0.0
    entry_fill_price: float = 0.0
    exit_fill_price: float = 0.0


@dataclass
class ExpectedNetEdgeEstimate:
    """Explicit pre-entry expected-edge calculation, net of all modeled costs."""

    expected_gross_edge_fraction: float = 0.0
    expected_gross_edge_usd: float = 0.0
    estimated_entry_fee: float = 0.0
    estimated_exit_fee: float = 0.0
    estimated_spread_cost: float = 0.0
    expected_slippage: float = 0.0
    safety_buffer_fraction: float = 0.0
    safety_buffer_usd: float = 0.0
    expected_net_edge_fraction: float = 0.0
    expected_net_edge_usd: float = 0.0
    costs: RoundTripCostEstimate = field(default_factory=RoundTripCostEstimate)

    @property
    def is_positive_after_costs(self) -> bool:
        return self.expected_net_edge_fraction > 0.0


@dataclass
class PaperFillResult:
    symbol: str = ""
    side: str = ""
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    fill_price: float = 0.0  # VWAP of filled quantity
    average_price: float = 0.0  # same as fill_price for VWAP
    fees: float = 0.0
    slippage_bps: float = 0.0  # adverse slippage vs top-of-book
    status: str = "FILLED"
    latency_ms: float = 50.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Depth execution details
    levels_consumed: int = 0
    vwap_price: float = 0.0
    depth_exhausted: bool = False


class PaperExecutionEngine:
    """R5: Deterministic multi-level book depth execution with VWAP."""

    def __init__(
        self,
        maker_fee: float = 0.001,
        taker_fee: float = 0.001,
        slippage_bps: float = 5.0,
        simulated_latency_ms: float = 50.0,
        partial_fill_probability: float = 0.0,
    ) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage_bps = slippage_bps
        self.simulated_latency_ms = simulated_latency_ms
        self.partial_fill_probability = partial_fill_probability

    def estimate_round_trip_cost(
        self,
        quantity: float,
        entry_reference_price: float,
        exit_reference_price: float,
    ) -> RoundTripCostEstimate:
        """Estimate realistic round-trip fees, spread/depth, and slippage.

        Reference prices are normally entry/exit book-walk VWAPs.  The
        configured adverse slippage is then applied to each side exactly as it
        is in :meth:`simulate_fill`.
        """
        if quantity <= 0 or entry_reference_price <= 0 or exit_reference_price <= 0:
            return RoundTripCostEstimate()
        slip_fraction = self.slippage_bps / 10000.0
        entry_fill = entry_reference_price * (1.0 + slip_fraction)
        exit_fill = exit_reference_price * (1.0 - slip_fraction)
        notional = entry_reference_price * quantity
        entry_fee = entry_fill * quantity * self.taker_fee
        exit_fee = exit_fill * quantity * self.taker_fee
        entry_slippage = (entry_fill - entry_reference_price) * quantity
        exit_slippage = (exit_reference_price - exit_fill) * quantity
        spread_cost = max(0.0, entry_reference_price - exit_reference_price) * quantity
        modeled_slippage = max(0.0, entry_slippage + exit_slippage)
        total = entry_fee + exit_fee + spread_cost + modeled_slippage
        return RoundTripCostEstimate(
            notional=notional,
            estimated_entry_fee=entry_fee,
            estimated_exit_fee=exit_fee,
            estimated_spread_cost=spread_cost,
            estimated_slippage=modeled_slippage,
            estimated_round_trip_cost=total,
            estimated_round_trip_cost_fraction=(total / notional if notional > 0 else 0.0),
            entry_fill_price=entry_fill,
            exit_fill_price=exit_fill,
        )

    def estimate_expected_net_edge(
        self,
        quantity: float,
        entry_reference_price: float,
        exit_reference_price: float,
        expected_gross_edge_fraction: float,
        safety_buffer_fraction: float = 0.0,
    ) -> ExpectedNetEdgeEstimate:
        """Calculate expected net edge before an entry is sent.

        The gross edge is a strategy estimate in decimal-fraction units.  Fees,
        bid/ask crossing, modeled adverse slippage, and a separately visible
        safety buffer are all subtracted.  The method deliberately does not
        infer a bullish return from the current book; callers must supply an
        independently generated expected gross edge.
        """
        costs = self.estimate_round_trip_cost(
            quantity, entry_reference_price, exit_reference_price
        )
        gross_fraction = max(0.0, expected_gross_edge_fraction)
        safety_fraction = max(0.0, safety_buffer_fraction)
        gross_usd = costs.notional * gross_fraction
        safety_usd = costs.notional * safety_fraction
        net_usd = gross_usd - costs.estimated_round_trip_cost - safety_usd
        net_fraction = net_usd / costs.notional if costs.notional > 0 else -safety_fraction
        return ExpectedNetEdgeEstimate(
            expected_gross_edge_fraction=gross_fraction,
            expected_gross_edge_usd=gross_usd,
            estimated_entry_fee=costs.estimated_entry_fee,
            estimated_exit_fee=costs.estimated_exit_fee,
            estimated_spread_cost=costs.estimated_spread_cost,
            expected_slippage=costs.estimated_slippage,
            safety_buffer_fraction=safety_fraction,
            safety_buffer_usd=safety_usd,
            expected_net_edge_fraction=net_fraction,
            expected_net_edge_usd=net_usd,
            costs=costs,
        )

    # ---- BLOCKER 5: Depth-walk execution ----
    def depth_walk(
        self,
        side: str,
        requested_qty: float,
        bids: list[tuple[float, float]] | None = None,
        asks: list[tuple[float, float]] | None = None,
        top_of_book: float = 0.0,
    ) -> tuple[float, float, int, float]:
        """Walk the order book and compute filled_qty, VWAP, levels consumed.

        Returns: (filled_qty, vwap, levels_consumed, remaining_qty)
        """
        if side == "buy":
            levels = asks or []
        else:
            levels = bids or []
        if not levels or requested_qty <= 0:
            return (0.0, top_of_book, 0, requested_qty)
        remaining = requested_qty
        total_cost = 0.0
        total_qty = 0.0
        levels_used = 0
        for price, qty in levels:
            if remaining <= 0:
                break
            fill = min(remaining, qty)
            total_cost += fill * price
            total_qty += fill
            remaining -= fill
            levels_used += 1
        if total_qty <= 0:
            return (0.0, top_of_book, 0, requested_qty)
        vwap = total_cost / total_qty
        return (total_qty, vwap, levels_used, remaining)

    async def simulate_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        bid: float = 0.0,
        ask: float = 0.0,
        last: float = 0.0,
        bids_depth: list[tuple[float, float]] | None = None,
        asks_depth: list[tuple[float, float]] | None = None,
    ) -> PaperFillResult:
        """R5: Simulate fill using depth walk or top-of-book fallback."""
        if quantity <= 0:
            return PaperFillResult(
                symbol=symbol, side=side, requested_qty=quantity, status="REJECTED"
            )
        # Simulate latency
        await asyncio.sleep(self.simulated_latency_ms / 1000.0)
        # Determine reference price
        if side == "buy":
            ref_price = ask if ask > 0 else (last if last > 0 else 0)
        else:
            ref_price = bid if bid > 0 else (last if last > 0 else 0)
        if ref_price <= 0:
            return PaperFillResult(
                symbol=symbol, side=side, requested_qty=quantity, status="REJECTED"
            )

        # ---- Depth walk execution ----
        if side == "buy":
            levels = asks_depth or ([(ask, 10.0)] if ask > 0 else [(ref_price, 10.0)])
            best_book_price = levels[0][0] if levels and levels[0][0] > 0 else ref_price
        else:
            levels = bids_depth or ([(bid, 10.0)] if bid > 0 else [(ref_price, 10.0)])
            best_book_price = levels[0][0] if levels and levels[0][0] > 0 else ref_price

        filled, vwap, levels_used, remaining = self.depth_walk(
            side, quantity, bids=levels, asks=levels, top_of_book=best_book_price
        )
        if filled <= 0:
            filled = quantity  # fallback to single-level fill
            vwap = best_book_price
            remaining = 0.0

        # Apply slippage to VWAP
        s = self.slippage_bps / 10000.0
        if side == "buy":
            fill_price = vwap * (1.0 + s)
        else:
            fill_price = vwap * (1.0 - s)
        notional = fill_price * filled
        fees = notional * self.taker_fee

        # Compute adverse slippage bps vs top-of-book
        if side == "buy":
            bps = (fill_price - best_book_price) / best_book_price * 10000
        else:
            bps = (best_book_price - fill_price) / best_book_price * 10000

        status = "FILLED"
        if remaining > 0.001:
            status = "PARTIALLY_FILLED_CANCELED"

        return PaperFillResult(
            symbol=symbol,
            side=side,
            requested_qty=quantity,
            filled_qty=filled,
            remaining_qty=remaining,
            fill_price=fill_price,
            average_price=vwap,
            fees=fees,
            slippage_bps=max(0.0, bps),
            status=status,
            latency_ms=self.simulated_latency_ms,
            levels_consumed=levels_used,
            vwap_price=vwap,
            depth_exhausted=(remaining > 0),
        )
