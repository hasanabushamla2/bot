"""Base exchange adapter interface.

Every exchange adapter MUST implement this abstract base class.
The core engine depends ONLY on this interface, never on a specific
exchange implementation.

Adapters are responsible for:
- Normalizing all exchange-specific data to internal formats.
- Handling connection lifecycle (connect, disconnect, reconnect).
- Translating internal order requests to exchange API calls.
- Reconciling exchange state after restarts.
"""

from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

# --- Normalized Types ---


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderState(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(str, Enum):
    GTC = "GTC"  # Good Till Canceled
    IOC = "IOC"  # Immediate Or Cancel
    FOK = "FOK"  # Fill Or Kill


@dataclass(frozen=True, slots=True)
class NormalizedInstrument:
    """Exchange-independent instrument representation."""

    exchange: str
    symbol: str
    base_asset: str
    quote_asset: str
    min_order_size: float
    price_precision: int
    quantity_precision: int
    maker_fee: float
    taker_fee: float
    is_active: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedTicker:
    """Exchange-independent ticker."""

    exchange: str
    symbol: str
    bid: float
    ask: float
    last: float
    volume_24h: float
    high_24h: float
    low_24h: float
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedOrderBookLevel:
    """Single level in an order book."""

    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class NormalizedOrderBook:
    """Exchange-independent order book snapshot."""

    exchange: str
    symbol: str
    bids: list[NormalizedOrderBookLevel]
    asks: list[NormalizedOrderBookLevel]
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    """Exchange-independent public trade."""

    exchange: str
    symbol: str
    trade_id: str
    price: float
    quantity: float
    side: OrderSide
    timestamp: datetime


@dataclass
class NormalizedOrderRequest:
    """Request to place an order — exchange-independent."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None  # Required for LIMIT
    time_in_force: TimeInForce = TimeInForce.GTC
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def idempotency_key(self) -> str:
        """Deterministic idempotency key to prevent duplicate orders."""
        raw = (
            f"{self.symbol}:{self.side.value}:{self.order_type.value}:"
            f"{self.quantity}:{self.price or 0}:{self.time_in_force.value}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class NormalizedOrder:
    """Exchange-independent order status."""

    exchange: str
    symbol: str
    client_order_id: str
    exchange_order_id: str | None
    side: OrderSide
    order_type: OrderType
    state: OrderState
    quantity: float
    filled_quantity: float
    filled_avg_price: float | None
    price: float | None
    total_fees: float
    fee_currency: str | None
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedBalance:
    """Exchange-independent balance snapshot."""

    exchange: str
    asset: str
    free: float
    locked: float
    total: float
    timestamp: datetime


# --- Adapter Interface ---


class ExchangeAdapter(ABC):
    """Abstract base for all exchange adapters.

    Each concrete adapter wraps an exchange's official API or a
    well-maintained third-party library. The engine only calls
    these methods — it never makes exchange-specific calls directly.
    """

    def __init__(self, exchange_name: str, api_key: str, api_secret: str, **kwargs: Any) -> None:
        self.exchange_name = exchange_name
        self.api_key = api_key
        self.api_secret = api_secret
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # --- Connection Lifecycle ---

    @abstractmethod
    async def connect(self) -> None:
        """Establish connections (WebSocket + REST readiness check)."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close all connections."""
        ...

    @abstractmethod
    async def reconnect(self) -> None:
        """Reconnect after connection loss. Resubscribe to all streams."""
        ...

    # --- Market Data ---

    @abstractmethod
    async def get_instruments(self) -> list[NormalizedInstrument]:
        """Fetch all available instruments from the exchange."""
        ...

    @abstractmethod
    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        """Get current ticker for a symbol (REST fallback)."""
        ...

    @abstractmethod
    async def get_order_book(self, symbol: str, depth: int = 20) -> NormalizedOrderBook:
        """Get current order book snapshot (REST fallback)."""
        ...

    @abstractmethod
    async def subscribe_ticker(self, symbol: str) -> AsyncIterator[NormalizedTicker]:
        """Subscribe to real-time ticker updates via WebSocket."""
        ...

    @abstractmethod
    async def subscribe_order_book(self, symbol: str) -> AsyncIterator[NormalizedOrderBook]:
        """Subscribe to real-time order book updates via WebSocket."""
        ...

    @abstractmethod
    async def subscribe_trades(self, symbol: str) -> AsyncIterator[NormalizedTrade]:
        """Subscribe to real-time trade stream via WebSocket."""
        ...

    # --- Order Execution ---

    @abstractmethod
    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrder:
        """Place a new order. Returns normalized order with exchange state."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> NormalizedOrder:
        """Cancel an existing order by exchange order ID."""
        ...

    @abstractmethod
    async def get_order(self, order_id: str, symbol: str) -> NormalizedOrder:
        """Query the current state of an order by exchange order ID."""
        ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[NormalizedOrder]:
        """Get all currently open orders."""
        ...

    @abstractmethod
    async def get_order_history(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[NormalizedOrder]:
        """Get historical orders for reconciliation."""
        ...

    # --- Account ---

    @abstractmethod
    async def get_balances(self) -> list[NormalizedBalance]:
        """Get current account balances."""
        ...

    # --- Health ---

    @abstractmethod
    async def health_check(self) -> bool:
        """Check exchange connectivity and API key validity."""
        ...

    @abstractmethod
    async def get_server_time(self) -> datetime:
        """Get exchange server time for clock synchronization."""
        ...
