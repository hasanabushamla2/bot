#!/usr/bin/env python3
# ruff: noqa: T201
"""R14: Soak Harness — real orchestrator.start() with background feed. PAPER ONLY."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time as time_module
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _generate_replay_feed(symbols: list[str], ticks: int):
    base_prices = {"BTCUSDT": 50000.0, "ETHUSDT": 3000.0, "SOLUSDT": 100.0}
    events = []
    for i in range(ticks):
        for raw in symbols:
            base = base_prices.get(raw, 100.0)
            trend = (i - ticks // 3) * base * 0.0002
            price = base + trend + math.sin(i * 0.1) * base * 0.005
            bid = price * 0.9995
            ask = price * 1.0005
            vol = base * 10000 + abs(trend) * 100
            bid_depths = [(bid - s * price * 0.0001, 20.0 / (s + 1)) for s in range(20)]
            ask_depths = [(ask + s * price * 0.0001, 20.0 / (s + 1)) for s in range(20)]
            events.append({"type": "ticker", "symbol": raw, "bid": bid, "ask": ask,
                           "last": price, "volume_24h": vol})
            events.append({"type": "book", "symbol": raw, "bids": bid_depths, "asks": ask_depths})
    return events


async def _background_feed(orch, symbols: list[str], duration: int, stop_event: asyncio.Event):
    """Feed replay events through public ingestion while orchestrator.start() runs."""
    feed = _generate_replay_feed(symbols, max(200, duration // 2))
    feed_idx = 0
    t0 = time_module.monotonic()
    while time_module.monotonic() - t0 < duration and not stop_event.is_set():
        batch_end = min(feed_idx + 10, len(feed))
        for evt in feed[feed_idx:batch_end]:
            if evt["type"] == "ticker":
                orch.process_ticker(evt["symbol"], evt["bid"], evt["ask"],
                                    evt["last"], evt["volume_24h"])
            elif evt["type"] == "book":
                orch.process_order_book(evt["symbol"], evt["bids"], evt["asks"])
        feed_idx = batch_end if batch_end < len(feed) else 0
        await asyncio.sleep(0.05)


async def run_soak(duration: int, symbols: list[str], experiment_id: str, mode: str = "replay"):
    from src.core.logging_config import setup_logging
    from src.paper.orchestrator import PaperTradingOrchestrator

    setup_logging(level="INFO", fmt="json", log_dir="logs", max_bytes=10 * 1024 * 1024, backup_count=5)
    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    db_path = f"data/soak_{experiment_id}.db"
    orch = PaperTradingOrchestrator(
        symbols=symbols, initial_balance=10000, max_symbols=len(symbols), db_path=db_path
    )

    wall_start = datetime.now(UTC)
    if mode == "replay":
        # Kick off orchestrator.start() and background feed in parallel
        stop_event = asyncio.Event()
        feed_task = asyncio.create_task(_background_feed(orch, symbols, duration, stop_event))
        try:
            result = await orch.start(duration_seconds=duration)
        finally:
            stop_event.set()
            await feed_task
    else:
        result = await orch.start(duration_seconds=duration)
    wall_end = datetime.now(UTC)
    wall_secs = (wall_end - wall_start).total_seconds()

    # Write summary artifact
    artifact_dir = Path("artifacts/soak") / experiment_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    log_size = sum(
        os.path.getsize(os.path.join("logs", f))
        for f in os.listdir("logs") if f.startswith("engine.log")
    ) if os.path.isdir("logs") else 0

    from src.db.persist import PaperPersistence
    p_check = PaperPersistence(db_path)
    p_check.connect()
    db_trade_count = p_check.count_closed_trades()
    p_check.close()

    summary = {
        "experiment_id": experiment_id,
        "commit_sha": orch._get_commit_sha(),
        "mode": mode,
        "start_time": wall_start.isoformat(),
        "end_time": wall_end.isoformat(),
        "duration_seconds": duration,
        "wall_seconds": wall_secs,
        "database_backend": "sqlite",
        "PASS_FAIL": "PASS",
        "metrics": {
            "runtime_seconds": result.get("duration_seconds", 0),
            "wall_seconds": wall_secs,
            "market_events_received": result.get("publish_count", 0),
            "eventbus_publish_count": result.get("publish_count", 0),
            "eventbus_consume_count": result.get("consume_count", 0),
            "signals_generated": result.get("total_signals", 0),
            "opportunities_created": result.get("total_opportunities", 0),
            "risk_assessments": result.get("risk_assessments", 0),
            "risk_approved": result.get("risk_approved", 0),
            "risk_rejected": result.get("risk_rejected", 0),
            "orders_created": result.get("orders_created", 0),
            "fills_created": result.get("fills_created", 0),
            "partial_fills": result.get("partial_fills", 0),
            "positions_opened": result.get("positions_opened", 0),
            "positions_closed": result.get("positions_closed", 0),
            "trailing_exits": result.get("trailing_exits", 0),
            "hard_stop_exits": result.get("hard_stop_exits", 0),
            "persistence_writes": result.get("persistence_writes", 0),
            "persistence_errors": result.get("persistence_errors", 0),
            "lease_heartbeat_success": result.get("lease_heartbeat_success", 0),
            "lease_heartbeat_errors": result.get("lease_heartbeat_errors", 0),
            "exceptions": result.get("exceptions", 0),
            "cash": result.get("final_equity", 0),
            "equity": result.get("final_equity", 0),
            "realized_pnl": result.get("net_pnl", 0),
            "fees": result.get("total_fees", 0),
            "slippage": result.get("total_slippage", 0),
            "closed_trade_ram": result.get("closed_trade_ram", 0),
            "closed_trade_ram_limit": result.get("closed_trade_ram_limit", 0),
            "db_closed_trades": db_trade_count,
            "rss_start_mb": result.get("rss_start_mb", 0),
            "rss_peak_mb": result.get("rss_peak_mb", 0),
            "rss_end_mb": result.get("rss_end_mb", 0),
            "task_count_start": result.get("task_count_start", 0),
            "task_count_peak": result.get("task_count_peak", 0),
            "task_count_end": result.get("task_count_end", 0),
            "queue_depth_peak": result.get("queue_depth_peak", 0),
            "db_bytes_end": db_size,
            "log_bytes_end": log_size,
        },
        "invariants": {
            "cash_non_negative": True,
            "quantity_non_negative": True,
            "equity_finite": True,
        },
        "failure_reasons": [],
    }

    (artifact_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(json.dumps({
        "experiment_id": experiment_id,
        "wall_seconds": wall_secs,
        "runtime_seconds": result.get("duration_seconds", 0),
        "final_equity": result.get("final_equity", 0),
        "net_pnl": result.get("net_pnl", 0),
        "trades": result.get("total_trades", 0),
        "publish_count": result.get("publish_count", 0),
        "consume_count": result.get("consume_count", 0),
        "signals": result.get("total_signals", 0),
        "opportunities": result.get("total_opportunities", 0),
        "risk_assessments": result.get("risk_assessments", 0),
        "orders": result.get("orders_created", 0),
        "fills": result.get("fills_created", 0),
        "positions_opened": result.get("positions_opened", 0),
        "positions_closed": result.get("positions_closed", 0),
        "rss_start_mb": result.get("rss_start_mb", 0),
        "rss_peak_mb": result.get("rss_peak_mb", 0),
        "rss_end_mb": result.get("rss_end_mb", 0),
        "mode": "PAPER", "live_trading": "DISABLED",
        "artifact": str(artifact_dir / "summary.json"),
    }, indent=2, default=str))

    # Auto-fail checks
    s = orch.account.state
    failures = []
    if s.cash < 0:
        failures.append("NEGATIVE_CASH")
    if any(p.quantity < 0 for p in s.open_positions.values()):
        failures.append("NEGATIVE_QTY")
    if not (-1e12 < s.equity < 1e12):
        failures.append("NON_FINITE_EQUITY")
    if result.get("persistence_errors", 0) > 0:
        failures.append("PERSISTENCE_ERRORS")
    # R14: Stale feed check
    all_healthy = all(
        f.is_healthy for f in orch.feed_health.get_all()
    ) if orch.feed_health.get_all() else True
    if not all_healthy and orch._accepting_new:
        failures.append("STALE_FEED_WHILE_ACCEPTING")

    if failures:
        print(f"SOAK FAILED: {failures}", file=sys.stderr)
        summary["PASS_FAIL"] = "FAIL"
        summary["failure_reasons"] = failures
        for f in failures:
            if f == "NEGATIVE_CASH":
                summary["invariants"]["cash_non_negative"] = False
            elif f == "NEGATIVE_QTY":
                summary["invariants"]["quantity_non_negative"] = False
            elif f == "NON_FINITE_EQUITY":
                summary["invariants"]["equity_finite"] = False
        (artifact_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        sys.exit(1)


async def main():
    parser = argparse.ArgumentParser(description="Soak Harness — PAPER ONLY")
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--experiment-id", type=str, default=None)
    parser.add_argument("--mode", type=str, default="replay", choices=["replay", "live-public"])
    args = parser.parse_args()
    from src.core.logging_config import setup_logging
    setup_logging(level="INFO", fmt="json", log_dir="logs")
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    exp_id = args.experiment_id or f"soak-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    print(f"SOAK START: experiment={exp_id} duration={args.duration}s mode={args.mode}")
    await run_soak(args.duration, symbols, exp_id, args.mode)
    print(f"SOAK END: experiment={exp_id}")


if __name__ == "__main__":
    asyncio.run(main())
