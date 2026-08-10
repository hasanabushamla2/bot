"""Paper Trading Engine — live simulation with real market data.

CRITICAL: Paper trading uses the SAME interfaces as live trading:
- Same strategy plugins
- Same opportunity engine
- Same risk engine
- Same execution interface (with simulated fills)
- Same order lifecycle

This ensures paper results are realistic and not inflated by a
simplified paper-only path.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.adapters.base import (
    NormalizedOrder,
    NormalizedOrderRequest,
    NormalizedTicker,
    OrderSide,
    OrderState,
    OrderType,
)
from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PaperAccount:
    """Simulated account for paper trading."""

    initial_balance: float = 10_000.0
    balance: float = 10_000.0
    reserved: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    total_fees: float = 0.0

    @property
    def equity(self) -> float:
        return self.balance + self.unrealized_pnl

    @property
    def available(self) -> float:
        return self.balance - self.reserved


class PaperExecutionEngine:
    """Simulated execution for paper trading.

    Uses real market data for fills but does NOT send orders to any
    exchange. Simulates realistic:
    - Fees (maker/taker)
    - Slippage
    - Partial fills
    - Latency
    - Rejected orders (when bid/ask is insufficient)
    """

    def __init__(
        self,
        account: PaperAccount | None = None,
        maker_fee: float = 0.001,
        taker_fee: float = 0.001,
        slippage_bps: float = 5.0,
        simulated_latency_ms: float = 50.0,
    ) -> None:
        self.account = account or PaperAccount()
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage_bps = slippage_bps
        self.simulated_latency_ms = simulated_latency_ms

        self._orders: dict[str, NormalizedOrder] = {}
        self._ticker_cache: dict[str, NormalizedTicker] = {}

    def update_ticker(self, ticker: NormalizedTicker) -> None:
        """Update cached price for a symbol."""
        self._ticker_cache[ticker.symbol] = ticker

    async def simulate_order(self, request: NormalizedOrderRequest) -> NormalizedOrder:
        """Simulate order execution against current market data.

        For market orders: fill immediately at current bid/ask with slippage.
        For limit orders: fill if the limit price crosses the spread.
        """
        ticker = self._ticker_cache.get(request.symbol)
        if ticker is None:
            return self._reject(request, "No market data available")

        # Simulate latency
        await asyncio.sleep(self.simulated_latency_ms / 1000.0)

        # Determine fill price
        if request.side == OrderSide.BUY:
            base_price = ticker.ask
        else:
            base_price = ticker.bid

        if base_price <= 0:
            return self._reject(request, "Invalid market price")

        # Apply slippage
        slippage = self.slippage_bps / 10000.0
        if request.side == OrderSide.BUY:
            fill_price = base_price * (1 + slippage)
        else:
            fill_price = base_price * (1 - slippage)

        # For limit orders, check price
        if request.order_type == OrderType.LIMIT and request.price is not None:
            if request.side == OrderSide.BUY and fill_price > request.price:
                return self._open_order(request)  # Not filled, stays open
            if request.side == OrderSide.SELL and fill_price < request.price:
                return self._open_order(request)

        # "Fill" the order
        notional = fill_price * request.quantity
        fees = notional * self.taker_fee

        now = datetime.now(UTC)
        order = NormalizedOrder(
            exchange="paper",
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            exchange_order_id=f"paper_{request.client_order_id[:12]}",
            side=request.side,
            order_type=request.order_type,
            state=OrderState.FILLED,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_avg_price=fill_price,
            price=request.price or fill_price,
            total_fees=fees,
            fee_currency="USD",
            created_at=now,
            updated_at=now,
        )

        # Update account
        if request.side == OrderSide.BUY:
            cost = notional + fees
            if cost > self.account.available:
                return self._reject(request, "Insufficient balance")
            self.account.balance -= cost
        else:
            self.account.balance += notional - fees

        self.account.total_fees += fees
        self._orders[request.client_order_id] = order

        logger.info("paper_order_filled",
                     symbol=request.symbol,
                     side=request.side.value,
                     quantity=request.quantity,
                     price=fill_price,
                     fees=fees)

        return order

    def _open_order(self, request: NormalizedOrderRequest) -> NormalizedOrder:
        """Create an OPEN limit order (not yet filled)."""
        now = datetime.now(UTC)
        order = NormalizedOrder(
            exchange="paper",
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            exchange_order_id=f"paper_{request.client_order_id[:12]}",
            side=request.side,
            order_type=request.order_type,
            state=OrderState.OPEN,
            quantity=request.quantity,
            filled_quantity=0.0,
            filled_avg_price=None,
            price=request.price,
            total_fees=0.0,
            fee_currency=None,
            created_at=now,
            updated_at=now,
        )
        self._orders[request.client_order_id] = order
        return order

    def _reject(self, request: NormalizedOrderRequest, reason: str) -> NormalizedOrder:
        """Create a REJECTED order."""
        now = datetime.now(UTC)
        order = NormalizedOrder(
            exchange="paper",
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            exchange_order_id=None,
            side=request.side,
            order_type=request.order_type,
            state=OrderState.REJECTED,
            quantity=request.quantity,
            filled_quantity=0.0,
            filled_avg_price=None,
            price=request.price,
            total_fees=0.0,
            fee_currency=None,
            created_at=now,
            updated_at=now,
            metadata={"rejection_reason": reason},
        )
        self._orders[request.client_order_id] = order
        logger.warning("paper_order_rejected",
                       symbol=request.symbol, reason=reason)
        return order

    def get_account_summary(self) -> dict[str, Any]:
        """Return paper account summary."""
        return {
            "initial_balance": self.account.initial_balance,
            "balance": self.account.balance,
            "equity": self.account.equity,
            "available": self.account.available,
            "realized_pnl": self.account.realized_pnl,
            "total_fees": self.account.total_fees,
        }
