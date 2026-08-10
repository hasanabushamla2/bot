"""Tests for the Universe Manager."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.portfolio.liquidity import LiquidityAnalyzer
from src.portfolio.universe import UniverseConfig, UniverseManager, UniverseStatus


class TestUniverseManager:
    def test_register_new_asset(self) -> None:
        mgr = UniverseManager()
        asset = mgr.register("BTC-USD", "coinbase")
        assert asset.symbol == "BTC-USD"
        assert asset.status == UniverseStatus.WATCH

    def test_register_duplicate_returns_existing(self) -> None:
        mgr = UniverseManager()
        a1 = mgr.register("BTC-USD", "coinbase")
        a2 = mgr.register("BTC-USD", "coinbase")
        assert a1 is a2

    def test_watch_period_active(self) -> None:
        """Asset should stay WATCH until period passes with good data."""
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=3600))
        asset = mgr.register("BTC-USD", "coinbase")
        # Provide fresh data so it doesn't fail on stale-data check
        asset.last_data_at = datetime.now(UTC)
        asset.data_healthy = True
        # Freshly added: still WATCH
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.WATCH

    def test_active_after_watch_with_good_data(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0))  # No watch
        asset = mgr.register("BTC-USD", "coinbase")
        # Give it good metrics
        asset.spread_pct = 0.5
        asset.volume_24h = 50_000_000.0
        asset.liquidity_score = 0.8
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.ACTIVE

    def test_suspended_for_wide_spread(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0, max_spread_pct=5.0))
        asset = mgr.register("BTC-USD", "coinbase")
        asset.spread_pct = 10.0  # Too wide
        asset.volume_24h = 50_000_000.0
        asset.liquidity_score = 0.5
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.SUSPENDED

    def test_suspended_for_low_volume(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0, min_volume_24h=1_000_000.0))
        asset = mgr.register("SHITCOIN-USD", "coinbase")
        asset.spread_pct = 1.0
        asset.volume_24h = 100.0  # Way too low
        asset.liquidity_score = 0.1
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.SUSPENDED

    def test_suspended_for_stale_data(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0, max_stale_seconds=60))
        asset = mgr.register("BTC-USD", "coinbase")
        asset.spread_pct = 1.0
        asset.volume_24h = 50_000_000.0
        asset.liquidity_score = 0.8
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC) - timedelta(seconds=120)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.SUSPENDED

    def test_degraded_for_high_volatility(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0, max_volatility_pct=100.0))
        asset = mgr.register("WILD-USD", "coinbase")
        asset.spread_pct = 1.0
        asset.volume_24h = 50_000_000.0
        asset.liquidity_score = 0.8
        asset.volatility_pct = 150.0  # Very volatile
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.DEGRADED

    def test_manual_suspend_unsuspend(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0))
        asset = mgr.register("BTC-USD", "coinbase")
        asset.spread_pct = 0.5
        asset.volume_24h = 50_000_000.0
        asset.liquidity_score = 0.8
        asset.data_healthy = True
        asset.last_data_at = datetime.now(UTC)
        mgr.evaluate_all()
        assert asset.status == UniverseStatus.ACTIVE

        mgr.suspend("BTC-USD", "coinbase", "manual test")
        assert asset.status == UniverseStatus.SUSPENDED

        mgr.unsuspend("BTC-USD", "coinbase")
        assert asset.status == UniverseStatus.WATCH

    def test_get_active_returns_only_active(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0))
        # Good asset
        a1 = mgr.register("BTC-USD", "coinbase")
        a1.spread_pct = 0.5
        a1.volume_24h = 50_000_000.0
        a1.liquidity_score = 0.8
        a1.data_healthy = True
        a1.last_data_at = datetime.now(UTC)

        # Bad asset
        a2 = mgr.register("ILLIQUID-USD", "coinbase")
        a2.spread_pct = 50.0
        a2.volume_24h = 100.0
        a2.liquidity_score = 0.05
        a2.data_healthy = True
        a2.last_data_at = datetime.now(UTC)

        mgr.evaluate_all()
        active = mgr.get_active()
        assert len(active) == 1
        assert active[0].symbol == "BTC-USD"

    def test_summary_returns_counts(self) -> None:
        mgr = UniverseManager(UniverseConfig(watch_period_seconds=0))
        a1 = mgr.register("BTC-USD", "coinbase")
        a1.spread_pct = 0.5
        a1.volume_24h = 50_000_000.0
        a1.liquidity_score = 0.8
        a1.data_healthy = True
        a1.last_data_at = datetime.now(UTC)

        a2 = mgr.register("ETH-USD", "coinbase")
        a2.spread_pct = 1.0
        a2.volume_24h = 30_000_000.0
        a2.liquidity_score = 0.75
        a2.data_healthy = True
        a2.last_data_at = datetime.now(UTC)

        mgr.evaluate_all()
        summary = mgr.summary()
        assert summary["total"] == 2
        assert summary["active"] == 2

    def test_update_liquidity_creates_if_missing(self) -> None:
        mgr = UniverseManager()
        liq_analyzer = LiquidityAnalyzer()
        from src.adapters.base import NormalizedOrderBook, NormalizedOrderBookLevel

        book = NormalizedOrderBook(
            exchange="coinbase",
            symbol="BTC-USD",
            bids=[NormalizedOrderBookLevel(price=49990.0, quantity=5.0)],
            asks=[NormalizedOrderBookLevel(price=50010.0, quantity=5.0)],
            timestamp=datetime.now(UTC),
        )
        metrics = liq_analyzer.analyze(book, volume_24h=50_000_000.0)
        mgr.update_liquidity("BTC-USD", "coinbase", metrics)
        asset = mgr.get("BTC-USD", "coinbase")
        assert asset is not None
        assert asset.liquidity_score > 0
