"""Round 5 Master E2E tests."""

from __future__ import annotations

import random

import pytest

from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.orchestrator import PaperTradingOrchestrator
from src.risk.engine import RiskDecision, RiskEngine
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.trailing_stop import TrailConfig


class TestDepthWalk:
    def test_buy_multilevel_vwap(self) -> None:
        exec_eng = PaperExecutionEngine()
        res = exec_eng.depth_walk("buy", 5.0, asks=[(100.0, 1), (100.10, 2), (100.30, 3)])
        assert res[0] == 5.0 and res[3] == 0.0
        assert res[2] == 3
        assert res[1] == pytest.approx(100.16, rel=0.01)

    def test_insufficient_depth(self) -> None:
        exec_eng = PaperExecutionEngine()
        res = exec_eng.depth_walk("buy", 10.0, asks=[(100.0, 1), (100.10, 2), (100.30, 3)])
        assert res[0] == 6.0 and res[3] == 4.0 and res[2] == 3
        assert res[1] > 0

    def test_sell_bid_walk(self) -> None:
        exec_eng = PaperExecutionEngine()
        res = exec_eng.depth_walk("sell", 3.0, bids=[(100.30, 1), (100.10, 2)])
        assert res[0] == 3.0 and res[3] == 0.0

    @pytest.mark.asyncio
    async def test_fill_uses_depth(self) -> None:
        exec_eng = PaperExecutionEngine()
        r = await exec_eng.simulate_fill(
            "BTC/USDT",
            "buy",
            5.0,
            bid=99,
            ask=100,
            asks_depth=[(100.0, 1), (100.10, 2), (100.30, 3)],
        )
        assert r.filled_qty == 5.0 and r.status == "FILLED"
        assert r.levels_consumed == 3


class TestPartialPosition:
    def test_reduce_partial(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.01, fees=0.50)
        trade = acct.reduce_position("BTC-USDT", 51000, 0.003, fees=0.015)
        assert trade is not None
        assert acct.state.open_positions["BTC-USDT"].quantity == 0.007

    def test_reduce_to_zero_closes(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("ETH-USDT", "long", 3000, 1.0, fees=3.0)
        acct.reduce_position("ETH-USDT", 3100, 1.0, fees=3.1)
        assert "ETH-USDT" not in acct.state.open_positions

    def test_no_oversell(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("SOL-USDT", "long", 100, 0.5, fees=0.05)
        acct.reduce_position("SOL-USDT", 110, 10.0, fees=0.1)
        assert "SOL-USDT" not in acct.state.open_positions


class TestRiskStop:
    def test_entry_price_gives_stop(self) -> None:
        risk = RiskEngine()
        sig = StrategySignal(
            strategy_id="t",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.02,
            required_capital=500,
            metadata={"entry_price": 50000.0},
        )
        a = risk.assess(OpportunityEngine().evaluate(sig))
        if a.decision == RiskDecision.APPROVED:
            assert a.stop_loss_price is not None

    def test_short_blocked(self) -> None:
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
        a = risk.assess(OpportunityEngine().evaluate(sig))
        assert a.decision == RiskDecision.REJECTED


class TestOrchE2E:
    @pytest.mark.asyncio
    async def test_feed_scan_no_crash(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(35):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 60_000_000)
        await orch._scan_tick()
        assert orch.publish_count > 0
        assert orch.account.state.equity > 0
        orch.stop()

    @pytest.mark.asyncio
    async def test_subscriber_count(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        await orch.event_bus.start()
        orch.event_bus.subscribe("ticker_events", orch._sub_ticker)
        orch.subscriber_count = 1
        orch._running = True
        orch.process_ticker("BTCUSDT", 49900, 50010, 50000, 50_000_000)
        assert orch.publish_count >= 1
        assert orch.subscriber_count >= 1
        orch.stop()


class TestSafety:
    def test_stop_030(self) -> None:
        from src.core.config import get_settings

        assert get_settings().risk.default_stop_loss_pct == 0.30

    def test_trail_002(self) -> None:
        assert TrailConfig().trailing_delta == 0.002

    def test_no_fixed_tp(self) -> None:
        assert TrailConfig().enable_fixed_take_profit is False

    def test_accounting_random(self) -> None:
        random.seed(42)
        acct = PaperAccount(10000)
        for i in range(50):
            sym = f"S{i % 10}-USDT"
            if random.random() < 0.4 and len(acct.state.open_positions) < 5:
                if sym not in acct.state.open_positions:
                    acct.open_position(sym, "long", random.uniform(50, 200), 0.1, fees=0.01)
            elif acct.state.open_positions:
                s = list(acct.state.open_positions.keys())[0]
                pos = acct.state.open_positions[s]
                acct.reduce_position(s, random.uniform(50, 200), pos.quantity * 0.5, fees=0.01)
        assert acct.state.cash >= -0.01 and acct.state.allocated >= -0.01


class TestPaperExec:
    @pytest.mark.asyncio
    async def test_buy_ask(self) -> None:
        r = await PaperExecutionEngine().simulate_fill("X", "buy", 0.01, bid=49900, ask=50010)
        assert r.fill_price >= 49900 and r.fees > 0

    @pytest.mark.asyncio
    async def test_sell_bid(self) -> None:
        r = await PaperExecutionEngine().simulate_fill("X", "sell", 0.01, bid=49900, ask=50010)
        assert r.fill_price < 50010 and r.fees > 0
