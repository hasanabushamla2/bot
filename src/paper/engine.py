"""PaperExecutionEngine — R5: depth-walk execution with VWAP, partial fills."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.core.logging_config import get_logger

logger = get_logger(__name__)


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
            levels = asks_depth or [(ask, 10.0)] if ask > 0 else [(ref_price, 10.0)]
        else:
            levels = bids_depth or [(bid, 10.0)] if bid > 0 else [(ref_price, 10.0)]
        filled, vwap, levels_used, remaining = self.depth_walk(
            side, quantity, bids=levels, asks=levels, top_of_book=ref_price
        )
        if filled <= 0:
            filled = quantity  # fallback to single-level fill
            vwap = ref_price
            remaining = 0.0

        # Apply slippage to VWAP
        s = self.slippage_bps / 10000.0
        if side == "buy":
            fill_price = vwap * (1.0 + s)
        else:
            fill_price = vwap * (1.0 - s)
        notional = fill_price * filled
        fees = notional * self.taker_fee

        # Compute adverse slippage bps
        if side == "buy":
            bps = (fill_price - ref_price) / ref_price * 10000
        else:
            bps = (ref_price - fill_price) / ref_price * 10000

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
