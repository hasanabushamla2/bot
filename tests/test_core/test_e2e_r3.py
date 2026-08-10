"""R3: End-to-end integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import BookLevel, CanonicalSymbol, OrderBookSnapshot, TickerEvent
from src.data.order_book import OrderBookEngine
from src.features.engine import FeatureEngine
from src.micro_live.account import MicroLiveAccount
from src.micro_live.config import MicroLivePolicy, MicroLiveSettings
from src.micro_live.orchestrator import MicroLiveOrchestrator
from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.position_monitor import PositionMonitor
from src.risk.engine import RiskDecision, RiskEngine
from src.scanner.global_scanner import AssetClass, AssetSnapshot, GlobalScanner, ScannerConfig
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.trailing_stop import TrailConfig


class TestE2EPipeline:
    def test_full_pipeline(self) -> None:
        canonical = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDT").symbol
        engine = FeatureEngine()
        for i in range(30):
            engine.update_price(canonical, 50000.0 + i * 100)
        feat = engine.get(canonical)
        feat.trend_strength = 0.8
        feat.momentum_1m = 3.0
        feat.momentum_5m = 6.0
        feat.acceleration = 1.0
        feat.volatility_5m_pct = 0.5
        strat = MomentumStrategy()
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        opp = OpportunityEngine().evaluate(sig)
        assert opp.status.value == "ranked"
        a = RiskEngine().assess(opp)
        assert a.decision == RiskDecision.APPROVED
        acct = PaperAccount(10000)
        pos = acct.open_position(
            canonical,
            "long",
            feat.last_price,
            0.1,
            fees=5.0,
            stop_loss_price=feat.last_price * 0.997,
        )
        assert pos is not None

    def test_hard_stop_exit(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5.0, stop_loss_price=49850)
        pos = acct.state.open_positions["BTC-USDT"]
        pos.current_price = 49840
        monitor = PositionMonitor(acct)
        monitor.register_position(pos)
        exits = monitor.check_all()
        assert len(exits) > 0
        assert exits[0]["reason"] == "hard_stop"
        trade = acct.close_position("BTC-USDT", 49840, fees=4.98, exit_reason="hard_stop")
        assert trade is not None
        assert trade.net_pnl < 0

    def test_trail_activation_exit(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5.0, stop_loss_price=49850)
        pos = acct.state.open_positions["BTC-USDT"]
        monitor = PositionMonitor(
            acct, TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        monitor.register_position(pos)
        pos.current_price = 50200
        monitor.check_all()
        pos.current_price = 50080
        exits = monitor.check_all()
        assert len(exits) > 0
        assert exits[0]["reason"] == "trail_hit"
        exits2 = monitor.check_all()
        assert len(exits2) == 0


class TestMicroLiveR3:
    @pytest.mark.asyncio
    async def test_dry_run_tick(self) -> None:
        settings = MicroLiveSettings(
            enabled=True, acknowledged=True, dry_run=True, mode="micro_live"
        )
        policy = MicroLivePolicy(capital_cap_usd=50, default_slot_size_usd=5, max_slots=10)
        orch = MicroLiveOrchestrator(settings, policy)
        orch.adapter._market_limits = {
            "BTC/USDT": {
                "type": "spot",
                "active": True,
                "limits": {"cost": {"min": 0.01}, "amount": {"min": 0.00001}},
            }
        }
        await orch._tick(["BTC/USDT"], 0)
        assert len(orch._execution_log) >= 1
        entry = [e for e in orch._execution_log if e.get("side") == "buy"]
        exit_ = [e for e in orch._execution_log if e.get("side") == "sell"]
        assert len(entry) > 0
        assert len(exit_) > 0
        s = orch.account.summary()
        assert s["capital_cap"] == 50

    def test_cap_atomic(self) -> None:
        acct = MicroLiveAccount(capital_cap=50, slot_size=5, max_slots=10)
        p = acct.open_position("BTC/USDT", 50000, 0.0001, entry_fee=0.005)
        assert p is not None
        assert acct.state.effective_exposure <= 50.0


class TestPartialFills:
    @pytest.mark.asyncio
    async def test_partial_buy(self) -> None:
        exec_eng = PaperExecutionEngine(partial_fill_probability=0.5)
        import random

        random.seed(0)
        r = await exec_eng.simulate_fill("BTC/USDT", "buy", 0.1, bid=49990, ask=50010)
        assert r.status in ("FILLED", "PARTIALLY_FILLED")
        assert r.filled_qty > 0
        assert r.fees > 0

    @pytest.mark.asyncio
    async def test_no_oversell(self) -> None:
        exec_eng = PaperExecutionEngine(partial_fill_probability=0.5)
        import random

        random.seed(1)
        r = await exec_eng.simulate_fill("BTC/USDT", "sell", 0.1, bid=49990, ask=50010)
        assert r.filled_qty <= r.requested_qty
        assert r.remaining_qty >= 0


class TestSafetyInvariants:
    def test_spot_only(self) -> None:
        risk = RiskEngine()
        signal = StrategySignal(
            strategy_id="t",
            symbol="BTC-USDT",
            direction=SignalDirection.SHORT,
            confidence=0.9,
            estimated_return=0.02,
            required_capital=500,
            metadata={"entry_price": 50000.0},
        )
        opp = OpportunityEngine().evaluate(signal)
        a = risk.assess(opp)
        assert a.decision == RiskDecision.REJECTED

    def test_stop_030(self) -> None:
        from src.core.config import get_settings

        assert get_settings().risk.default_stop_loss_pct == 0.30

    def test_trail_002(self) -> None:
        assert TrailConfig().trailing_delta == 0.002

    def test_no_fixed_tp(self) -> None:
        assert TrailConfig().enable_fixed_take_profit is False

    def test_cap_50(self) -> None:
        acct = MicroLiveAccount(capital_cap=50)
        assert not acct.can_open_position(51.0)
        assert acct.can_open_position(5.0)

    def test_accounting_random(self) -> None:
        import random

        random.seed(99)
        acct = MicroLiveAccount(capital_cap=50, slot_size=5, max_slots=10)
        pids: list[str] = []
        for i in range(60):
            if (not pids or random.random() < 0.3) and len(pids) < 5:
                p = acct.open_position(
                    f"S{i}-USDT",
                    random.uniform(10, 200),
                    5.0 / random.uniform(10, 200),
                    entry_fee=0.005,
                )
                if p:
                    pids.append(p.position_id)
            elif pids:
                pid = pids.pop(0)
                acct.close_position(pid, random.uniform(10, 200), exit_fee=0.005)
        assert acct.state.cash_available >= -0.01
        assert acct.state.effective_exposure <= acct.state.micro_capital_cap + 0.02


class TestRuntimeModules:
    def test_scanner_accepts_features(self) -> None:
        scanner = GlobalScanner(
            ScannerConfig(min_signal_confidence=0.05, min_volume_24h_usd=100_000)
        )
        snap = AssetSnapshot(
            symbol="BTC-USDT",
            exchange="binance",
            asset_class=AssetClass.CRYPTO_SPOT,
            last_price=50000,
            bid=49999,
            ask=50001,
            spread_pct=0.004,
            volume_24h=500_000_000,
            price_change_1m_pct=2.0,
            price_change_5m_pct=5.0,
            volume_vs_avg_ratio=3.0,
            bid_ask_ratio=1.2,
            depth_bid_10bps=500.0,
        )
        results = scanner.scan([snap])
        sigs = scanner.to_strategy_signals(results)
        assert isinstance(sigs, list)

    def test_feed_health(self) -> None:
        fh = FeedHealthMonitor()
        fh.record_message("binance", "BTC-USDT", "ticker", exchange_ts=datetime.now(UTC))
        h = fh.get("binance", "BTC-USDT", "ticker")
        assert h is not None
        assert h.messages_received == 1
        assert h.is_healthy

    def test_order_book(self) -> None:
        obe = OrderBookEngine()
        snap = OrderBookSnapshot.create(
            "binance",
            CanonicalSymbol("binance", "BTC", "USDT"),
            [BookLevel(50000, 1.0)],
            [BookLevel(50010, 1.0)],
            last_update_id=1,
        )
        obe.apply_snapshot(snap)
        book = obe.get_book("binance", "BTC-USDT")
        assert book is not None
        assert book.best_bid == 50000.0

    def test_event_bus(self) -> None:
        async def _test():
            bus = EventBus(default_max_queue=100)
            results: list[str] = []

            async def handler(event):
                results.append(event.symbol)

            await bus.start()
            bus.subscribe("test", handler)
            evt = TickerEvent.create(
                "binance",
                CanonicalSymbol("binance", "BTC", "USDT"),
                50000,
                50001,
                50000.5,
                volume_24h=1000,
            )
            await bus.publish(evt)
            await asyncio.sleep(0.15)
            await bus.shutdown()
            assert len(results) >= 1

        asyncio.run(_test())
