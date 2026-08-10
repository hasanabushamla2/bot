"""Tests for symbol normalization and canonical event models."""

from __future__ import annotations

from datetime import datetime

from src.data.normalization import (
    BookLevel,
    CanonicalSymbol,
    MarketEventType,
    OrderBookDelta,
    OrderBookSnapshot,
    TickerEvent,
    TradeEvent,
)


class TestCanonicalSymbol:
    def test_basic(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDT")
        assert cs.base == "BTC"
        assert cs.quote == "USDT"
        assert cs.symbol == "BTC-USDT"

    def test_with_slash(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("kraken", "XBT/USD")
        assert cs.symbol == "XBT-USD"

    def test_with_dash(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("coinbase", "ETH-USD")
        assert cs.symbol == "ETH-USD"

    def test_with_underscore(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("bybit", "BTC_USDT")
        assert cs.symbol == "BTC-USDT"

    def test_usdc_quote(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDC")
        assert cs.base == "BTC"
        assert cs.quote == "USDC"

    def test_btc_quote(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("binance", "ETHBTC")
        assert cs.base == "ETH"
        assert cs.quote == "BTC"

    def test_lowercase(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("binance", "btcusdt")
        assert cs.symbol == "BTC-USDT"

    def test_unknown_quote_fallback(self) -> None:
        cs = CanonicalSymbol.from_exchange_symbol("some_exchange", "ABCDEFGH")
        assert cs.base
        assert cs.quote
        assert cs.symbol


class TestTickerEvent:
    def test_create(self) -> None:
        cs = CanonicalSymbol("binance", "BTC", "USDT")
        event = TickerEvent.create(
            exchange="binance",
            canonical=cs,
            bid=50000.0,
            ask=50001.0,
            last=50000.5,
            volume_24h=1000.0,
        )
        assert event.symbol == "BTC-USDT"
        assert event.event_type == MarketEventType.TICKER
        assert event.bid == 50000.0
        assert isinstance(event.local_receive_timestamp, datetime)


class TestTradeEvent:
    def test_create(self) -> None:
        cs = CanonicalSymbol("binance", "ETH", "USDT")
        event = TradeEvent.create(
            exchange="binance",
            canonical=cs,
            trade_id="12345",
            price=3000.0,
            quantity=1.5,
            is_buyer_maker=True,
        )
        assert event.symbol == "ETH-USDT"
        assert event.trade_id == "12345"
        assert event.is_buyer_maker is True


class TestOrderBookSnapshot:
    def test_create(self) -> None:
        cs = CanonicalSymbol("binance", "BTC", "USDT")
        bids = [BookLevel(50000.0, 1.0), BookLevel(49990.0, 2.0)]
        asks = [BookLevel(50010.0, 1.5)]
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=cs,
            bids=bids,
            asks=asks,
            last_update_id=1000,
        )
        assert snap.symbol == "BTC-USDT"
        assert len(snap.bids) == 2
        assert snap.last_update_id == 1000


class TestOrderBookDelta:
    def test_create(self) -> None:
        cs = CanonicalSymbol("binance", "BTC", "USDT")
        delta = OrderBookDelta.create(
            exchange="binance",
            canonical=cs,
            bids=[BookLevel(50001.0, 0.5)],
            asks=[],
            first_update_id=1001,
            final_update_id=1005,
        )
        assert delta.first_update_id == 1001
        assert delta.final_update_id == 1005
        assert len(delta.bids) == 1
