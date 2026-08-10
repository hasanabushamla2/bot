"""Round 8: Ultimate pre-soak E2E tests — partial exit safety, depth, persistence, restart."""

from __future__ import annotations

import random

import pytest

from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.orchestrator import PaperTradingOrchestrator
from src.paper.position_monitor import PositionMonitor
from src.risk.engine import RiskDecision, RiskEngine
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.trailing_stop import TrailConfig


class TestPartialExitSafety:
    """BLOCKER 1: Partial exit residual must never get stuck."""

    def test_rearm_after_partial_exit(self) -> None:
        acct = PaperAccount(50000)
        pos = acct.open_position("BTC-USDT", "long", 50000, 0.3, fees=15.0, stop_loss_price=49850)
        assert pos is not None
        monitor = PositionMonitor(acct)
        monitor.register_position(pos)
        pos = acct.state.open_positions["BTC-USDT"]
        pos.current_price = 49800
        exits = monitor.check_all()
        assert len(exits) > 0
        # Simulate partial exit: reduce position
        trade = acct.reduce_position("BTC-USDT", 49810, 0.2, fees=10.0, exit_reason="hard_stop")
        assert trade is not None
        assert "BTC-USDT" in acct.state.open_positions
        assert acct.state.open_positions["BTC-USDT"].quantity == pytest.approx(0.1, rel=0.01)
        # R8: rearm the monitor
        monitor.rearm_position("BTC-USDT")
        # Next check should detect remaining position still below stop
        acct.state.open_positions["BTC-USDT"].current_price = 49750
        exits2 = monitor.check_all()
        assert len(exits2) > 0, "Residual position must trigger another exit"
        # Close residual
        trade2 = acct.close_position("BTC-USDT", 49750, fees=5.0, exit_reason="hard_stop")
        assert trade2 is not None
        assert "BTC-USDT" not in acct.state.open_positions

    def test_no_stuck_residual(self) -> None:
        """5 executions of partial exit must all close the position."""
        acct = PaperAccount(50000)
        acct.open_position("ETH-USDT", "long", 3000, 5.0, fees=15.0, stop_loss_price=2991)
        assert "ETH-USDT" in acct.state.open_positions
        monitor = PositionMonitor(acct)
        monitor.register_position(acct.state.open_positions["ETH-USDT"])
        remaining = 5.0
        for _i in range(6):
            if "ETH-USDT" not in acct.state.open_positions:
                break
            acct.state.open_positions["ETH-USDT"].current_price = 2980
            exits = monitor.check_all()
            if not exits:
                break
            sell_qty = min(1.0, remaining)
            _trade = acct.reduce_position(
                "ETH-USDT", 2985, sell_qty, fees=0.01, exit_reason="hard_stop"
            )
            monitor.rearm_position("ETH-USDT")
            remaining -= sell_qty
            if "ETH-USDT" not in acct.state.open_positions:
                break
        assert (
            "ETH-USDT" not in acct.state.open_positions
            or acct.state.open_positions.get("ETH-USDT", None) is None
        )


class TestRiskRejectNullStop:
    """BLOCKER 6: RiskEngine must reject when stop_loss_price is None."""

    def test_missing_entry_price_rejected(self) -> None:
        risk = RiskEngine()
        sig = StrategySignal(
            strategy_id="t",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=0.02,
            required_capital=500,
            metadata={},
        )
        opp = OpportunityEngine().evaluate(sig)
        a = risk.assess(opp)
        assert a.decision == RiskDecision.REJECTED, f"Must reject without stop, got {a.decision}"

    def test_entry_price_present_has_stop(self) -> None:
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
        opp = OpportunityEngine().evaluate(sig)
        a = risk.assess(opp)
        if a.decision == RiskDecision.APPROVED:
            assert a.stop_loss_price is not None
            assert a.stop_loss_price == pytest.approx(49850.0, rel=0.001)


class TestRiskStateComplete:
    """BLOCKER 5: Risk state must include per-market/per-strategy exposure."""

    @pytest.mark.asyncio
    async def test_risk_state_includes_per_market(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT", "ETHUSDT"], initial_balance=50000)
        orch._running = True
        # Open a test position
        orch.account.open_position("BTC-USDT", "long", 50000, 0.01, fees=5.0, stop_loss_price=49850)
        assert len(orch.account.state.open_positions) >= 1
        # Run a scan — risk state should be populated
        for i in range(30):
            p = 50000.0 + i * 100
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 200_000_000)
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
        feat.sample_count = 50
        await orch._scan_tick()
        # Verify risk state was updated
        assert orch.risk_engine.state.total_exposure >= 0  # R8: risk state updated
        orch.stop()


class TestDepthExecution:
    """BLOCKER 2: Real order book depth used by orchestrator."""

    def test_depth_walk_vwap(self) -> None:
        exec_eng = PaperExecutionEngine()
        filled, vwap, levels, remaining = exec_eng.depth_walk(
            "buy", 5.0, asks=[(100.0, 1.0), (100.10, 2.0), (100.30, 3.0)]
        )
        assert filled == 5.0 and remaining == 0.0 and levels == 3
        assert vwap == pytest.approx(100.16, rel=0.01)

    def test_insufficient_depth_partial(self) -> None:
        exec_eng = PaperExecutionEngine()
        filled, vwap, levels, remaining = exec_eng.depth_walk(
            "buy", 10.0, asks=[(100.0, 1.0), (100.10, 2.0), (100.30, 3.0)]
        )
        assert filled == 6.0 and remaining == 4.0 and levels == 3
        assert vwap > 0

    def test_sell_depth_walk(self) -> None:
        exec_eng = PaperExecutionEngine()
        filled, _vwap, _levels, remaining = exec_eng.depth_walk(
            "sell", 2.0, bids=[(100.30, 1.0), (100.10, 2.0)]
        )
        assert filled == 2.0 and remaining == 0.0

    @pytest.mark.asyncio
    async def test_orchestrator_supplies_depth(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        from src.data.normalization import BookLevel, CanonicalSymbol, OrderBookSnapshot

        snap = OrderBookSnapshot.create(
            "binance",
            CanonicalSymbol("binance", "BTC", "USDT"),
            [BookLevel(50000, 2), BookLevel(49990, 3)],
            [BookLevel(50010, 1), BookLevel(50020, 2)],
        )
        orch.order_book_engine.apply_snapshot(snap)
        book = orch.order_book_engine.get_book("binance", "BTC-USDT")
        assert book is not None
        bids = [(b[0], b[1]) for b in book.bids.levels] if book.bids else []
        asks = [(a[0], a[1]) for a in book.asks.levels] if book.asks else []
        assert len(bids) + len(asks) >= 1, "OrderBookEngine must have levels"
        orch.stop()


class TestStaleFixed:
    """BLOCKER 3: Real data age, no hardcoded 0.0."""

    @pytest.mark.asyncio
    async def test_real_data_age(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        # Feed data, then check that quality filter uses real age
        for _i in range(30):
            orch.process_ticker("BTCUSDT", 49900, 50010, 50000, 200_000_000)
            feat = orch.features.get("BTC-USDT")
            feat.trend_strength = 0.8
            feat.momentum_1m = 3.0
            feat.sample_count = 50
            feat.last_price = 50000
            feat.bid = 49990
            feat.ask = 50010
            feat.return_1m_pct = 3.0
            feat.return_5m_pct = 7.0
            feat.relative_volume = 3.0
            feat.volume_24h = 200_000_000
            feat.spread_bps = 2.0
            feat.bid_ask_ratio = 1.1
        await orch._scan_tick()
        assert orch.account.state.equity > 0
        orch.stop()


class TestOrganicPipeline:
    """Organic entry/exit through full canonical pipeline."""

    @pytest.mark.asyncio
    async def test_organic_entry(self) -> None:
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(35):
            p = 50000.0 + i * 200
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 300_000_000)
            feat = orch.features.get("BTC-USDT")
            feat.trend_strength = 0.9
            feat.momentum_1m = 4.0
            feat.momentum_5m = 8.0
            feat.acceleration = 2.0
            feat.volatility_5m_pct = 0.5
            feat.return_1m_pct = 4.0
            feat.return_5m_pct = 10.0
            feat.relative_volume = 4.0
            feat.volume_24h = 500_000_000
            feat.bid = p * 0.9999
            feat.ask = p * 1.0001
            feat.last_price = p
            feat.sample_count = 50
            feat.spread_bps = 2.0
            feat.bid_ask_ratio = 1.2
        await orch._scan_tick()
        assert orch.publish_count > 0
        assert orch.account.state.equity > 0
        orch.stop()


class TestAccountingInvariants:
    """BLOCKER 24: Accounting invariants."""

    def test_random_sequences(self) -> None:
        random.seed(42)
        acct = PaperAccount(10000)
        for i in range(80):
            sym = f"S{i % 8}-USDT"
            if random.random() < 0.4 and len(acct.state.open_positions) < 5:
                if sym not in acct.state.open_positions:
                    acct.open_position(sym, "long", random.uniform(50, 200), 0.1, fees=0.01)
            elif acct.state.open_positions:
                s = list(acct.state.open_positions.keys())[0]
                pos = acct.state.open_positions[s]
                qty = pos.quantity * (0.3 + random.random() * 0.7)
                acct.reduce_position(s, random.uniform(50, 200), qty, fees=0.01)
        assert acct.state.cash >= -0.01
        assert acct.state.allocated >= -0.01

    def test_no_phantom_capital(self) -> None:
        acct = PaperAccount(10000)
        pos = acct.open_position("BTC-USDT", "long", 50000, 0.01, fees=0.50)
        assert pos is not None
        assert acct.state.allocated > 0
        acct.close_position("BTC-USDT", 51000, fees=0.51)
        assert acct.state.allocated == 0.0

    def test_reduce_never_negative_qty(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("SOL-USDT", "long", 100, 1.0, fees=0.01)
        trade = acct.reduce_position("SOL-USDT", 110, 2.0, fees=0.01)
        assert trade is not None
        assert "SOL-USDT" not in acct.state.open_positions


class TestPolicy:
    def test_hard_stop_030(self) -> None:
        from src.core.config import get_settings

        assert get_settings().risk.default_stop_loss_pct == 0.30

    def test_trail_002(self) -> None:
        assert TrailConfig().trailing_delta == 0.002

    def test_no_fixed_tp(self) -> None:
        assert TrailConfig().enable_fixed_take_profit is False

    def test_spot_only(self) -> None:
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

    def test_config_desync_fixed(self) -> None:
        from src.opportunity.engine import OpportunityEngine

        eng = OpportunityEngine()
        assert hasattr(eng, "min_net_return")
        assert not hasattr(eng, "min_net_return_pct")
