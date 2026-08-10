"""R9: Instance A → B full persistence test."""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import UTC, datetime

from src.db.persist import PaperPersistence


class TestPersistenceAB:
    def test_save_load_account(self):
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        db.save_account(
            {"cash": 50000, "initial_balance": 50000, "trade_count": 3, "peak_equity": 52000}
        )
        loaded = db.load_account()
        assert loaded is not None and loaded["cash"] == 50000 and loaded["trade_count"] == 3
        db.close()
        os.unlink(db.db_path)

    def test_position_persist_restore(self):
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        pid = str(uuid.uuid4())
        db.save_position(
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
                "opened_at": datetime.now(UTC).isoformat(),
            }
        )
        loaded = db.load_open_positions()
        assert (
            len(loaded) == 1 and loaded[0]["symbol"] == "BTC-USDT" and loaded[0]["quantity"] == 0.1
        )
        db.delete_position(pid)
        assert len(db.load_open_positions()) == 0
        db.close()
        os.unlink(db.db_path)

    def test_trail_persist_restore(self):
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        pid = str(uuid.uuid4())
        db.save_position(
            {
                "position_id": pid,
                "symbol": "BTC-USDT",
                "quantity": 0.1,
                "entry_price": 50000,
                "entry_notional": 5000,
                "cost_basis": 5005,
                "entry_fee": 5.0,
                "stop_loss_price": 49850,
                "opened_at": datetime.now(UTC).isoformat(),
            }
        )
        db.save_trail(pid, {"trail_peak": 51000, "trail_level": 50898, "trail_activated": True})
        loaded = db.load_trail(pid)
        assert (
            loaded is not None and loaded["trail_peak"] == 51000 and loaded["trail_activated"] == 1
        )
        db.close()
        os.unlink(db.db_path)

    def test_order_fill_persist(self):
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        oid = str(uuid.uuid4())
        cid = f"client-{uuid.uuid4()}"
        db.save_order(
            {
                "order_id": oid,
                "client_order_id": cid,
                "symbol": "BTC-USDT",
                "side": "buy",
                "requested_qty": 0.1,
                "filled_qty": 0.1,
                "remaining_qty": 0,
                "avg_fill_price": 50010,
                "status": "FILLED",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        db.save_fill(
            {
                "fill_id": str(uuid.uuid4()),
                "order_id": oid,
                "symbol": "BTC-USDT",
                "side": "buy",
                "quantity": 0.1,
                "price": 50010,
                "fees": 5.0,
                "slippage_bps": 2.0,
                "filled_at": datetime.now(UTC).isoformat(),
            }
        )
        assert db.client_order_exists(cid)
        assert not db.client_order_exists("nonexistent")
        db.close()
        os.unlink(db.db_path)

    def test_risk_state_persist(self):
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        db.save_risk(
            {
                "total_exposure": 5000,
                "per_market_exposure": {"BTC": 5000},
                "per_strategy_exposure": {"momentum_v1": 5000},
                "strategy_position_counts": {"momentum_v1": 1},
                "peak_equity": 52000,
                "consecutive_losses": 2,
                "circuit_breaker_active": False,
            }
        )
        loaded = db.load_risk()
        assert (
            loaded is not None
            and loaded["total_exposure"] == 5000
            and loaded["per_market"] == {"BTC": 5000}
            and loaded["consecutive_losses"] == 2
        )
        db.close()
        os.unlink(db.db_path)

    def test_instance_a_to_b_full(self):
        """R9: INSTANCE A writes, INSTANCE B restores automatically."""
        db = PaperPersistence(tempfile.mktemp(suffix=".db"))
        db.connect()
        pid = str(uuid.uuid4())
        db.start_session("R9-TEST", "65cb1a4")
        db.audit("RUNTIME_STARTED", "Instance A")
        db.save_account(
            {
                "cash": 45000,
                "initial_balance": 50000,
                "allocated": 5000,
                "realized_pnl": 0,
                "total_fees": 5.0,
                "trade_count": 1,
                "win_count": 0,
                "loss_count": 0,
                "peak_equity": 50000,
            }
        )
        db.save_position(
            {
                "position_id": pid,
                "symbol": "BTC-USDT",
                "direction": "long",
                "quantity": 0.1,
                "entry_price": 50000,
                "entry_notional": 5000,
                "cost_basis": 5005,
                "entry_fee": 5.0,
                "stop_loss_price": 49850,
                "strategy_id": "momentum_v1",
                "opened_at": datetime.now(UTC).isoformat(),
            }
        )
        db.save_trail(
            pid,
            {
                "trail_peak": 51000,
                "trail_level": 50898,
                "trail_activated": True,
                "exit_intent_active": False,
            },
        )
        db.save_risk(
            {
                "total_exposure": 5000,
                "per_market_exposure": {"BTC": 5000},
                "per_strategy_exposure": {"momentum_v1": 5000},
                "strategy_position_counts": {"momentum_v1": 1},
                "peak_equity": 51000,
                "consecutive_losses": 0,
                "circuit_breaker_active": False,
            }
        )
        oid = str(uuid.uuid4())
        cid = f"exit-{uuid.uuid4()}"
        db.save_order(
            {
                "order_id": oid,
                "client_order_id": cid,
                "symbol": "BTC-USDT",
                "side": "sell",
                "requested_qty": 0.1,
                "filled_qty": 0.06,
                "remaining_qty": 0.04,
                "avg_fill_price": 49850,
                "status": "PARTIALLY_FILLED",
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        db.close()

        # INSTANCE B
        db2 = PaperPersistence(db.db_path)
        db2.connect()
        db2.audit("RUNTIME_RESTORED", "Instance B")
        acct = db2.load_account()
        assert acct is not None and acct["cash"] == 45000 and acct["allocated"] == 5000
        positions = db2.load_open_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert (
            pos["position_id"] == pid
            and pos["symbol"] == "BTC-USDT"
            and pos["quantity"] == 0.1
            and pos["entry_price"] == 50000
            and pos["stop_loss_price"] == 49850
        )
        trail = db2.load_trail(pid)
        assert trail is not None and trail["trail_peak"] == 51000 and trail["trail_activated"] == 1
        assert db2.client_order_exists(cid)
        risk = db2.load_risk()
        assert risk is not None and risk["total_exposure"] == 5000 and risk["peak_equity"] == 51000
        db2.end_session("R9-TEST", "COMPLETED")
        db2.close()
        os.unlink(db2.db_path)
