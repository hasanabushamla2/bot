"""Cross-exchange universe selection for global paper trading."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from src.adapters.crypto.ccxt_public import CCXTPublicAdapter, PublicTicker
from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class VenueSelection:
    venue: str
    symbol: str
    spread_bps: float
    quote_volume_24h: float


@dataclass
class GlobalUniverse:
    by_venue: dict[str, list[str]] = field(default_factory=dict)
    selections: list[VenueSelection] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def symbol_count(self) -> int:
        return len(self.selections)


class MultiVenueUniverseBuilder:
    """Select each base asset on its best currently accessible venue.

    Keeping one venue per base prevents a paper portfolio from unknowingly
    duplicating the same directional exposure on several exchanges.
    """

    def __init__(
        self,
        adapters: list[CCXTPublicAdapter],
        *,
        min_volume_usd: float = 250_000.0,
        max_spread_bps: float = 35.0,
        max_symbols_per_venue: int = 150,
        max_global_symbols: int = 500,
    ) -> None:
        self.adapters = adapters
        self.min_volume_usd = min_volume_usd
        self.max_spread_bps = max_spread_bps
        self.max_symbols_per_venue = max_symbols_per_venue
        self.max_global_symbols = max_global_symbols

    async def build(self) -> GlobalUniverse:
        result = GlobalUniverse()
        connected = await asyncio.gather(
            *(self._connect_and_fetch(adapter) for adapter in self.adapters),
            return_exceptions=True,
        )
        candidates: list[tuple[CCXTPublicAdapter, PublicTicker]] = []
        for adapter, response in zip(self.adapters, connected, strict=True):
            if isinstance(response, BaseException):
                result.errors[adapter.exchange_id] = str(response)
                continue
            tickers = response
            ranked_symbols = adapter.rank_liquid_markets(
                tickers,
                min_volume_usd=self.min_volume_usd,
                max_spread_bps=self.max_spread_bps,
                max_symbols=self.max_symbols_per_venue,
            )
            candidates.extend((adapter, tickers[symbol]) for symbol in ranked_symbols)

        # Prefer tighter spreads, then deeper 24h volume, for each base asset.
        best_by_symbol: dict[str, tuple[CCXTPublicAdapter, PublicTicker]] = {}
        for adapter, ticker in candidates:
            current = best_by_symbol.get(ticker.symbol)
            score = (ticker.spread_bps, -ticker.quote_volume_24h, adapter.exchange_id)
            if current is None:
                best_by_symbol[ticker.symbol] = (adapter, ticker)
                continue
            current_adapter, current_ticker = current
            current_score = (
                current_ticker.spread_bps,
                -current_ticker.quote_volume_24h,
                current_adapter.exchange_id,
            )
            if score < current_score:
                best_by_symbol[ticker.symbol] = (adapter, ticker)

        ordered = sorted(
            best_by_symbol.values(),
            key=lambda item: (item[1].spread_bps, -item[1].quote_volume_24h, item[1].symbol),
        )
        if self.max_global_symbols > 0:
            ordered = ordered[: self.max_global_symbols]
        for adapter, ticker in ordered:
            selection = VenueSelection(
                venue=adapter.exchange_id,
                symbol=ticker.symbol,
                spread_bps=ticker.spread_bps,
                quote_volume_24h=ticker.quote_volume_24h,
            )
            result.selections.append(selection)
            result.by_venue.setdefault(adapter.exchange_id, []).append(ticker.symbol)
        logger.info(
            "global_universe_built",
            symbols=result.symbol_count,
            venues={venue: len(symbols) for venue, symbols in result.by_venue.items()},
            errors=result.errors,
        )
        return result

    @staticmethod
    async def _connect_and_fetch(adapter: CCXTPublicAdapter) -> dict[str, PublicTicker]:
        await adapter.connect()
        return await adapter.get_all_tickers()

    async def close(self) -> None:
        await asyncio.gather(*(adapter.disconnect() for adapter in self.adapters), return_exceptions=True)
