"""Tests for the local Order Book Engine."""

from __future__ import annotations

import pytest

from src.data.normalization import BookLevel, OrderBookDelta, OrderBookSnapshot
from src.data.order_book import OrderBookEngine, _SideBook


class TestSideBook:
    def test_empty(self) -> None:
        book = _SideBook(is_bids=True)
        assert book.best() == (0.0, 0.0)
        assert book.count == 0

    def test_bids_descending(self) -> None:
        book = _SideBook(is_bids=True)
        book.apply_snapshot(
            [
                BookLevel(100.0, 1.0),
                BookLevel(101.0, 2.0),
                BookLevel(99.0, 3.0),
            ]
        )
        assert book.best() == (101.0, 2.0)  # Highest bid first
        assert book.count == 3

    def test_asks_ascending(self) -> None:
        book = _SideBook(is_bids=False)
        book.apply_snapshot(
            [
                BookLevel(101.0, 1.0),
                BookLevel(100.0, 2.0),
                BookLevel(102.0, 3.0),
            ]
        )
        assert book.best() == (100.0, 2.0)  # Lowest ask first
        assert book.count == 3

    def test_upsert_new_price(self) -> None:
        book = _SideBook(is_bids=True)
        book.apply_snapshot([BookLevel(100.0, 1.0)])
        book.apply_delta((BookLevel(101.0, 2.0),))
        assert book.count == 2
        assert book.best() == (101.0, 2.0)

    def test_upsert_update_price(self) -> None:
        book = _SideBook(is_bids=True)
        book.apply_snapshot([BookLevel(100.0, 1.0)])
        book.apply_delta((BookLevel(100.0, 5.0),))
        assert book.count == 1
        assert book.best() == (100.0, 5.0)

    def test_delete_zero_quantity(self) -> None:
        book = _SideBook(is_bids=True)
        book.apply_snapshot([BookLevel(100.0, 1.0), BookLevel(99.0, 2.0)])
        book.apply_delta((BookLevel(100.0, 0.0),))  # Delete
        assert book.count == 1
        assert book.best() == (99.0, 2.0)

    def test_depth_up_to_price(self) -> None:
        book = _SideBook(is_bids=True)
        book.apply_snapshot(
            [
                BookLevel(100.0, 1.0),
                BookLevel(99.5, 2.0),
                BookLevel(99.0, 3.0),
            ]
        )
        assert book.depth_up_to_price(99.5) == 3.0  # 1.0 + 2.0
        assert book.depth_up_to_price(99.0) == 6.0

    def test_vwap_for_size(self) -> None:
        book = _SideBook(is_bids=False)  # Asks
        book.apply_snapshot(
            [
                BookLevel(100.0, 1.0),
                BookLevel(101.0, 2.0),
            ]
        )
        vwap = book.vwap_for_size(2.0)
        # (1.0*100 + 1.0*101) / 2.0 = 100.5
        assert vwap == pytest.approx(100.5)

    def test_vwap_not_enough_depth(self) -> None:
        book = _SideBook(is_bids=False)
        book.apply_snapshot([BookLevel(100.0, 2.0)])
        assert book.vwap_for_size(10.0) is None


class TestOrderBookEngine:
    def test_get_or_create(self) -> None:
        engine = OrderBookEngine()
        book = engine.get_or_create("binance", "BTC-USDT")
        assert book.symbol == "BTC-USDT"
        assert not book.initialized

    def test_apply_snapshot_initializes(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0), BookLevel(49990.0, 2.0)],
            asks=[BookLevel(50010.0, 1.5), BookLevel(50020.0, 3.0)],
            last_update_id=100,
        )
        engine.apply_snapshot(snap)
        book = engine.get_book("binance", "BTC-USDT")
        assert book is not None
        assert book.initialized
        assert book.best_bid == 50000.0
        assert book.best_ask == 50010.0
        assert book.last_update_id == 100

    def test_apply_delta_updates(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0)],
            asks=[BookLevel(50010.0, 1.0)],
            last_update_id=100,
        )
        engine.apply_snapshot(snap)

        delta = OrderBookDelta.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(49995.0, 2.0)],
            asks=[],
            first_update_id=101,
            final_update_id=105,
        )
        applied = engine.apply_delta(delta)
        assert applied
        book = engine.get_book("binance", "BTC-USDT")
        assert book is not None
        assert book.bids.count == 2

    def test_delta_before_snapshot_buffered(self) -> None:
        engine = OrderBookEngine()
        delta = OrderBookDelta.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50001.0, 1.0)],
            asks=[],
            first_update_id=10,
            final_update_id=15,
        )
        applied = engine.apply_delta(delta)
        assert not applied  # Buffered, not yet applied
        book = engine.get_book("binance", "BTC-USDT")
        assert book is not None
        assert not book.initialized

    def test_stale_delta_dropped(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0)],
            asks=[BookLevel(50010.0, 1.0)],
            last_update_id=100,
        )
        engine.apply_snapshot(snap)

        delta = OrderBookDelta.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(49990.0, 1.0)],
            asks=[],
            first_update_id=50,  # Before last_update_id
            final_update_id=55,
        )
        applied = engine.apply_delta(delta)
        assert not applied  # Stale, dropped

    def test_gap_detection_flags_resync(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0)],
            asks=[BookLevel(50010.0, 1.0)],
            last_update_id=100,
        )
        engine.apply_snapshot(snap)

        delta = OrderBookDelta.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(49990.0, 1.0)],
            asks=[],
            first_update_id=200,  # Gap: 101 → 200
            final_update_id=210,
        )
        applied = engine.apply_delta(delta)
        assert not applied  # Gap
        assert engine.needs_resync("binance", "BTC-USDT")

    def test_mid_price(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0)],
            asks=[BookLevel(50020.0, 1.0)],
            last_update_id=1,
        )
        engine.apply_snapshot(snap)
        book = engine.get_book("binance", "BTC-USDT")
        assert book is not None
        assert book.mid_price == 50010.0

    def test_spread_bps(self) -> None:
        engine = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            exchange="binance",
            canonical=_canonical("BTC-USDT"),
            bids=[BookLevel(50000.0, 1.0)],
            asks=[BookLevel(50010.0, 1.0)],
            last_update_id=1,
        )
        engine.apply_snapshot(snap)
        book = engine.get_book("binance", "BTC-USDT")
        assert book is not None
        # spread = 10, mid = 50005, spread_bps ≈ 2.0
        assert book.spread_bps == pytest.approx(2.0, rel=0.1)


def _canonical(symbol: str):
    from src.data.normalization import CanonicalSymbol

    return CanonicalSymbol.from_exchange_symbol("binance", symbol)
