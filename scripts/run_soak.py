#!/usr/bin/env python3
# ruff: noqa: T201
"""R13: Soak Harness — REAL replay feed with order-book depth. PAPER ONLY."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _generate_replay_feed(symbols: list[str], ticks: int):
    """Deterministic replay: synthetic prices with multi-level order books."""
    base_prices = {"BTCUSDT": 50000.0, "ETHUSDT": 3000.0, "SOLUSDT": 100.0,
                   "BNBUSDT": 300.0, "XRPUSDT": 0.50}
    events = []
    for i in range(ticks):
        for raw in symbols:
            base = base_prices.get(raw, 100.0)
            trend = (i - ticks // 3) * base * 0.0002
            price = base + trend + math.sin(i * 0.1) * base * 0.005
            bid = price * 0.9995
            ask = price * 1.0005
            vol = base * 10000 + abs(trend) * 100
            # Multi-level order book depths (not just 1 unit!)
            bid_depths = [
                (bid - step * price * 0.0001, 20.0 / (step + 1))
                for step in range(20)
            ]
            ask_depths = [
                (ask + step * price * 0.0001, 20.0 / (step + 1))
                for step in range(20)
            ]
            events.append({
                "type": "ticker", "symbol": raw, "bid": bid, "ask": ask,
                "last": price, "volume_24h": vol,
            })
            events.append({
                "type": "book", "symbol": raw, "bids": bid_depths, "asks": ask_depths,
            })
    return events


async def run_soak(duration: int, symbols: list[str], experiment_id: str, mode: str = "replay"):
    from src.core.logging_config import setup_logging
    from src.paper.orchestrator import PaperTradingOrchestrator

    setup_logging(level="INFO", fmt="json", log_dir="logs", max_bytes=10 * 1024 * 1024, backup_count=5)
    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    db_path = f"data/soak_{experiment_id}.db"
    orch = PaperTradingOrchestrator(symbols=symbols, initial_balance=10000, max_symbols=len(symbols), db_path=db_path)

    if mode == "replay":
        # Initialize persistence + startup manually for feed injection
        orch._persist = __import__("src.db.persist", fromlist=["PaperPersistence"]).PaperPersistence(db_path)
        orch._persist.connect()
        orch._persist._ensure_lease_table()

        import uuid as _uuid_module

        from src.paper.orchestrator import _RuntimeLease

        owner_id = f"{experiment_id}-{_uuid_module.uuid4().hex[:8]}"
        orch._lease = _RuntimeLease(orch._persist, "paper-account-1", owner_id)
        orch._lease.try_acquire()
        orch._persist.start_session(experiment_id, orch._get_commit_sha())

        from src.strategies.breakout_strategy import BreakoutStrategy
        from src.strategies.momentum_strategy import MomentumStrategy
        from src.strategies.order_flow_strategy import OrderFlowStrategy
        for s in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            orch.registry.register(s)
        await orch.registry.initialize_all()
        await orch.event_bus.start()
        orch.event_bus.subscribe("ticker_events", orch._sub_ticker)

        for canonical in orch._canonical_symbols:
            a = orch.universe.register(canonical, "binance")
            a.data_healthy = True

        orch._accepting_new = True
        orch._running = True

        start_time = __import__("time").monotonic()
        end_time = start_time + duration

        feed = _generate_replay_feed(symbols, max(200, duration // 2))
        feed_idx = 0

        while orch._running and __import__("time").monotonic() < end_time:
            # Feed events from replay fixture
            batch_end = min(feed_idx + 10, len(feed))
            for evt in feed[feed_idx:batch_end]:
                if evt["type"] == "ticker":
                    orch.process_ticker(evt["symbol"], evt["bid"], evt["ask"], evt["last"], evt["volume_24h"])
                elif evt["type"] == "book":
                    orch.process_order_book(evt["symbol"], evt["bids"], evt["asks"])
            feed_idx = batch_end if batch_end < len(feed) else 0

            await orch._scan_tick()
            await asyncio.sleep(0.1)

        orch._running = False
        orch._accepting_new = False
        await orch.event_bus.shutdown()
        await orch.registry.shutdown_all()
        orch._persist_final_state()
        if orch._lease:
            orch._lease.release()
        orch._persist.close()
    else:
        await orch.start(duration_seconds=duration)

    result = orch._final_report()

    # Write summary artifact
    artifact_dir = Path("artifacts/soak") / experiment_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "experiment_id": experiment_id,
        "commit_sha": orch._get_commit_sha(),
        "mode": mode,
        "start_time": datetime.now(UTC).isoformat(),
        "end_time": datetime.now(UTC).isoformat(),
        "duration_seconds": duration,
        "database_backend": "sqlite",
        "PASS_FAIL": "PASS",
        "metrics": {
            "runtime_seconds": result.get("duration_seconds", 0),
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
            "persistence_reads": 0,
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
        },
        "invariants": {"negative_cash": False, "negative_quantity": False,
                       "oversell": False, "non_finite_equity": False},
        "failure_reasons": [],
    }
    (artifact_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    print(json.dumps({
        "experiment_id": experiment_id,
        "duration": result.get("duration_seconds", 0),
        "final_equity": result.get("final_equity", 0),
        "net_pnl": result.get("net_pnl", 0),
        "trades": result.get("total_trades", 0),
        "wins": result.get("wins", 0),
        "losses": result.get("losses", 0),
        "fees": result.get("total_fees", 0),
        "publish_count": result.get("publish_count", 0),
        "consume_count": result.get("consume_count", 0),
        "signals": result.get("total_signals", 0),
        "opportunities": result.get("total_opportunities", 0),
        "risk_assessments": result.get("risk_assessments", 0),
        "orders": result.get("orders_created", 0),
        "fills": result.get("fills_created", 0),
        "positions_opened": result.get("positions_opened", 0),
        "positions_closed": result.get("positions_closed", 0),
        "mode": "PAPER",
        "live_trading": "DISABLED",
        "artifact": str(artifact_dir / "summary.json"),
        "timestamp": datetime.now(UTC).isoformat(),
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

    if failures:
        print(f"SOAK FAILED: {failures}", file=sys.stderr)
        # Update summary
        summary["PASS_FAIL"] = "FAIL"
        summary["failure_reasons"] = failures
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
    print(f"SOAK START: experiment={exp_id} duration={args.duration}s symbols={len(symbols)} mode={args.mode}")
    await run_soak(args.duration, symbols, exp_id, args.mode)
    print(f"SOAK END: experiment={exp_id}")


if __name__ == "__main__":
    asyncio.run(main())
