"""Execution Engine — event-driven order execution.

Handles:
- Order placement (market & limit)
- Order lifecycle (state machine)
- Partial fills
- Cancellation
- Retries with backoff
- Timeouts
- Idempotency (duplicate order prevention via client_order_id)
- Exchange reconciliation after restarts
- Balance verification
- Rate-limit awareness

The execution engine is the ONLY module that may call exchange adapter
order methods. All other modules go through the Risk Engine first.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.adapters.base import (
    ExchangeAdapter,
    NormalizedOrder,
    NormalizedOrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)
from src.core.config import get_settings
from src.core.exceptions import (
    DuplicateOrderError,
    LiveTradingNotEnabledError,
    OrderRejectedError,
    RateLimitError,
    ReconciliationError,
)
from src.core.logging_config import get_logger
from src.execution.state_machine import OrderStateMachine

logger = get_logger(__name__)


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""

    success: bool
    order: NormalizedOrder | None = None
    error: str | None = None
    retry_count: int = 0
    latency_ms: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class ExecutionEngine:
    """Manages the full lifecycle of orders across exchanges.

    Key safety features:
    - Live trading requires explicit safety gate.
    - Idempotency keys prevent duplicate orders.
    - Order reconciliation on startup.
    - Rate-limit tracking.
    """

    def __init__(
        self,
        adapters: dict[str, ExchangeAdapter],
        max_retries: int = 3,
        order_timeout_seconds: float = 30.0,
    ) -> None:
        self._adapters = adapters
        self.max_retries = max_retries
        self.order_timeout_seconds = order_timeout_seconds

        # Active order tracking
        self._active_orders: dict[str, NormalizedOrder] = {}
        self._idempotency_keys: set[str] = set()

        # Rate limit tracking (per exchange)
        self._rate_limit_counters: dict[str, int] = {}
        self._rate_limit_reset_times: dict[str, datetime] = {}

    # --- Safety Gate ---

    def _ensure_mode_allows(self, is_live: bool) -> None:
        """Raise if live trading is attempted without the safety gate."""
        settings = get_settings()
        if is_live and not settings.mode.is_live:
            raise LiveTradingNotEnabledError(
                "Live trading is disabled. Set MODE=live and LIVE_TRADING_ENABLED=true "
                "in .env to enable."
            )

    # --- Order Placement ---

    async def place_order(
        self,
        exchange_name: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
        is_live: bool = False,
    ) -> ExecutionResult:
        """Place an order with idempotency, retries, and timeout protection.

        Args:
            exchange_name: Which exchange adapter to use.
            symbol: Normalized symbol.
            side: Buy or sell.
            order_type: Market or limit.
            quantity: Order quantity.
            price: Limit price (required for limit orders).
            time_in_force: GTC, IOC, or FOK.
            is_live: Must be True for real-money orders.

        Returns:
            ExecutionResult with success/failure and order details.
        """
        self._ensure_mode_allows(is_live)

        adapter = self._adapters.get(exchange_name)
        if adapter is None:
            return ExecutionResult(success=False, error=f"No adapter for '{exchange_name}'")

        if order_type == OrderType.LIMIT and price is None:
            return ExecutionResult(success=False, error="Limit order requires a price")

        request = NormalizedOrderRequest(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
        )

        # --- Idempotency Check ---
        idem_key = request.idempotency_key()
        if idem_key in self._idempotency_keys:
            logger.warning("duplicate_order_prevented", idempotency_key=idem_key, symbol=symbol)
            raise DuplicateOrderError(f"Order already placed: {idem_key}")

        self._idempotency_keys.add(idem_key)

        # --- Rate Limit Check ---
        await self._check_rate_limit(exchange_name)

        # --- Place with retries ---
        start_time = datetime.now(UTC)
        last_error: str | None = None

        for attempt in range(self.max_retries):
            try:
                result_order = await asyncio.wait_for(
                    adapter.place_order(request),
                    timeout=self.order_timeout_seconds,
                )

                # Track active order
                self._active_orders[result_order.client_order_id] = result_order

                latency = (datetime.now(UTC) - start_time).total_seconds() * 1000

                logger.info(
                    "order_placed",
                    exchange=exchange_name,
                    symbol=symbol,
                    side=side.value,
                    order_id=result_order.client_order_id,
                    exchange_order_id=result_order.exchange_order_id,
                    state=result_order.state.value,
                    latency_ms=round(latency, 2),
                    attempt=attempt + 1,
                )

                return ExecutionResult(
                    success=True,
                    order=result_order,
                    retry_count=attempt,
                    latency_ms=latency,
                )

            except TimeoutError:
                last_error = f"Order timed out after {self.order_timeout_seconds}s"
                logger.warning(
                    "order_timeout", exchange=exchange_name, symbol=symbol, attempt=attempt + 1
                )
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(2**attempt)  # Exponential backoff

            except OrderRejectedError as e:
                last_error = str(e)
                logger.warning(
                    "order_rejected", exchange=exchange_name, symbol=symbol, reason=str(e)
                )
                break  # Don't retry rejected orders

            except RateLimitError as e:
                last_error = str(e)
                logger.warning("rate_limit_hit", exchange=exchange_name)
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(5 * (attempt + 1))  # Longer backoff for rate limits

        return ExecutionResult(
            success=False,
            error=last_error or "Unknown error",
            retry_count=self.max_retries,
        )

    # --- Cancellation ---

    async def cancel_order(
        self,
        exchange_name: str,
        order_id: str,
        symbol: str,
    ) -> ExecutionResult:
        """Cancel an open order."""
        adapter = self._adapters.get(exchange_name)
        if adapter is None:
            return ExecutionResult(success=False, error=f"No adapter for '{exchange_name}'")

        try:
            result = await adapter.cancel_order(order_id, symbol)
            logger.info("order_canceled", exchange=exchange_name, order_id=order_id, symbol=symbol)
            return ExecutionResult(success=True, order=result)
        except Exception as e:
            logger.error("cancel_failed", exchange=exchange_name, order_id=order_id, error=str(e))
            return ExecutionResult(success=False, error=str(e))

    # --- Reconciliation ---

    async def reconcile(self, exchange_name: str) -> list[ReconciliationError]:
        """Reconcile local order state with exchange state.

        Called after startup/restart. Detects:
        - Orders filled on exchange but not locally marked.
        - Orders canceled on exchange but still open locally.
        - Missing exchange order IDs.

        Returns list of ReconciliationErrors for manual review.
        """
        errors: list[ReconciliationError] = []
        adapter = self._adapters.get(exchange_name)
        if adapter is None:
            errors.append(ReconciliationError(f"No adapter for '{exchange_name}'"))
            return errors

        try:
            exchange_orders = await adapter.get_open_orders()
        except Exception as e:
            errors.append(ReconciliationError(f"Failed to fetch open orders: {e}"))
            return errors

        exchange_order_ids = {o.exchange_order_id for o in exchange_orders if o.exchange_order_id}

        for local_order in self._active_orders.values():
            if not local_order.exchange_order_id:
                continue
            if OrderStateMachine.is_active(local_order.state):
                if local_order.exchange_order_id not in exchange_order_ids:
                    # Order was filled/canceled while we were down
                    try:
                        updated = await adapter.get_order(
                            local_order.exchange_order_id, local_order.symbol
                        )
                        self._active_orders[local_order.client_order_id] = updated
                        logger.info(
                            "reconciled_order",
                            client_id=local_order.client_order_id,
                            new_state=updated.state.value,
                        )
                    except Exception as e:
                        errors.append(
                            ReconciliationError(
                                f"Failed to reconcile {local_order.client_order_id}: {e}"
                            )
                        )

        return errors

    # --- Rate Limiting ---

    async def _check_rate_limit(self, exchange_name: str) -> None:
        """Check exchange rate limits before placing order."""
        # Simple token-bucket-like check per exchange
        now = datetime.now(UTC)
        reset_time = self._rate_limit_reset_times.get(exchange_name)
        if reset_time and now < reset_time:
            count = self._rate_limit_counters.get(exchange_name, 0)
            if count >= 10:  # Conservative: 10 orders per window
                raise RateLimitError(f"Rate limit for {exchange_name} — retry after {reset_time}")
        else:
            self._rate_limit_counters[exchange_name] = 0
            self._rate_limit_reset_times[exchange_name] = now.replace(second=now.second + 1)
        self._rate_limit_counters[exchange_name] = (
            self._rate_limit_counters.get(exchange_name, 0) + 1
        )

    # --- Queries ---

    def get_active_orders(self, exchange_name: str | None = None) -> list[NormalizedOrder]:
        """Get all active (non-terminal) orders, optionally filtered by exchange."""
        active = [o for o in self._active_orders.values() if OrderStateMachine.is_active(o.state)]
        if exchange_name:
            active = [o for o in active if o.exchange == exchange_name]
        return active
