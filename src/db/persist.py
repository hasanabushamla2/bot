# mypy: ignore-errors
"""R12: SQLite paper persistence — same schema as PostgreSQL path. Lease + closed trades."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)

SCHEMA = """CREATE TABLE IF NOT EXISTS paper_account(id INTEGER PRIMARY KEY CHECK(id=1),initial_balance REAL,cash REAL,allocated REAL DEFAULT 0,realized_pnl REAL DEFAULT 0,total_fees REAL DEFAULT 0,total_slippage REAL DEFAULT 0,trade_count INTEGER DEFAULT 0,win_count INTEGER DEFAULT 0,loss_count INTEGER DEFAULT 0,peak_equity REAL,max_drawdown_pct REAL DEFAULT 0,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_positions(position_id TEXT PRIMARY KEY,symbol TEXT,direction TEXT DEFAULT 'long',quantity REAL,entry_price REAL,entry_notional REAL,cost_basis REAL,entry_fee REAL DEFAULT 0,stop_loss_price REAL,strategy_id TEXT DEFAULT '',is_open INTEGER DEFAULT 1,opened_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_trail(position_id TEXT PRIMARY KEY REFERENCES paper_positions(position_id),trail_peak REAL,trail_level REAL DEFAULT 0,trail_activated INTEGER DEFAULT 0,exit_intent_active INTEGER DEFAULT 0,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_orders(order_id TEXT PRIMARY KEY,client_order_id TEXT UNIQUE,symbol TEXT,side TEXT,requested_qty REAL,filled_qty REAL DEFAULT 0,remaining_qty REAL,avg_fill_price REAL,status TEXT DEFAULT 'NEW',created_at TEXT,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_fills(fill_id TEXT PRIMARY KEY,order_id TEXT,symbol TEXT,side TEXT,quantity REAL,price REAL,notional REAL,fees REAL DEFAULT 0,slippage_bps REAL DEFAULT 0,filled_at TEXT);
CREATE TABLE IF NOT EXISTS paper_risk(id INTEGER PRIMARY KEY CHECK(id=1),total_exposure REAL DEFAULT 0,per_market TEXT DEFAULT '{}',per_strategy TEXT DEFAULT '{}',strat_counts TEXT DEFAULT '{}',peak_equity REAL DEFAULT 0,consecutive_losses INTEGER DEFAULT 0,breaker_active INTEGER DEFAULT 0,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_session(session_id TEXT PRIMARY KEY,commit_sha TEXT,started_at TEXT,ended_at TEXT,status TEXT DEFAULT 'STARTING');
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT,details TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS runtime_lease(account_id TEXT PRIMARY KEY,owner_id TEXT,acquired_at TEXT,heartbeat_at TEXT,expires_at TEXT);
CREATE TABLE IF NOT EXISTS paper_closed_trades(trade_id TEXT PRIMARY KEY,symbol TEXT,direction TEXT,entry_price REAL,exit_price REAL,quantity REAL,gross_pnl REAL,fees REAL,slippage_cost REAL,net_pnl REAL,return_pct REAL,exit_reason TEXT,strategy_id TEXT DEFAULT '',entry_time TEXT,exit_time TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS paper_symbol_risk(symbol TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_signal_state(signal_key TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_strategy_risk(strategy_id TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_telemetry_state(state_key TEXT PRIMARY KEY,state_json TEXT NOT NULL,updated_at TEXT);
CREATE TABLE IF NOT EXISTS paper_runtime_metrics(metric_name TEXT PRIMARY KEY,metric_value REAL NOT NULL DEFAULT 0,updated_at TEXT);"""


class PaperPersistence:
    def __init__(self, db_path: str = "data/paper_trading.db") -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate_schema()
        self._conn.commit()
        logger.info("persist_connected", path=self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _migrate_schema(self) -> None:
        """Apply additive SQLite migrations without invalidating old soak DBs."""
        if not self._conn:
            return
        additions: dict[str, dict[str, str]] = {
            "paper_account": {
                "unrealized_pnl": "REAL DEFAULT 0",
                "equity": "REAL DEFAULT 0",
            },
            "paper_positions": {
                "entry_reference_price": "REAL DEFAULT 0",
                "entry_slippage_cost": "REAL DEFAULT 0",
                "trail_activation_pct": "REAL DEFAULT 0",
                "current_price": "REAL DEFAULT 0",
                "unrealized_pnl": "REAL DEFAULT 0",
                "signal_id": "TEXT DEFAULT ''",
                "signal_timestamp": "TEXT",
                "entry_confidence": "REAL",
                "mfe_pct": "REAL DEFAULT 0",
                "mae_pct": "REAL DEFAULT 0",
                "metadata_json": "TEXT DEFAULT '{}'",
            },
            "paper_trail": {
                "activation_price": "REAL DEFAULT 0",
                "activation_pct": "REAL DEFAULT 0",
                "trail_distance_pct": "REAL DEFAULT 0",
            },
            "paper_closed_trades": {
                "entry_fee": "REAL DEFAULT 0",
                "exit_fee": "REAL DEFAULT 0",
                "holding_seconds": "REAL DEFAULT 0",
                "signal_id": "TEXT DEFAULT ''",
                "signal_timestamp": "TEXT",
                "entry_confidence": "REAL",
                "mfe_pct": "REAL DEFAULT 0",
                "mae_pct": "REAL DEFAULT 0",
            },
        }
        for table, columns in additions.items():
            existing = {
                str(row["name"])
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, definition in columns.items():
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )

    def _ensure_lease_table(self) -> None:
        """Ensure the runtime_lease table exists (called by orchestrator)."""
        if self._conn:
            self._conn.executescript(
                "CREATE TABLE IF NOT EXISTS runtime_lease(account_id TEXT PRIMARY KEY,owner_id TEXT,acquired_at TEXT,heartbeat_at TEXT,expires_at TEXT);"
            )
            self._conn.commit()

    @contextmanager
    def _tx(self):
        if not self._conn:
            raise RuntimeError("Not connected")
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _now(self):
        return datetime.now(UTC).isoformat()

    # ACCOUNT
    def save_account(self, s: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_account(id,initial_balance,cash,allocated,unrealized_pnl,equity,realized_pnl,total_fees,total_slippage,trade_count,win_count,loss_count,peak_equity,max_drawdown_pct,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s.get("initial_balance", 10000),
                    s.get("cash", 10000),
                    s.get("allocated", 0),
                    s.get("unrealized_pnl", 0),
                    s.get("equity", s.get("cash", 10000) + s.get("allocated", 0)),
                    s.get("realized_pnl", 0),
                    s.get("total_fees", 0),
                    s.get("total_slippage", 0),
                    s.get("trade_count", 0),
                    s.get("win_count", 0),
                    s.get("loss_count", 0),
                    s.get("peak_equity", s.get("cash", 10000)),
                    s.get("max_drawdown_pct", 0),
                    self._now(),
                ),
            )

    def load_account(self) -> dict | None:
        if not self._conn:
            return None
        r = self._conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        return dict(r) if r else None

    # POSITIONS
    def save_position(self, p: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_positions(position_id,symbol,direction,quantity,entry_price,entry_reference_price,entry_notional,cost_basis,entry_fee,entry_slippage_cost,stop_loss_price,trail_activation_pct,current_price,unrealized_pnl,strategy_id,signal_id,signal_timestamp,entry_confidence,mfe_pct,mae_pct,metadata_json,is_open,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    p["position_id"],
                    p["symbol"],
                    p.get("direction", "long"),
                    p["quantity"],
                    p["entry_price"],
                    p.get("entry_reference_price", p["entry_price"]),
                    p.get("entry_notional", 0),
                    p.get("cost_basis", 0),
                    p.get("entry_fee", 0),
                    p.get("entry_slippage_cost", 0),
                    p.get("stop_loss_price"),
                    p.get("trail_activation_pct", 0),
                    p.get("current_price", 0),
                    p.get("unrealized_pnl", 0),
                    p.get("strategy_id", ""),
                    p.get("signal_id", ""),
                    p.get("signal_timestamp"),
                    p.get("entry_confidence"),
                    p.get("mfe_pct", 0),
                    p.get("mae_pct", 0),
                    json.dumps(p.get("metadata", {})),
                    1,
                    p.get("opened_at", self._now()),
                    self._now(),
                ),
            )

    def delete_position(self, pid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM paper_trail WHERE position_id=?", (pid,))
            c.execute("DELETE FROM paper_positions WHERE position_id=?", (pid,))

    def delete_trail(self, pid: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM paper_trail WHERE position_id=?", (pid,))

    def cleanup_orphan_trails(self) -> int:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM paper_trail WHERE position_id NOT IN (SELECT position_id FROM paper_positions WHERE is_open=1)"
            )
            return cur.rowcount

    def count_orphan_trails(self) -> int:
        if not self._conn:
            return 0
        r = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trail WHERE position_id NOT IN (SELECT position_id FROM paper_positions WHERE is_open=1)"
        ).fetchone()
        return r["cnt"] if r else 0

    def load_open_positions(self) -> list[dict]:
        if not self._conn:
            return []
        return [
            dict(r)
            for r in self._conn.execute("SELECT * FROM paper_positions WHERE is_open=1").fetchall()
        ]

    # TRAILING
    def save_trail(self, pid: str, t: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_trail(position_id,trail_peak,trail_level,activation_price,activation_pct,trail_distance_pct,trail_activated,exit_intent_active,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    t.get("trail_peak", 0),
                    t.get("trail_level", 0),
                    t.get("activation_price", 0),
                    t.get("activation_pct", 0),
                    t.get("trail_distance_pct", 0),
                    1 if t.get("trail_activated") else 0,
                    1 if t.get("exit_intent_active") else 0,
                    self._now(),
                ),
            )

    def load_trail(self, pid: str) -> dict | None:
        if not self._conn:
            return None
        r = self._conn.execute("SELECT * FROM paper_trail WHERE position_id=?", (pid,)).fetchone()
        return dict(r) if r else None

    # ORDERS / FILLS
    def save_order(self, o: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_orders(order_id,client_order_id,symbol,side,requested_qty,filled_qty,remaining_qty,avg_fill_price,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    o["order_id"],
                    o["client_order_id"],
                    o["symbol"],
                    o["side"],
                    o["requested_qty"],
                    o.get("filled_qty", 0),
                    o.get("remaining_qty", o["requested_qty"]),
                    o.get("avg_fill_price"),
                    o.get("status", "NEW"),
                    o.get("created_at", self._now()),
                    self._now(),
                ),
            )

    def save_fill(self, f: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_fills(fill_id,order_id,symbol,side,quantity,price,notional,fees,slippage_bps,filled_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    f["fill_id"],
                    f["order_id"],
                    f["symbol"],
                    f["side"],
                    f["quantity"],
                    f["price"],
                    f.get("notional", f["price"] * f["quantity"]),
                    f.get("fees", 0),
                    f.get("slippage_bps", 0),
                    f.get("filled_at", self._now()),
                ),
            )

    def load_open_orders(self) -> list[dict]:
        if not self._conn:
            return []
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM paper_orders WHERE status NOT IN ('FILLED','CANCELED','REJECTED','PARTIALLY_FILLED_CANCELED')"
            ).fetchall()
        ]

    def client_order_exists(self, cid: str) -> bool:
        if not self._conn:
            return False
        return (
            self._conn.execute(
                "SELECT 1 FROM paper_orders WHERE client_order_id=?", (cid,)
            ).fetchone()
            is not None
        )

    # RISK STATE
    def save_risk(self, s: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_risk(id,total_exposure,per_market,per_strategy,strat_counts,peak_equity,consecutive_losses,breaker_active,updated_at) VALUES(1,?,?,?,?,?,?,?,?)",
                (
                    s.get("total_exposure", 0),
                    json.dumps(s.get("per_market_exposure", {})),
                    json.dumps(s.get("per_strategy_exposure", {})),
                    json.dumps(s.get("strategy_position_counts", {})),
                    s.get("peak_equity", 0),
                    s.get("consecutive_losses", 0),
                    1 if s.get("circuit_breaker_active") else 0,
                    self._now(),
                ),
            )

    def load_risk(self) -> dict | None:
        if not self._conn:
            return None
        r = self._conn.execute("SELECT * FROM paper_risk WHERE id=1").fetchone()
        if not r:
            return None
        d = dict(r)
        for f in ("per_market", "per_strategy", "strat_counts"):
            try:
                d[f] = json.loads(d.get(f, "{}"))
            except Exception:
                d[f] = {}
        return d

    # SESSION / AUDIT
    def start_session(self, sid: str, sha: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_session(session_id,commit_sha,started_at,status) VALUES(?,?,?,?)",
                (sid, sha, self._now(), "RUNNING"),
            )

    def end_session(self, sid: str, status: str = "COMPLETED") -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE paper_session SET ended_at=?,status=? WHERE session_id=?",
                (self._now(), status, sid),
            )

    def audit(self, event: str, details: str = "") -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO audit_log(event_type,details,created_at) VALUES(?,?,?)",
                (event, details, self._now()),
            )

    # ── R12: CLOSED TRADES (durable) ──
    def save_closed_trade(self, t: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_closed_trades(trade_id,symbol,direction,entry_price,exit_price,quantity,gross_pnl,fees,entry_fee,exit_fee,slippage_cost,net_pnl,return_pct,exit_reason,strategy_id,entry_time,exit_time,holding_seconds,signal_id,signal_timestamp,entry_confidence,mfe_pct,mae_pct,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    t["trade_id"],
                    t["symbol"],
                    t.get("direction", "long"),
                    t["entry_price"],
                    t["exit_price"],
                    t["quantity"],
                    t.get("gross_pnl", 0),
                    t.get("fees", t.get("entry_fee", 0) + t.get("exit_fee", 0)),
                    t.get("entry_fee", 0),
                    t.get("exit_fee", 0),
                    t.get("slippage_cost", 0),
                    t.get("net_pnl", 0),
                    t.get("return_pct", 0),
                    t.get("exit_reason", ""),
                    t.get("strategy_id", ""),
                    t.get("entry_time", self._now()),
                    t.get("exit_time", self._now()),
                    t.get("holding_seconds", 0),
                    t.get("signal_id", ""),
                    t.get("signal_timestamp"),
                    t.get("entry_confidence"),
                    t.get("mfe_pct", 0),
                    t.get("mae_pct", 0),
                    self._now(),
                ),
            )

    def load_closed_trades(self) -> list[dict]:
        if not self._conn:
            return []
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM paper_closed_trades ORDER BY exit_time DESC"
            ).fetchall()
        ]

    # ── R14: Bounded recent closed trades query ──
    def load_recent_closed_trades(self, limit: int = 200) -> list[dict]:
        if not self._conn:
            return []
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM paper_closed_trades ORDER BY exit_time DESC LIMIT ?", (limit,)
            ).fetchall()
        ]

    def count_closed_trades(self) -> int:
        if not self._conn:
            return 0
        r = self._conn.execute("SELECT COUNT(*) as cnt FROM paper_closed_trades").fetchone()
        return r["cnt"] if r else 0

    # ── R12: Order/fill idempotency checks ──
    def order_id_exists(self, order_id: str) -> bool:
        if not self._conn:
            return False
        return (
            self._conn.execute(
                "SELECT 1 FROM paper_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            is not None
        )

    def fill_id_exists(self, fill_id: str) -> bool:
        if not self._conn:
            return False
        return (
            self._conn.execute(
                "SELECT 1 FROM paper_fills WHERE fill_id=?", (fill_id,)
            ).fetchone()
            is not None
        )

    def closed_trade_exists(self, trade_id: str) -> bool:
        if not self._conn:
            return False
        return (
            self._conn.execute(
                "SELECT 1 FROM paper_closed_trades WHERE trade_id=?", (trade_id,)
            ).fetchone()
            is not None
        )

    # SYMBOL RISK / SIGNAL FRESHNESS / RUNTIME TELEMETRY
    def save_symbol_risk_state(self, state: dict[str, Any]) -> None:
        with self._tx() as c:
            for symbol, value in state.items():
                c.execute(
                    "INSERT OR REPLACE INTO paper_symbol_risk(symbol,state_json,updated_at) VALUES(?,?,?)",
                    (symbol, json.dumps(value), self._now()),
                )

    def load_symbol_risk_state(self) -> dict[str, Any]:
        if not self._conn:
            return {}
        result: dict[str, Any] = {}
        for row in self._conn.execute("SELECT symbol,state_json FROM paper_symbol_risk"):
            try:
                result[str(row["symbol"])] = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def save_signal_state(self, state: dict[str, Any]) -> None:
        with self._tx() as c:
            for signal_key, value in state.items():
                c.execute(
                    "INSERT OR REPLACE INTO paper_signal_state(signal_key,state_json,updated_at) VALUES(?,?,?)",
                    (signal_key, json.dumps(value), self._now()),
                )

    def load_signal_state(self) -> dict[str, Any]:
        if not self._conn:
            return {}
        result: dict[str, Any] = {}
        for row in self._conn.execute("SELECT signal_key,state_json FROM paper_signal_state"):
            try:
                result[str(row["signal_key"])] = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def save_strategy_risk_state(self, state: dict[str, Any]) -> None:
        with self._tx() as c:
            for strategy_id, value in state.items():
                c.execute(
                    "INSERT OR REPLACE INTO paper_strategy_risk(strategy_id,state_json,updated_at) VALUES(?,?,?)",
                    (str(strategy_id), json.dumps(value), self._now()),
                )

    def load_strategy_risk_state(self) -> dict[str, Any]:
        if not self._conn:
            return {}
        result: dict[str, Any] = {}
        for row in self._conn.execute("SELECT strategy_id,state_json FROM paper_strategy_risk"):
            try:
                result[str(row["strategy_id"])] = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return result

    def save_telemetry_state(self, state_key: str, state: dict[str, Any]) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO paper_telemetry_state(state_key,state_json,updated_at) VALUES(?,?,?)",
                (state_key, json.dumps(state), self._now()),
            )

    def load_telemetry_state(self, state_key: str) -> dict[str, Any]:
        if not self._conn:
            return {}
        row = self._conn.execute(
            "SELECT state_json FROM paper_telemetry_state WHERE state_key=?", (state_key,)
        ).fetchone()
        if row is None:
            return {}
        try:
            loaded = json.loads(row["state_json"])
            return loaded if isinstance(loaded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def save_runtime_metrics(self, metrics: dict[str, float | int]) -> None:
        with self._tx() as c:
            for name, value in metrics.items():
                c.execute(
                    "INSERT OR REPLACE INTO paper_runtime_metrics(metric_name,metric_value,updated_at) VALUES(?,?,?)",
                    (name, float(value), self._now()),
                )

    def load_runtime_metrics(self) -> dict[str, float]:
        if not self._conn:
            return {}
        return {
            str(row["metric_name"]): float(row["metric_value"])
            for row in self._conn.execute(
                "SELECT metric_name,metric_value FROM paper_runtime_metrics"
            )
        }
