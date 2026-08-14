"""Offline tests for read-only multi-venue market discovery."""

from __future__ import annotations

from typing import Any

import pytest

from src.adapters.crypto.ccxt_public import CCXTPublicAdapter
from src.scanner.multi_venue import MultiVenueUniverseBuilder


class FakeExchange:
    def __init__(self, markets: dict[str, dict[str, Any]], tickers: dict[str, dict[str, Any]]) -> None:
        self.markets = markets
        self._tickers = tickers

    async def load_markets(self) -> dict[str, dict[str, Any]]:
        return self.markets

    async def fetch_tickers(self) -> dict[str, dict[str, Any]]:
        return dict(self._tickers)

    async def fetch_time(self) -> int:
        return 1

    async def fetch_order_book(self, symbol: str, limit: int) -> dict[str, Any]:
        return {"bids": [[10.0, 100.0]], "asks": [[10.01, 100.0]]}


@pytest.mark.asyncio
async def test_ccxt_public_filters_non_spot_stables_and_leveraged_tokens() -> None:
    markets = {
        "ACE/USDT": {"base": "ACE", "quote": "USDT", "active": True, "spot": True},
        "USDC/USDT": {"base": "USDC", "quote": "USDT", "active": True, "spot": True},
        "BTC3L/USDT": {"base": "BTC3L", "quote": "USDT", "active": True, "spot": True},
        "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "active": True, "spot": False},
    }
    tickers = {
        "ACE/USDT": {
            "bid": 1.0,
            "ask": 1.001,
            "last": 1.0,
            "quoteVolume": 2_000_000.0,
        }
    }
    adapter = CCXTPublicAdapter("binance", FakeExchange(markets, tickers))
    await adapter.connect()

    assert [market.symbol for market in adapter.markets()] == ["ACE-USDT"]
    normalized = await adapter.get_all_tickers()
    assert adapter.rank_liquid_markets(normalized) == ["ACE-USDT"]
    book = await adapter.get_order_book("ACE-USDT")
    assert book and book["bids"][0] == (10.0, 100.0)


@pytest.mark.asyncio
async def test_global_builder_assigns_asset_to_tighter_venue() -> None:
    market = {"ACE/USDT": {"base": "ACE", "quote": "USDT", "active": True, "spot": True}}
    wide = FakeExchange(
        market,
        {"ACE/USDT": {"bid": 1.0, "ask": 1.02, "last": 1.01, "quoteVolume": 5_000_000.0}},
    )
    tight = FakeExchange(
        market,
        {"ACE/USDT": {"bid": 1.0, "ask": 1.001, "last": 1.0, "quoteVolume": 1_000_000.0}},
    )
    kucoin = CCXTPublicAdapter("kucoin", wide)
    binance = CCXTPublicAdapter("binance", tight)
    builder = MultiVenueUniverseBuilder(
        [kucoin, binance], max_spread_bps=250.0, max_global_symbols=10
    )

    universe = await builder.build()

    assert universe.by_venue == {"binance": ["ACE-USDT"]}
    assert universe.selections[0].venue == "binance"
