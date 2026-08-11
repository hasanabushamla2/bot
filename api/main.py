"""FastAPI Monitoring API — R19: cross-platform hardened, Windows-safe.

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


# ══════════════════════════════════════════════════════════════════════
# Cross-platform helpers
# ══════════════════════════════════════════════════════════════════════

def _get_memory_mb() -> float | None:
    """Get process RSS memory in MB. Cross-platform via psutil or null."""
    try:
        import psutil  # type: ignore[import-untyped]
        proc = psutil.Process()
        return proc.memory_info().rss / (1024 * 1024)
    except Exception:
        # Windows fallback: try resource, but it's Unix-only
        try:
            import resource as _res
            return _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            return None


def _uptime_seconds() -> float:
    return (datetime.now(UTC) - START_TIME).total_seconds()


def _get_db_path() -> str:
    """Resolve PAPER_DB_PATH from env, with no silent fallback."""
    return os.environ.get("PAPER_DB_PATH", "data/paper_trading.db")


def _get_db_info(db_path: str) -> dict[str, Any]:
    """Return database metadata without reading contents."""
    p = Path(db_path)
    exists = p.exists()
    info: dict[str, Any] = {
        "configured_path": db_path,
        "resolved_path": str(p.resolve()) if exists else str(p),
        "filename": p.name,
        "exists": exists,
        "size_bytes": p.stat().st_size if exists else 0,
        "size_mb": round(p.stat().st_size / (1024 * 1024), 2) if exists else 0,
        "last_modified": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).isoformat() if exists else None,
    }
    return info


def _read_db(db_path: str) -> dict[str, Any] | None:
    """Read SQLite paper database for current state. Graceful on missing DB."""
    import sqlite3
    if not os.path.exists(db_path):
        return None
    result: dict[str, Any] = {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

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
        try:
            result["order_count"] = conn.execute(
                "SELECT COUNT(*) as cnt FROM paper_orders"
            ).fetchone()["cnt"]
        except Exception:
            result["order_count"] = 0
        try:
            result["fill_count"] = conn.execute(
                "SELECT COUNT(*) as cnt FROM paper_fills"
            ).fetchone()["cnt"]
        except Exception:
            result["fill_count"] = 0

        # Closed trades
        try:
            result["closed_trades"] = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM paper_closed_trades ORDER BY exit_time DESC LIMIT 500"
                ).fetchall()
            ]
        except Exception:
            result["closed_trades"] = []

        # Trail states
        try:
            result["trails"] = [
                dict(r) for r in conn.execute("SELECT * FROM paper_trail").fetchall()
            ]
        except Exception:
            result["trails"] = []

        # Lease
        try:
            lease = conn.execute(
                "SELECT * FROM runtime_lease WHERE account_id='paper-account-1'"
            ).fetchone()
            result["lease"] = dict(lease) if lease else {}
        except Exception:
            result["lease"] = {}

        # Audit log
        try:
            result["audit_log"] = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT 100"
                ).fetchall()
            ]
        except Exception:
            result["audit_log"] = []

        conn.close()
    except sqlite3.DatabaseError:
        # Corrupt or locked DB — return empty
        return {"db_error": "Database read failed"}

    return result


def _get_heartbeat_age(db_path: str) -> float | None:
    """Get heartbeat file age, relative to the configured DB path."""
    db_dir = str(Path(db_path).parent) if Path(db_path).parent != Path(".") else "data"
    hb_path = Path(db_dir) / ".heartbeat"
    if not hb_path.exists():
        return None
    return time.time() - hb_path.stat().st_mtime


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


def _safe_json(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def _classify_health(state_value: bool | None) -> str:
    """Map a boolean health indicator to a clear string state."""
    if state_value is True:
        return "healthy"
    if state_value is False:
        return "unhealthy"
    return "unknown"


# ══════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════


@app.get("/health")
async def health():
    db_path = _get_db_path()
    db_info = _get_db_info(db_path)
    hb_age = _get_heartbeat_age(db_path)
    data = _read_db(db_path)

    session_info = {}
    if data and data.get("session"):
        s = data["session"]
        session_info = {
            "session_id": s.get("session_id", ""),
            "status": s.get("status", "UNKNOWN"),
            "started_at": s.get("started_at", ""),
            "commit_sha": s.get("commit_sha", ""),
        }

    engine_running = db_info["exists"] and (hb_age is not None and hb_age < 120)

    return {
        "status": "healthy" if db_info["exists"] else "no_data",
        "uptime_seconds": _uptime_seconds(),
        "started_at": START_TIME.isoformat(),
        "engine_running": engine_running,
        "engine_state": "running" if engine_running else
                        ("stale" if hb_age is not None else "stopped"),
        "live_trading": False,
        "mode": "PAPER",
        "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
        "session": session_info,
        "database": {
            "path": db_info["configured_path"],
            "filename": db_info["filename"],
            "exists": db_info["exists"],
            "size_mb": db_info["size_mb"],
            "last_modified": db_info["last_modified"],
        },
    }


@app.get("/metrics")
async def metrics():
    db_path = _get_db_path()
    data = _read_db(db_path)

    if data is None:
        return {
            "events": 0, "consumed": 0, "signals": 0, "opportunities": 0,
            "risk_assessments": 0, "orders": 0, "fills": 0,
            "db_available": False, "db_path": _get_db_info(db_path)["configured_path"],
        }

    if "db_error" in data:
        return {
            "events": 0, "consumed": 0, "signals": 0, "opportunities": 0,
            "risk_assessments": 0, "orders": 0, "fills": 0,
            "db_error": data["db_error"],
        }

    acct = data.get("account", {})
    return {
        "events": 0,
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
        "db_available": True,
        "db_path": _get_db_info(db_path)["configured_path"],
    }


@app.get("/positions")
async def positions():
    db_path = _get_db_path()
    data = _read_db(db_path)
    if data is None or "db_error" in data:
        return {"positions": [], "count": 0, "db_available": False}

    positions_list = data.get("positions", [])
    trails = {t["position_id"]: t for t in data.get("trails", [])}

    enriched = []
    for p in positions_list:
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

    return {"positions": enriched, "count": len(enriched), "db_available": True}


@app.get("/trades")
async def trades(limit: int = 500):
    db_path = _get_db_path()
    data = _read_db(db_path)
    if data is None or "db_error" in data:
        return {"trades": [], "count": 0, "db_available": False}

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
        "db_available": True,
    }


@app.get("/risk")
async def risk():
    db_path = _get_db_path()
    data = _read_db(db_path)
    if data is None or "db_error" in data:
        return {"cash": 0, "equity": 0, "exposure": 0, "db_available": False}

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
        "db_available": True,
    }


@app.get("/system")
async def system():
    db_path = _get_db_path()
    db_info = _get_db_info(db_path)
    data = _read_db(db_path)
    hb_age = _get_heartbeat_age(db_path)
    rss_mb = _get_memory_mb()

    lease_info = {}
    session_info = {}
    if data and "db_error" not in data:
        lease_info = data.get("lease", {})
        s = data.get("session", {})
        if s:
            session_info = {
                "session_id": s.get("session_id", ""),
                "status": s.get("status", "UNKNOWN"),
                "started_at": s.get("started_at", ""),
                "commit_sha": s.get("commit_sha", ""),
            }

    return {
        "memory": {
            "rss_mb": round(rss_mb, 2) if rss_mb is not None else None,
            "available": rss_mb is not None,
        },
        "database": {
            "configured_path": db_info["configured_path"],
            "filename": db_info["filename"],
            "exists": db_info["exists"],
            "size_bytes": db_info["size_bytes"],
            "size_mb": db_info["size_mb"],
            "last_modified": db_info["last_modified"],
        },
        "session": session_info,
        "heartbeat": {
            "file_exists": hb_age is not None,
            "age_seconds": round(hb_age, 1) if hb_age is not None else None,
            "healthy": hb_age is not None and hb_age < 120,
            "state": _classify_health(hb_age is not None and hb_age < 120),
        },
        "lease": {
            "owner_id": lease_info.get("owner_id", ""),
            "acquired_at": lease_info.get("acquired_at", ""),
            "heartbeat_at": lease_info.get("heartbeat_at", ""),
            "expires_at": lease_info.get("expires_at", ""),
            "active": bool(lease_info),
            "state": "active" if lease_info and lease_info.get("expires_at", "") > datetime.now(UTC).isoformat() else "inactive",
        },
        "feed_health": {
            "status": "monitoring",
            "note": "Feed health managed by orchestrator runtime",
        },
        "uptime_seconds": _uptime_seconds(),
        "platform": os.name,
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
