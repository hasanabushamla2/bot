"""R3: PaperExecutionEngine — realistic simulated fills using bid/ask/spread."""

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
    fill_price: float = 0.0
    average_price: float = 0.0
    fees: float = 0.0
    slippage_bps: float = 0.0
    status: str = "FILLED"
    latency_ms: float = 50.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class PaperExecutionEngine:
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

    async def simulate_fill(
        self,
        symbol: str,
        side: str,
        quantity: float,
        bid: float = 0.0,
        ask: float = 0.0,
        last: float = 0.0,
    ) -> PaperFillResult:
        if side == "buy":
            ref_price = ask if ask > 0 else (last if last > 0 else 0)
        else:
            ref_price = bid if bid > 0 else (last if last > 0 else 0)
        if ref_price <= 0:
            return PaperFillResult(
                symbol=symbol, side=side, requested_qty=quantity, status="REJECTED"
            )
        await asyncio.sleep(self.simulated_latency_ms / 1000.0)
        s = self.slippage_bps / 10000.0
        if side == "buy":
            fill_price = ref_price * (1 + s)
        else:
            fill_price = ref_price * (1 - s)
        filled_qty = quantity
        status = "FILLED"
        if self.partial_fill_probability > 0:
            import random

            if random.random() < self.partial_fill_probability:
                filled_qty = quantity * random.uniform(0.25, 0.75)
                status = "PARTIALLY_FILLED"
        notional = fill_price * filled_qty
        fees = notional * self.taker_fee
        bps = (
            (fill_price - ref_price) / ref_price * 10000
            if side == "buy"
            else (ref_price - fill_price) / ref_price * 10000
        )
        return PaperFillResult(
            symbol=symbol,
            side=side,
            requested_qty=quantity,
            filled_qty=filled_qty,
            remaining_qty=quantity - filled_qty,
            fill_price=fill_price,
            average_price=fill_price,
            fees=fees,
            slippage_bps=abs(bps),
            status=status,
            latency_ms=self.simulated_latency_ms,
        )
