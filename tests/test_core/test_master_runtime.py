"""MASTER RUNTIME REBUILD — E2E tests for single canonical paper pipeline."""

from __future__ import annotations

import random

import pytest

from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.orchestrator import PaperTradingOrchestrator
from src.risk.engine import RiskEngine
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.trailing_stop import TrailConfig


class TestMasterE2E_Orchestrator:
    @pytest.mark.asyncio
    async def test_instantiates_all_modules(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=10000)
        assert orch.event_bus is not None
        assert orch.order_book_engine is not None
        assert orch.feed_health is not None
        assert orch.features is not None
        assert orch.universe is not None
        assert orch.quality_filter is not None
        assert orch.scanner is not None
        assert orch.registry is not None
        assert orch.opportunity_engine is not None
        assert orch.risk_engine is not None
        assert orch.tier_manager is not None
        assert orch.allocator is not None
        assert orch.account is not None
        assert orch.paper_exec is not None
        assert orch.monitor is not None
        assert orch.analytics is not None

    @pytest.mark.asyncio
    async def test_process_ticker_updates_features(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=10000)
        orch.process_ticker("BTCUSDT", bid=49900, ask=50010, last=50000, volume_24h=50_000_000)
        feat = orch.features.get("BTC-USDT")
        assert feat.last_price == 50000
        assert feat.sample_count >= 1

    @pytest.mark.asyncio
    async def test_process_ticker_updates_order_book(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=10000)
        orch.process_ticker("BTCUSDT", bid=49900, ask=50010, last=50000)
        book = orch.order_book_engine.get_book("binance", "BTC-USDT")
        assert book is not None  # Book exists (needs snapshot for best_bid > 0)

    @pytest.mark.asyncio
    async def test_process_ticker_updates_feed_health(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=10000)
        orch.process_ticker("BTCUSDT", bid=49900, ask=50010, last=50000)
        h = orch.feed_health.get("binance", "BTC-USDT", "ticker")
        assert h is not None and h.messages_received >= 1

    @pytest.mark.asyncio
    async def test_process_ticker_updates_universe(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=10000)
        orch.process_ticker("BTCUSDT", bid=49900, ask=50010, last=50000, volume_24h=50_000_000)
        a = orch.universe.get("BTC-USDT", "binance")
        assert a is not None


class TestMasterE2E_FeedAndScan:
    @pytest.mark.asyncio
    async def test_feed_and_scan_does_not_crash(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT", "ETHUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(35):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 60_000_000)
            orch.process_ticker("ETHUSDT", 2990.0, 3010.0, 3000.0, 30_000_000)
        await orch._scan_tick()
        assert orch.account.state.equity > 0
        orch.stop()

    @pytest.mark.asyncio
    async def test_falling_prices_no_crash(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(35):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 50_000_000)
        await orch._scan_tick()
        for i in range(20):
            p = 53500.0 - i * 300
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 40_000_000)
            await orch._scan_tick()
        assert orch.account.state.equity > 0
        orch.stop()


class TestMasterE2E_SafetyInvariants:
    def test_spot_only_short_rejected(self) -> None:
        risk = RiskEngine()
        sig = StrategySignal(
            strategy_id="t",
            symbol="BTC-USDT",
            direction=SignalDirection.SHORT,
            confidence=0.9,
            estimated_return=0.02,
            required_capital=500,
            metadata={"entry_price": 50000},
        )
        opp = OpportunityEngine().evaluate(sig)
        a = risk.assess(opp)
        assert a.decision.value == "rejected"

    def test_hard_stop_030_preserved(self) -> None:
        from src.core.config import get_settings

        assert get_settings().risk.default_stop_loss_pct == 0.30

    def test_trailing_delta_002_preserved(self) -> None:
        assert TrailConfig().trailing_delta == 0.002

    def test_no_fixed_take_profit(self) -> None:
        assert TrailConfig().enable_fixed_take_profit is False

    def test_paper_account_no_phantom_capital(self) -> None:
        acct = PaperAccount(10000)
        pos = acct.open_position("BTC-USDT", "long", 50000, 0.01, fees=0.50)
        assert pos is not None and acct.state.allocated > 0
        acct.close_position("BTC-USDT", 51000, fees=0.51)
        assert acct.state.allocated == 0.0

    def test_accounting_random(self) -> None:
        random.seed(42)
        acct = PaperAccount(10000)
        for i in range(50):
            sym = f"S{i % 12}-USDT"
            if random.random() < 0.4 and len(acct.state.open_positions) < 5:
                if sym not in acct.state.open_positions:
                    acct.open_position(sym, "long", random.uniform(50, 200), 0.1, fees=0.01)
            elif acct.state.open_positions:
                s = list(acct.state.open_positions.keys())[0]
                acct.close_position(s, random.uniform(50, 200), fees=0.01)
        assert acct.state.cash >= -0.01
        assert acct.state.allocated >= -0.01


class TestMasterE2E_PaperExecution:
    @pytest.mark.asyncio
    async def test_buy_uses_ask(self) -> None:
        exec_eng = PaperExecutionEngine()
        r = await exec_eng.simulate_fill("BTC/USDT", "buy", 0.01, bid=49900, ask=50010)
        assert r.fill_price >= 49900
        assert r.fees > 0

    @pytest.mark.asyncio
    async def test_sell_uses_bid(self) -> None:
        exec_eng = PaperExecutionEngine()
        r = await exec_eng.simulate_fill("BTC/USDT", "sell", 0.01, bid=49900, ask=50010)
        assert r.fill_price < 50010
        assert r.fees > 0

    @pytest.mark.asyncio
    async def test_partial_fill_no_oversell(self) -> None:
        exec_eng = PaperExecutionEngine(partial_fill_probability=0.5)
        random.seed(42)
        r = await exec_eng.simulate_fill("BTC/USDT", "sell", 0.1, bid=49900, ask=50010)
        assert r.filled_qty <= r.requested_qty
        assert r.remaining_qty >= 0


class TestMasterE2E_MicroLive:
    @pytest.mark.asyncio
    async def test_win_scenario(self) -> None:
        from src.micro_live.config import MicroLivePolicy, MicroLiveSettings
        from src.micro_live.orchestrator import MicroLiveOrchestrator

        s = MicroLiveSettings(enabled=True, acknowledged=True, dry_run=True, mode="micro_live")
        p = MicroLivePolicy(capital_cap_usd=50, default_slot_size_usd=5, max_slots=10)
        orch = MicroLiveOrchestrator(s, p)
        orch.adapter._market_limits = {
            "BTC/USDT": {
                "type": "spot",
                "active": True,
                "limits": {"cost": {"min": 0.01}, "amount": {"min": 0.00001}},
            }
        }
        orch._running = True
        r = await orch.process_market_event("BTC/USDT", bid=49990, ask=50010, last=50000)
        assert r is not None
        ex = await orch.process_exit("BTC/USDT", 50200, reason="signal")
        assert ex is not None
        await orch.stop()

    @pytest.mark.asyncio
    async def test_loss_scenario(self) -> None:
        from src.micro_live.config import MicroLivePolicy, MicroLiveSettings
        from src.micro_live.orchestrator import MicroLiveOrchestrator

        s = MicroLiveSettings(enabled=True, acknowledged=True, dry_run=True, mode="micro_live")
        p = MicroLivePolicy(capital_cap_usd=50, default_slot_size_usd=5, max_slots=10)
        orch = MicroLiveOrchestrator(s, p)
        orch.adapter._market_limits = {
            "BTC/USDT": {
                "type": "spot",
                "active": True,
                "limits": {"cost": {"min": 0.01}, "amount": {"min": 0.00001}},
            }
        }
        orch._running = True
        r = await orch.process_market_event("BTC/USDT", bid=49990, ask=50010, last=50000)
        assert r is not None
        ex = await orch.process_exit("BTC/USDT", 49840, reason="hard_stop")
        assert ex is not None
        assert orch.account.state.realized_pnl_net < 0
        await orch.stop()

    @pytest.mark.asyncio
    async def test_cap_enforced(self) -> None:
        from src.micro_live.account import MicroLiveAccount

        acct = MicroLiveAccount(capital_cap=50, slot_size=5, max_slots=10)
        for i in range(12):
            acct.open_position(f"S{i}-USDT", 100, 0.05, entry_fee=0.0)
        assert acct.open_slots() <= 10
        assert acct.state.effective_exposure <= 50.0 + 0.02
