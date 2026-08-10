"""Normalized market event models and symbol normalization.

All external exchange data flows through these strict models
before entering the engine. Every event is timestamped with both
the exchange time and the local receipt time for latency tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# Canonical symbol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CanonicalSymbol:
    """Canonical internal symbol representation.

    All exchanges normalize into this format. The canonical form
    uses uppercase with '-' separator: BASE-QUOTE.
    """

    exchange: str
    base: str
    quote: str

    @property
    def symbol(self) -> str:
        return f"{self.base}-{self.quote}"

    @classmethod
    def from_exchange_symbol(cls, exchange: str, raw: str) -> CanonicalSymbol:
        """Parse an exchange-specific symbol into canonical form."""
        cleaned = raw.upper().replace("/", "").replace("_", "").replace("-", "")

        # Common quote assets — sorted longest-first
        quote_candidates = sorted(
            [
                "USDT",
                "USDC",
                "BUSD",
                "FDUSD",
                "TUSD",
                "DAI",
                "BTC",
                "ETH",
                "BNB",
                "USD",
                "EUR",
                "GBP",
                "JPY",
            ],
            key=len,
            reverse=True,
        )

        # Find ALL matches and pick the one with the longest base
        # (most conservative split). This handles conflicts like
        # XBTUSD → XBT+USD (base=3) instead of XB+TUSD (base=2).
        best: tuple[str, str] | None = None
        for quote in quote_candidates:
            if cleaned.endswith(quote):
                base = cleaned[: -len(quote)]
                if base and (best is None or len(base) > len(best[0])):
                    best = (base, quote)

        if best is not None:
            return cls(exchange=exchange, base=best[0], quote=best[1])

        # Fallback: heuristic split
        mid = len(cleaned) // 2
        return cls(exchange=exchange, base=cleaned[:mid], quote=cleaned[mid:])


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class MarketEventType(str, Enum):
    TICKER = "ticker"
    TRADE = "trade"
    ORDER_BOOK_SNAPSHOT = "order_book_snapshot"
    ORDER_BOOK_DELTA = "order_book_delta"
    CANDLE = "candle"
    MARKET_STATUS = "market_status"
    INSTRUMENT_METADATA = "instrument_metadata"


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    HALT = "halt"
    AUCTION = "auction"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Normalized events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    """Base for all normalized market events."""

    exchange: str
    symbol: str  # Canonical "BASE-QUOTE"
    event_type: MarketEventType
    exchange_timestamp: datetime  # Timestamp from exchange
    local_receive_timestamp: datetime  # When we received it
    sequence_id: int | None = None
    raw_source: str | None = None  # For debugging / audit


@dataclass(frozen=True, slots=True)
class TickerEvent(NormalizedEvent):
    """Normalized 24hr ticker."""

    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h: float = 0.0
    quote_volume_24h: float = 0.0
    open_24h: float = 0.0
    price_change_pct: float = 0.0

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        bid: float,
        ask: float,
        last: float,
        high_24h: float = 0.0,
        low_24h: float = 0.0,
        volume_24h: float = 0.0,
        quote_volume_24h: float = 0.0,
        open_24h: float = 0.0,
        price_change_pct: float = 0.0,
        exchange_ts: datetime | None = None,
        sequence_id: int | None = None,
    ) -> TickerEvent:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.TICKER,
            exchange_timestamp=exchange_ts or now,
            local_receive_timestamp=now,
            sequence_id=sequence_id,
            bid=bid,
            ask=ask,
            last=last,
            high_24h=high_24h,
            low_24h=low_24h,
            volume_24h=volume_24h,
            quote_volume_24h=quote_volume_24h,
            open_24h=open_24h,
            price_change_pct=price_change_pct,
        )


@dataclass(frozen=True, slots=True)
class TradeEvent(NormalizedEvent):
    """Normalized public trade."""

    trade_id: str = ""
    price: float = 0.0
    quantity: float = 0.0
    is_buyer_maker: bool = False
    trade_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        trade_id: str,
        price: float,
        quantity: float,
        is_buyer_maker: bool = False,
        trade_ts: datetime | None = None,
        exchange_ts: datetime | None = None,
        sequence_id: int | None = None,
    ) -> TradeEvent:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.TRADE,
            exchange_timestamp=exchange_ts or trade_ts or now,
            local_receive_timestamp=now,
            sequence_id=sequence_id,
            trade_id=trade_id,
            price=price,
            quantity=quantity,
            is_buyer_maker=is_buyer_maker,
            trade_timestamp=trade_ts or now,
        )


@dataclass(frozen=True, slots=True)
class BookLevel:
    """Single order-book price level."""

    price: float
    quantity: float


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot(NormalizedEvent):
    """Full order-book snapshot."""

    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    last_update_id: int = 0

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        bids: list[BookLevel],
        asks: list[BookLevel],
        last_update_id: int = 0,
        exchange_ts: datetime | None = None,
        sequence_id: int | None = None,
    ) -> OrderBookSnapshot:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.ORDER_BOOK_SNAPSHOT,
            exchange_timestamp=exchange_ts or now,
            local_receive_timestamp=now,
            sequence_id=sequence_id,
            bids=tuple(bids),
            asks=tuple(asks),
            last_update_id=last_update_id,
        )


@dataclass(frozen=True, slots=True)
class OrderBookDelta(NormalizedEvent):
    """Incremental order-book update (Binance-style depth event)."""

    bids: tuple[BookLevel, ...] = ()  # (price, quantity) — qty 0 = delete
    asks: tuple[BookLevel, ...] = ()
    first_update_id: int = 0
    final_update_id: int = 0

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        bids: list[BookLevel],
        asks: list[BookLevel],
        first_update_id: int = 0,
        final_update_id: int = 0,
        exchange_ts: datetime | None = None,
        sequence_id: int | None = None,
    ) -> OrderBookDelta:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.ORDER_BOOK_DELTA,
            exchange_timestamp=exchange_ts or now,
            local_receive_timestamp=now,
            sequence_id=sequence_id,
            bids=tuple(bids),
            asks=tuple(asks),
            first_update_id=first_update_id,
            final_update_id=final_update_id,
        )


@dataclass(frozen=True, slots=True)
class CandleEvent(NormalizedEvent):
    """Normalized OHLCV candle."""

    interval: str = "1m"  # 1m, 5m, 15m, 1h, 4h, 1d, etc.
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    quote_volume: float = 0.0
    trades_count: int = 0
    is_closed: bool = True
    open_timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        interval: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        quote_volume: float = 0.0,
        trades_count: int = 0,
        is_closed: bool = True,
        open_ts: datetime | None = None,
        exchange_ts: datetime | None = None,
        sequence_id: int | None = None,
    ) -> CandleEvent:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.CANDLE,
            exchange_timestamp=exchange_ts or now,
            local_receive_timestamp=now,
            sequence_id=sequence_id,
            interval=interval,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            quote_volume=quote_volume,
            trades_count=trades_count,
            is_closed=is_closed,
            open_timestamp=open_ts or now,
        )


@dataclass(frozen=True, slots=True)
class MarketStatusEvent(NormalizedEvent):
    """Exchange or symbol trading status."""

    status: MarketStatus = MarketStatus.UNKNOWN
    message: str = ""

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        status: MarketStatus,
        message: str = "",
        exchange_ts: datetime | None = None,
    ) -> MarketStatusEvent:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.MARKET_STATUS,
            exchange_timestamp=exchange_ts or now,
            local_receive_timestamp=now,
            status=status,
            message=message,
        )


@dataclass(frozen=True, slots=True)
class InstrumentMetadata(NormalizedEvent):
    """Exchange instrument metadata and constraints."""

    base_asset: str = ""
    quote_asset: str = ""
    status: str = "TRADING"
    min_price: float = 0.0
    max_price: float = 0.0
    tick_size: float = 0.0
    min_qty: float = 0.0
    max_qty: float = 0.0
    step_size: float = 0.0
    min_notional: float = 0.0
    price_precision: int = 0
    quantity_precision: int = 0
    is_spot: bool = True
    is_margin: bool = False

    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        exchange: str,
        canonical: CanonicalSymbol,
        **kwargs: Any,
    ) -> InstrumentMetadata:
        now = datetime.now(UTC)
        return cls(
            exchange=exchange,
            symbol=canonical.symbol,
            event_type=MarketEventType.INSTRUMENT_METADATA,
            exchange_timestamp=now,
            local_receive_timestamp=now,
            base_asset=canonical.base,
            quote_asset=canonical.quote,
            **kwargs,
        )
