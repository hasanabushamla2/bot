"""FastAPI Monitoring API — read-only paper trading engine inspection.

NO trading actions. NO buy/sell controls. NO real orders.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

START_TIME = datetime.now(UTC)

app = FastAPI(
    title="Quant Engine Monitoring API",
    description="Read-only inspection endpoints for the paper trading engine.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── Helpers ──────────────────────────────────────────────────────────

def _uptime_seconds() -> float:
    return (datetime.now(UTC) - START_TIME).total_seconds()


def _read_db(db_path: str) -> dict[str, Any] | None:
    """Read SQLite paper database for current state."""
    import sqlite3
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    result: dict[str, Any] = {}

    # Account
    acct = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    result["account"] = dict(acct) if acct else {}

    # Positions
    result["positions"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM paper_positions WHERE is_open=1"
        ).fetchall()
    ]

    # Risk
    risk = conn.execute("SELECT * FROM paper_risk WHERE id=1").fetchone()
    result["risk"] = dict(risk) if risk else {}

    # Session
    sess = conn.execute(
        "SELECT * FROM paper_session ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    result["session"] = dict(sess) if sess else {}

    # Orders/Fills counts
    result["order_count"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM paper_orders"
    ).fetchone()["cnt"]
    result["fill_count"] = conn.execute(
        "SELECT COUNT(*) as cnt FROM paper_fills"
    ).fetchone()["cnt"]

    # Closed trades
    result["closed_trades"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM paper_closed_trades ORDER BY exit_time DESC LIMIT 500"
        ).fetchall()
    ]

    # Trail states
    result["trails"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM paper_trail"
        ).fetchall()
    ]

    # Lease
    lease = conn.execute("SELECT * FROM runtime_lease WHERE account_id='paper-account-1'").fetchone()
    result["lease"] = dict(lease) if lease else {}

    # Audit log
    result["audit_log"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT 100"
        ).fetchall()
    ]

    conn.close()
    return result


def _read_logs(lines: int = 200) -> list[str]:
    log_dir = Path("logs")
    log_file = log_dir / "engine.log"
    entries: list[str] = []
    if not log_file.exists():
        return entries
    try:
        with open(log_file) as f:
            for line in f:
                entries.append(line.strip())
                if len(entries) > lines:
                    entries = entries[-lines:]
    except Exception:
        pass
    return entries


def _get_heartbeat_age() -> float | None:
    hb_path = Path("data/.heartbeat")
    if not hb_path.exists():
        return None
    return time.time() - hb_path.stat().st_mtime


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    db_exists = os.path.exists(db_path)
    hb_age = _get_heartbeat_age()

    return {
        "status": "healthy" if db_exists else "no_data",
        "uptime_seconds": _uptime_seconds(),
        "started_at": START_TIME.isoformat(),
        "engine_running": db_exists and (hb_age is not None and hb_age < 120),
        "live_trading": False,
        "mode": "PAPER",
        "heartbeat_age_seconds": hb_age,
        "db_path": db_path,
        "db_exists": db_exists,
    }


@app.get("/metrics")
async def metrics():
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    data = _read_db(db_path)

    if data is None:
        return {"events": 0, "consumed": 0, "signals": 0, "opportunities": 0,
                "risk_assessments": 0, "orders": 0, "fills": 0, "no_db": True}

    acct = data.get("account", {})
    return {
        "events": 0,  # runtime-only counters; session metadata for persisted view
        "consumed": 0,
        "signals": 0,
        "opportunities": 0,
        "risk_assessments": 0,
        "orders": data.get("order_count", 0),
        "fills": data.get("fill_count", 0),
        "equity": acct.get("cash", 0) + acct.get("allocated", 0),
        "cash": acct.get("cash", 0),
        "realized_pnl": acct.get("realized_pnl", 0),
        "trade_count": acct.get("trade_count", 0),
        "win_count": acct.get("win_count", 0),
        "loss_count": acct.get("loss_count", 0),
    }


@app.get("/positions")
async def positions():
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    data = _read_db(db_path)
    if data is None:
        return {"positions": [], "count": 0}

    positions = data.get("positions", [])
    trails = {t["position_id"]: t for t in data.get("trails", [])}

    enriched = []
    for p in positions:
        pid = p.get("position_id", "")
        trail = trails.get(pid, {})
        enriched.append({
            "symbol": p.get("symbol", ""),
            "direction": p.get("direction", "long"),
            "quantity": p.get("quantity", 0),
            "entry_price": p.get("entry_price", 0),
            "entry_notional": p.get("entry_notional", 0),
            "stop_loss_price": p.get("stop_loss_price", 0),
            "strategy_id": p.get("strategy_id", ""),
            "trail_peak": trail.get("trail_peak", 0),
            "trail_activated": bool(trail.get("trail_activated", 0)),
            "opened_at": p.get("opened_at", ""),
        })

    return {"positions": enriched, "count": len(enriched)}


@app.get("/trades")
async def trades(limit: int = 500):
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    data = _read_db(db_path)
    if data is None:
        return {"trades": [], "count": 0}

    trades_list = data.get("closed_trades", [])[:limit]
    return {
        "trades": [
            {
                "trade_id": t.get("trade_id", ""),
                "symbol": t.get("symbol", ""),
                "direction": t.get("direction", "long"),
                "entry_price": t.get("entry_price", 0),
                "exit_price": t.get("exit_price", 0),
                "quantity": t.get("quantity", 0),
                "gross_pnl": t.get("gross_pnl", 0),
                "fees": t.get("fees", 0),
                "slippage_cost": t.get("slippage_cost", 0),
                "net_pnl": t.get("net_pnl", 0),
                "return_pct": t.get("return_pct", 0),
                "exit_reason": t.get("exit_reason", ""),
                "strategy_id": t.get("strategy_id", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("exit_time", ""),
            }
            for t in trades_list
        ],
        "count": len(trades_list),
    }


@app.get("/risk")
async def risk():
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    data = _read_db(db_path)
    if data is None:
        return {"cash": 0, "equity": 0, "exposure": 0, "risk_state": {}}

    acct = data.get("account", {})
    risk_data = data.get("risk", {})

    return {
        "cash": acct.get("cash", 0),
        "equity": acct.get("cash", 0) + acct.get("allocated", 0) + acct.get("realized_pnl", 0),
        "allocated": acct.get("allocated", 0),
        "realized_pnl": acct.get("realized_pnl", 0),
        "total_fees": acct.get("total_fees", 0),
        "total_slippage": acct.get("total_slippage", 0),
        "trade_count": acct.get("trade_count", 0),
        "peak_equity": acct.get("peak_equity", 0),
        "max_drawdown_pct": acct.get("max_drawdown_pct", 0),
        "exposure": risk_data.get("total_exposure", 0),
        "per_market": _safe_json(risk_data.get("per_market", "{}")),
        "per_strategy": _safe_json(risk_data.get("per_strategy", "{}")),
        "consecutive_losses": risk_data.get("consecutive_losses", 0),
        "breaker_active": bool(risk_data.get("breaker_active", 0)),
    }


@app.get("/system")
async def system():
    db_path = os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")
    data = _read_db(db_path)

    import resource as resmod
    try:
        rss_mb = resmod.getrusage(resmod.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        rss_mb = 0.0

    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    hb_age = _get_heartbeat_age()

    lease_info = {}
    if data:
        lease_info = data.get("lease", {})

    return {
        "memory": {
            "rss_mb": round(rss_mb, 2),
        },
        "database": {
            "path": db_path,
            "size_bytes": db_size,
            "size_mb": round(db_size / (1024 * 1024), 2) if db_size else 0,
            "exists": os.path.exists(db_path),
        },
        "heartbeat": {
            "file_exists": Path("data/.heartbeat").exists() if Path("data").exists() else False,
            "age_seconds": round(hb_age, 1) if hb_age is not None else None,
            "healthy": hb_age is not None and hb_age < 120,
        },
        "lease": {
            "owner_id": lease_info.get("owner_id", ""),
            "acquired_at": lease_info.get("acquired_at", ""),
            "heartbeat_at": lease_info.get("heartbeat_at", ""),
            "expires_at": lease_info.get("expires_at", ""),
            "active": bool(lease_info),
        },
        "feed_health": {
            "status": "monitoring",
            "note": "Feed health managed by orchestrator runtime",
        },
        "uptime_seconds": _uptime_seconds(),
    }


@app.get("/logs")
async def logs(limit: int = 200, level: str = ""):
    entries = _read_logs(limit)
    parsed: list[dict[str, Any]] = []
    for line in entries:
        try:
            obj = json.loads(line)
            if level and obj.get("level", "").upper() != level.upper():
                continue
            parsed.append({
                "timestamp": obj.get("timestamp", ""),
                "level": obj.get("level", ""),
                "event": obj.get("event", ""),
                "message": json.dumps({k: v for k, v in obj.items()
                                       if k not in ("timestamp", "level", "event", "logger")}),
            })
        except json.JSONDecodeError:
            parsed.append({"timestamp": "", "level": "RAW", "event": line[:200], "message": ""})

    return {"logs": parsed[-limit:], "count": len(parsed)}


def _safe_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val
