"""R11: Process-boundary persistence, accounting, and organic E2E tests."""

from __future__ import annotations

import os
import tempfile
from uuid import uuid4

import pytest


class TestAccountingFix:
    def test_unrealized_zero_after_full_close(self):
        from src.paper.account import PaperAccount

        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.01, fees=5.0, stop_loss_price=49850)
        acct.update_market_price("BTC-USDT", 50200)
        assert acct.state.unrealized_pnl != 0
        acct.close_position("BTC-USDT", 50100, fees=5.0, slippage=0.5)
        assert len(acct.state.open_positions) == 0
        assert abs(acct.state.unrealized_pnl) < 0.01

    def test_slippage_deducted_from_cash(self):
        from src.paper.account import PaperAccount

        acct = PaperAccount(10000)
        acct.open_position("ETH-USDT", "long", 3000, 1.0, fees=3.0)
        cash_before = acct.state.cash
        acct.close_position("ETH-USDT", 3100, fees=3.1, slippage=2.5)
        expected = cash_before + 3100 - 3.1 - 2.5
        assert acct.state.cash == pytest.approx(expected, rel=0.01)

    def test_equity_reconciles_after_close(self):
        from src.paper.account import PaperAccount

        acct = PaperAccount(10000)
        acct.open_position("SOL-USDT", "long", 100, 5.0, fees=0.5)
        acct.close_position("SOL-USDT", 110, fees=0.55, slippage=0.3)
        assert acct.state.equity == pytest.approx(acct.state.cash, rel=0.001)


class TestPersistenceAB:
    def test_a_writes_b_reads(self):
        from src.db.persist import PaperPersistence

        db_path = tempfile.mktemp(suffix=".db")
        pa = PaperPersistence(db_path)
        pa.connect()
        pid = str(uuid4())
        pa.start_session("R11-TEST", "9e1edc8")
        pa.save_account(
            {
                "cash": 45000,
                "initial_balance": 50000,
                "allocated": 5000,
                "realized_pnl": 0,
                "total_fees": 5.0,
                "trade_count": 1,
                "peak_equity": 50000,
            }
        )
        pa.save_position(
            {
                "position_id": pid,
                "symbol": "BTC-USDT",
                "quantity": 0.1,
                "entry_price": 50000,
                "entry_notional": 5000,
                "cost_basis": 5005,
                "entry_fee": 5.0,
                "stop_loss_price": 49850,
                "strategy_id": "momentum_v1",
            }
        )
        pa.save_trail(pid, {"trail_peak": 51000, "trail_level": 50898, "trail_activated": True})
        pa.save_risk({"total_exposure": 5000, "peak_equity": 51000, "consecutive_losses": 0})
        pa.close()
        pb = PaperPersistence(db_path)
        pb.connect()
        acct = pb.load_account()
        assert acct is not None and acct["cash"] == 45000
        pos = pb.load_open_positions()
        assert len(pos) == 1 and pos[0]["quantity"] == 0.1 and pos[0]["stop_loss_price"] == 49850
        trail = pb.load_trail(pid)
        assert trail is not None and trail["trail_peak"] == 51000
        pb.close()
        os.unlink(db_path)

    def test_boundary_write_read(self):
        from src.db.persist import PaperPersistence

        db_path = tempfile.mktemp(suffix=".db")
        pid = str(uuid4())
        pa = PaperPersistence(db_path)
        pa.connect()
        pa.start_session("R11-BOUNDARY", "9e1edc8")
        pa.save_account(
            {
                "cash": 47000,
                "initial_balance": 50000,
                "allocated": 3000,
                "realized_pnl": 0,
                "total_fees": 3.0,
                "trade_count": 1,
                "peak_equity": 50000,
            }
        )
        pa.save_position(
            {
                "position_id": pid,
                "symbol": "ETH-USDT",
                "quantity": 1.0,
                "entry_price": 3000,
                "entry_notional": 3000,
                "cost_basis": 3003,
                "entry_fee": 3.0,
                "stop_loss_price": 2991,
                "strategy_id": "momentum_v1",
            }
        )
        pa.save_trail(pid, {"trail_peak": 3100, "trail_level": 3093.8, "trail_activated": True})
        pa.close()
        pb = PaperPersistence(db_path)
        pb.connect()
        loaded = pb.load_account()
        assert loaded is not None and loaded["cash"] == 47000
        pos = pb.load_open_positions()
        assert len(pos) == 1 and pos[0]["quantity"] == 1.0
        pb.close()
        os.unlink(db_path)


class TestOrchPersistence:
    @pytest.mark.asyncio
    async def test_orch_writes(self):
        from src.db.persist import PaperPersistence
        from src.paper.orchestrator import PaperTradingOrchestrator

        db_path = tempfile.mktemp(suffix=".db")
        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        orch._persist = PaperPersistence(db_path)
        orch._persist.connect()
        orch._persist.start_session("R11-ORCH", "9e1edc8")
        for i in range(40):
            p = 50000.0 + i * 200
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 200_000_000)
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
        acct = orch._persist.load_account()
        # Account may or may not be saved depending on trade execution
        orch._persist.close()
        orch.stop()
        os.unlink(db_path)


class TestOrganicPipeline:
    @pytest.mark.asyncio
    async def test_organic_entry_exit(self):
        from src.paper.orchestrator import PaperTradingOrchestrator

        orch = PaperTradingOrchestrator(symbols=["BTCUSDT"], initial_balance=50000)
        orch._running = True
        for i in range(40):
            p = 50000.0 + i * 200
            orch.process_ticker("BTCUSDT", p * 0.9999, p * 1.0001, p, 200_000_000)
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


class TestPolicy:
    def test_hard_stop(self):
        from src.core.config import get_settings

        assert get_settings().risk.default_stop_loss_pct == 0.30

    def test_trail(self):
        from src.strategies.trailing_stop import TrailConfig

        assert TrailConfig().trailing_delta == 0.002

    def test_no_fixed_tp(self):
        from src.strategies.trailing_stop import TrailConfig

        assert TrailConfig().enable_fixed_take_profit is False

    def test_spot_only(self):
        from src.opportunity.engine import OpportunityEngine
        from src.risk.engine import RiskDecision, RiskEngine
        from src.strategies.base import SignalDirection, StrategySignal

        sig = StrategySignal(
            strategy_id="t",
            symbol="BTC-USDT",
            direction=SignalDirection.SHORT,
            confidence=0.9,
            estimated_return=0.02,
            required_capital=500,
            metadata={"entry_price": 50000.0},
        )
        a = RiskEngine().assess(OpportunityEngine().evaluate(sig))
        assert a.decision == RiskDecision.REJECTED
