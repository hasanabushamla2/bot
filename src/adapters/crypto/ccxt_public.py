"""Read-only CCXT market-data adapter for multi-venue paper trading.

The adapter intentionally exposes no order methods.  It discovers active spot
markets, normalizes tickers/books, and ranks a feed-budget-compatible universe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)

SUPPORTED_CCXT_EXCHANGES = ("binance", "kucoin", "okx", "bybit", "gateio", "mexc")
_STABLE_BASES = {
    "USDT", "USDC", "USDE", "DAI", "FDUSD", "TUSD", "USDP", "PYUSD", "EURC",
}
_LEVERAGED_SUFFIXES = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")


@dataclass(frozen=True)
class PublicMarket:
    venue: str
    symbol: str
    unified_symbol: str
    base: str
    quote: str


@dataclass(frozen=True)
class PublicTicker:
    venue: str
    symbol: str
    bid: float
    ask: float
    last: float
    quote_volume_24h: float
    timestamp: datetime

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 10_000.0 if mid > 0 and self.ask > self.bid else float("inf")


class CCXTPublicAdapter:
    """Common read-only interface over major CCXT spot exchanges."""

    def __init__(self, exchange_id: str, exchange_client: Any | None = None) -> None:
        if exchange_id not in SUPPORTED_CCXT_EXCHANGES:
            raise ValueError(f"Unsupported exchange: {exchange_id}")
        self.exchange_id = exchange_id
        self._exchange = exchange_client
        self._owns_exchange = exchange_client is None
        self._markets: dict[str, PublicMarket] = {}

    async def connect(self) -> None:
        if self._exchange is None:
            import ccxt.async_support as ccxt

            exchange_class = getattr(ccxt, self.exchange_id)
            self._exchange = exchange_class({
                "enableRateLimit": True,
                "timeout": 20_000,
                "options": {"defaultType": "spot"},
            })
        await self._exchange.load_markets()
        self._index_markets()
        logger.info("ccxt_public_connected", exchange=self.exchange_id, markets=len(self._markets))

    async def disconnect(self) -> None:
        if self._exchange is not None and self._owns_exchange:
            await self._exchange.close()
        self._exchange = None

    async def health_check(self) -> bool:
        try:
            if self._exchange is None:
                return False
            await self._exchange.fetch_time()
            return True
        except Exception:
            return False

    def markets(self) -> list[PublicMarket]:
        return list(self._markets.values())

    async def get_all_tickers(self) -> dict[str, PublicTicker]:
        if self._exchange is None:
            return {}
        # Fetch the venue-wide ticker snapshot in one public call. Passing
        # hundreds of symbols can create oversized URLs on some exchanges.
        raw_tickers = await self._exchange.fetch_tickers()
        now = datetime.now(UTC)
        result: dict[str, PublicTicker] = {}
        for unified, raw in raw_tickers.items():
            canonical = self._canonical_for_unified(str(unified))
            if canonical is None:
                continue
            try:
                bid = float(raw.get("bid") or 0.0)
                ask = float(raw.get("ask") or 0.0)
                last = float(raw.get("last") or raw.get("close") or 0.0)
                quote_volume = float(raw.get("quoteVolume") or 0.0)
                if quote_volume <= 0.0:
                    base_volume = float(raw.get("baseVolume") or 0.0)
                    quote_volume = base_volume * last
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= bid or last <= 0:
                continue
            result[canonical] = PublicTicker(
                venue=self.exchange_id,
                symbol=canonical,
                bid=bid,
                ask=ask,
                last=last,
                quote_volume_24h=quote_volume,
                timestamp=now,
            )
        return result

    def rank_liquid_markets(
        self,
        tickers: dict[str, PublicTicker],
        *,
        min_volume_usd: float = 250_000.0,
        max_spread_bps: float = 35.0,
        max_symbols: int = 150,
    ) -> list[str]:
        ranked = [
            ticker
            for ticker in tickers.values()
            if ticker.symbol in self._markets
            and ticker.quote_volume_24h >= min_volume_usd
            and ticker.spread_bps <= max_spread_bps
        ]
        ranked.sort(key=lambda ticker: (ticker.spread_bps, -ticker.quote_volume_24h, ticker.symbol))
        if max_symbols > 0:
            ranked = ranked[:max_symbols]
        return [ticker.symbol for ticker in ranked]

    async def get_order_book(
        self, symbol: str, depth: int = 20
    ) -> dict[str, list[tuple[float, float]]] | None:
        if self._exchange is None:
            return None
        market = self._markets.get(symbol)
        if market is None:
            return None
        raw = await self._exchange.fetch_order_book(market.unified_symbol, limit=depth)
        bids = [(float(price), float(qty)) for price, qty in raw.get("bids", [])[:depth]]
        asks = [(float(price), float(qty)) for price, qty in raw.get("asks", [])[:depth]]
        return {"bids": bids, "asks": asks}

    def _index_markets(self) -> None:
        self._markets.clear()
        raw_markets = getattr(self._exchange, "markets", {}) or {}
        for unified, raw in raw_markets.items():
            base = str(raw.get("base") or "").upper()
            quote = str(raw.get("quote") or "").upper()
            if quote != "USDT" or not base or base in _STABLE_BASES:
                continue
            if raw.get("active") is False or raw.get("spot") is False:
                continue
            if any(base.endswith(suffix) for suffix in _LEVERAGED_SUFFIXES):
                continue
            canonical = f"{base}-USDT"
            self._markets[canonical] = PublicMarket(
                venue=self.exchange_id,
                symbol=canonical,
                unified_symbol=str(unified),
                base=base,
                quote=quote,
            )

    def _canonical_for_unified(self, unified: str) -> str | None:
        for canonical, market in self._markets.items():
            if market.unified_symbol == unified:
                return canonical
        return None
