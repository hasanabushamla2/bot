#!/usr/bin/env python3
# ruff: noqa: T201
"""R10: Soak Harness — REAL PaperTradingOrchestrator. PAPER ONLY."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def run_soak(duration: int, symbols: list[str], experiment_id: str, mode: str = "replay"):
    from src.paper.orchestrator import PaperTradingOrchestrator

    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    orch = PaperTradingOrchestrator(
        symbols=symbols, initial_balance=10000, max_symbols=len(symbols)
    )
    result = await orch.start(duration_seconds=duration)

    print(json.dumps({
        "experiment_id": experiment_id,
        "duration": orch._final_report().get("duration_seconds", 0),
        "final_equity": result.get("final_equity", 0),
        "net_pnl": result.get("net_pnl", 0),
        "trades": result.get("total_trades", 0),
        "wins": result.get("wins", 0),
        "losses": result.get("losses", 0),
        "fees": result.get("total_fees", 0),
        "publish_count": result.get("publish_count", 0),
        "consume_count": result.get("consume_count", 0),
        "mode": "PAPER",
        "live_trading": "DISABLED",
        "timestamp": datetime.now(UTC).isoformat(),
    }, indent=2))


async def main():
    parser = argparse.ArgumentParser(description="Soak Harness — PAPER ONLY")
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT")
    parser.add_argument("--experiment-id", type=str, default=None)
    parser.add_argument("--mode", type=str, default="replay", choices=["replay", "live-public"])
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    exp_id = args.experiment_id or f"soak-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    print(
        f"SOAK START: experiment={exp_id} duration={args.duration}s "
        f"symbols={len(symbols)} mode={args.mode}"
    )
    await run_soak(args.duration, symbols, exp_id, args.mode)
    print(f"SOAK END: experiment={exp_id}")


if __name__ == "__main__":
    asyncio.run(main())
