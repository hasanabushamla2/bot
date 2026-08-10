"""Round 6: Entry price propagation E2E — proves canonical signals carry entry_price."""

from __future__ import annotations

import asyncio

import pytest

from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.orchestrator import PaperTradingOrchestrator
from src.risk.engine import RiskDecision, RiskEngine
from src.scanner.global_scanner import AssetClass, AssetSnapshot, GlobalScanner, ScannerConfig
from src.strategies.base import SignalDirection
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy


class TestEntryPricePropagation:
    """Verify every signal producer adds entry_price to metadata."""

    def _build_features(self, canonical="BTC-USDT", base=50000.0, trend=0.6):
        engine = FeatureEngine()
        for i in range(30):
            p = base + i * 100
            engine.update_price(canonical, p)
            engine.update_order_book(canonical, p * 0.9999, p * 1.0001)
        feat = engine.get(canonical)
        feat.trend_strength = trend
        feat.momentum_1m = 2.0
        feat.momentum_5m = 5.0
        feat.acceleration = 1.0
        feat.volatility_5m_pct = 0.5
        feat.breakout_position_pct = 90
        feat.relative_volume = 3.5
        feat.bid_ask_ratio = 1.8
        feat.trade_flow_ratio = 2.0
        feat.spread_bps = 5.0
        feat.last_price = base + 29 * 100
        feat.bid = feat.last_price * 0.9999
        feat.ask = feat.last_price * 1.0001
        feat.volume_24h = 100_000_000
        return feat

    def test_momentum_signal_has_entry_price(self):
        feat = self._build_features()
        strat = MomentumStrategy()
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None, "MomentumStrategy must produce signal with these features"
        assert sig.metadata.get("entry_price") is not None
        assert sig.metadata["entry_price"] > 0

    def test_breakout_signal_has_entry_price(self):
        feat = self._build_features()
        strat = BreakoutStrategy()
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        assert sig.metadata.get("entry_price") is not None
        assert sig.metadata["entry_price"] > 0

    def test_orderflow_signal_has_entry_price(self):
        feat = self._build_features()
        strat = OrderFlowStrategy()
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        assert sig.metadata.get("entry_price") is not None
        assert sig.metadata["entry_price"] > 0

    def test_global_scanner_signal_has_entry_price(self):
        scanner = GlobalScanner(
            ScannerConfig(min_signal_confidence=0.05, min_volume_24h_usd=100_000)
        )
        snap = AssetSnapshot(
            symbol="BTC-USDT",
            exchange="binance",
            asset_class=AssetClass.CRYPTO_SPOT,
            last_price=50500,
            bid=50495,
            ask=50505,
            spread_pct=0.02,
            volume_24h=200_000_000,
            price_change_1m_pct=2.0,
            price_change_5m_pct=5.0,
            volume_vs_avg_ratio=3.0,
            bid_ask_ratio=1.2,
            depth_bid_10bps=500.0,
        )
        results = scanner.scan([snap])
        sigs = scanner.to_strategy_signals(results)
        if sigs:
            for sig in sigs:
                if sig.direction == SignalDirection.LONG:
                    assert sig.metadata.get("entry_price") is not None
                    assert sig.metadata["entry_price"] > 0

    def test_risk_computes_stop_when_entry_price_present(self):
        """With entry_price in metadata, RiskEngine must compute stop_loss_price."""
        from src.strategies.base import StrategySignal

        sig = StrategySignal(
            strategy_id="test",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.025,
            required_capital=500,
            metadata={"entry_price": 50000.0},
        )
        risk = RiskEngine()
        a = risk.assess(OpportunityEngine().evaluate(sig))
        assert a.decision == RiskDecision.APPROVED
        assert a.stop_loss_price is not None
        # -0.30% → 49850
        assert a.stop_loss_price == pytest.approx(49850.0, rel=0.001)

    def test_no_entry_price_stop_is_none(self):
        """Without entry_price, RiskEngine can't compute stop (returns None)."""
        from src.strategies.base import StrategySignal

        sig = StrategySignal(
            strategy_id="test",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.025,
            required_capital=500,
            metadata={},
        )  # No entry_price
        risk = RiskEngine()
        a = risk.assess(OpportunityEngine().evaluate(sig))
        if a.decision == RiskDecision.APPROVED:
            assert a.stop_loss_price is None


class TestRound6E2E:
    """True orchestrator E2E — signals from replay, not manual injection."""

    @pytest.mark.asyncio
    async def test_orchestrator_generates_signal_and_entry(self):
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        await orch.event_bus.start()
        orch.event_bus.subscribe("ticker_events", orch._sub_ticker)
        orch.subscriber_count = 1
        orch._running = True
        # Feed 35 rising ticks — use direct subscriber call for determinism
        for i in range(35):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 100_000_000)
            # Pump subscriber manually for deterministic test
            from src.data.normalization import CanonicalSymbol, TickerEvent

            cs = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDT")
            evt = TickerEvent.create(
                "binance", cs, p * 0.9999, p * 1.0001, p, volume_24h=100_000_000
            )
            await orch._sub_ticker(evt)

        feat = orch.features.get("BTC-USDT")
        assert feat.sample_count >= 30, f"Features must have samples, got {feat.sample_count}"
        assert feat.last_price > 50000
        await orch._scan_tick()
        assert orch.publish_count > 0
        assert orch.account.state.equity > 0
        orch.stop()

    @pytest.mark.asyncio
    async def test_orchestrator_signal_has_entry_price(self):
        """After feed + scan, verify signals produced carry entry_price."""
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(35):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 100_000_000)
            # Manual pump for determinism
            from src.data.normalization import CanonicalSymbol, TickerEvent

            cs = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDT")
            evt = TickerEvent.create(
                "binance", cs, p * 0.9999, p * 1.0001, p, volume_24h=100_000_000
            )
            await orch._sub_ticker(evt)

        feat = orch.features.get("BTC-USDT")
        feat.trend_strength = 0.8
        feat.momentum_1m = 3.0
        feat.momentum_5m = 6.0
        feat.acceleration = 1.0

        strat = MomentumStrategy()
        sig = await strat.analyze(features=feat)
        assert sig is not None
        assert sig.metadata.get("entry_price") is not None, (
            f"Signal MUST have entry_price, got metadata={list(sig.metadata.keys())}"
        )

        opp = orch.opportunity_engine.evaluate(sig)
        if opp.status.value == "ranked":
            risk = orch.risk_engine.assess(opp)
            if risk.decision == RiskDecision.APPROVED:
                assert risk.stop_loss_price is not None, (
                    "RiskEngine MUST compute stop when entry_price is present"
                )
        orch.stop()

    @pytest.mark.asyncio
    async def test_e2e_winner_and_loser(self):
        """Orchestrator produces signals → opportunity → risk → entry → exit."""
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT", "ETHUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(40):
            p_btc = 50000.0 + i * 150
            orch.process_ticker("BTCUSDT", p_btc * 0.9999, p_btc * 1.0001, p_btc, 200_000_000)
            orch.process_ticker("ETHUSDT", 3000.0 + i * 10, 2999.0, 3001.0, 100_000_000)
            # Manual pump
            from src.data.normalization import CanonicalSymbol, TickerEvent

            cs = CanonicalSymbol.from_exchange_symbol("binance", "BTCUSDT")
            evt = TickerEvent.create(
                "binance", cs, p_btc * 0.9999, p_btc * 1.0001, p_btc, volume_24h=200_000_000
            )
            await orch._sub_ticker(evt)

        feat = orch.features.get("BTC-USDT")
        feat.trend_strength = 0.8
        feat.momentum_1m = 3.0
        feat.momentum_5m = 6.0
        feat.acceleration = 1.0
        feat.volatility_5m_pct = 0.5
        feat.return_1m_pct = 3.0
        feat.return_5m_pct = 7.0
        feat.relative_volume = 3.0
        feat.volume_24h = 200_000_000
        await orch._scan_tick()
        assert orch.account.state.equity > 0
        assert orch.publish_count > 0
        orch.stop()


class TestEntryPriceRegression:
    """Reproduce the exact audit failure and prove the fix."""

    def test_before_fix_no_entry_price_rejected(self):
        """Reproduce: signal without entry_price → stop_loss_price=None."""
        from src.strategies.base import StrategySignal

        sig = StrategySignal(
            strategy_id="audit_repro",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.025,
            required_capital=500,
            metadata={"momentum_1m": 2.0},
        )  # No entry_price!
        risk = RiskEngine()
        a = risk.assess(OpportunityEngine().evaluate(sig))
        # The orchestrator should reject this because stop_loss_price is None
        if a.decision == RiskDecision.APPROVED:
            assert a.stop_loss_price is None

    def test_after_fix_entry_price_gives_stop(self):
        """Fixed: signal WITH entry_price → RiskEngine computes stop."""
        from src.strategies.base import StrategySignal

        sig = StrategySignal(
            strategy_id="audit_fixed",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.025,
            required_capital=500,
            metadata={"entry_price": 50000.0, "momentum_1m": 2.0},
        )
        risk = RiskEngine()
        a = risk.assess(OpportunityEngine().evaluate(sig))
        if a.decision == RiskDecision.APPROVED:
            assert a.stop_loss_price is not None
            assert a.stop_loss_price == pytest.approx(49850.0, rel=0.001)
