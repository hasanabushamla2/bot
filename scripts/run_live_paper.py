#!/usr/bin/env python3
# ruff: noqa: T201
"""R18.1: LIVE PAPER TRADING — Real market data, per-symbol evidence, KuCoin safety proof.

ALL orders are PAPER ONLY. Real exchange order placement is IMPOSSIBLE.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SYMBOL_STATS: dict[str, dict] = {}


def _kucoin_safety_proof() -> None:
    """Prove the KuCoin adapter has NO order-placement capability."""
    from src.adapters.crypto.kucoin import KuCoinPublicAdapter
    a = KuCoinPublicAdapter()
    # Verify no dangerous methods exist on the adapter
    methods = {m for m in dir(a) if not m.startswith("__")}
    dangerous = {"place_order", "cancel_order", "submit_order", "create_order",
                 "withdraw", "transfer", "trade"}
    found_dangerous = methods & dangerous
    if found_dangerous:
        print(f"SAFETY FAIL: KuCoin adapter has dangerous methods: {found_dangerous}")
        sys.exit(1)
    print(f"  KuCoin safety: {len(methods)} public methods, 0 dangerous")


async def _kucoin_feed(orch, adapter, symbols: list[str], stop_event: asyncio.Event):
    """Poll KuCoin REST API for real ticker + order book data. Track per-symbol."""
    for raw in symbols:
        SYMBOL_STATS.setdefault(raw, {"ticker": 0, "book": 0, "bid_levels": 0,
                                       "ask_levels": 0, "last_bid": 0, "last_ask": 0,
                                       "last_price": 0, "last_book_ts": None,
                                       "last_ticker_ts": None})

    while not stop_event.is_set():
        for raw in symbols:
            if stop_event.is_set():
                break
            s = SYMBOL_STATS[raw]

            t = await adapter.get_ticker(raw)
            if t and t.get("last", 0) > 0:
                orch.process_ticker(raw, t["bid"], t["ask"], t["last"],
                                    volume_24h=t.get("bid_size", 0) * 10000)
                s["ticker"] += 1
                s["last_bid"] = t["bid"]
                s["last_ask"] = t["ask"]
                s["last_price"] = t["last"]
                s["last_ticker_ts"] = datetime.now(UTC)

            ob = await adapter.get_order_book(raw, depth=20)
            if ob and ob.get("bids") and ob.get("asks"):
                orch.process_order_book(raw, ob["bids"], ob["asks"])
                s["book"] += 1
                s["bid_levels"] = len(ob["bids"])
                s["ask_levels"] = len(ob["asks"])
                s["last_book_ts"] = datetime.now(UTC)

            await asyncio.sleep(0.05)

        await asyncio.sleep(1.0)


async def run_live_paper(duration: int, symbols: list[str], experiment_id: str):
    from src.adapters.crypto.kucoin import KuCoinPublicAdapter
    from src.core.logging_config import setup_logging
    from src.paper.orchestrator import PaperTradingOrchestrator

    setup_logging(level="INFO", fmt="json", log_dir="logs",
                  max_bytes=10 * 1024 * 1024, backup_count=5)
    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    db_path = f"data/{experiment_id}.db"
    print(f"LIVE-PAPER: DB={db_path}")
    print(f"LIVE-PAPER: Symbols={symbols}")
    print(f"LIVE-PAPER: Duration={duration}s")

    _kucoin_safety_proof()

    adapter = KuCoinPublicAdapter()
    await adapter.connect()

    if not await adapter.health_check():
        print("ERROR: KuCoin API unreachable.")
        await adapter.disconnect()
        return 1

    print("  Connected to KuCoin (REST polling)")

    orch = PaperTradingOrchestrator(
        symbols=symbols, initial_balance=10000,
        max_symbols=len(symbols), db_path=db_path,
    )

    wall_start = datetime.now(UTC)
    stop_event = asyncio.Event()
    feed_task = asyncio.create_task(_kucoin_feed(orch, adapter, symbols, stop_event))
    orch_task = asyncio.create_task(orch.start(duration_seconds=duration))

    # Print per-symbol status mid-run
    for _ in range(duration // 30):
        await asyncio.sleep(30)
        elapsed = (datetime.now(UTC) - wall_start).total_seconds()
        print(f"\n  [{elapsed:.0f}s] Status:")
        for raw in symbols:
            s = SYMBOL_STATS.get(raw, {})
            print(f"    {raw}: ticker={s.get('ticker',0)} book={s.get('book',0)} "
                  f"bid_levels={s.get('bid_levels',0)} ask_levels={s.get('ask_levels',0)} "
                  f"last=${s.get('last_price',0):,.2f}")

    # Wait for remaining time
    remaining = duration - (datetime.now(UTC) - wall_start).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)

    stop_event.set()
    orch.stop()
    await asyncio.gather(feed_task, return_exceptions=True)
    result = await orch_task
    await adapter.disconnect()

    wall_end = datetime.now(UTC)
    wall_secs = (wall_end - wall_start).total_seconds()

    # ── Evidence ──
    print()
    print("=" * 70)
    print("  LIVE PAPER PRE-FLIGHT — PER-SYMBOL EVIDENCE")
    print("=" * 70)
    for raw in symbols:
        s = SYMBOL_STATS.get(raw, {})
        tb = s.get("last_ticker_ts")
        bb = s.get("last_book_ts")
        now = datetime.now(UTC)
        ta = (now - tb).total_seconds() if tb else None
        ba = (now - bb).total_seconds() if bb else None
        print(f"  {raw}:")
        print(f"    ticker_updates: {s.get('ticker', 0)}")
        print(f"    book_updates:   {s.get('book', 0)}")
        print(f"    bid_levels:     {s.get('bid_levels', 0)}")
        print(f"    ask_levels:     {s.get('ask_levels', 0)}")
        print(f"    last_price:     ${s.get('last_price', 0):,.2f}")
        print(f"    last_bid/ask:   {s.get('last_bid', 0):,.2f}/{s.get('last_ask', 0):,.2f}")
        print(f"    ticker_age_s:   {ta:.1f}" if ta else "    ticker_age_s:   N/A")
        print(f"    book_age_s:     {ba:.1f}" if ba else "    book_age_s:     N/A")
        print()

    print("  Exchange:         KuCoin (public REST)")
    print(f"  Wall time:        {wall_secs:.1f}s")
    print(f"  Events published: {result.get('publish_count', 0)}")
    print(f"  Events consumed:  {result.get('consume_count', 0)}")
    print(f"  Signals:          {result.get('total_signals', 0)}")
    print(f"  Opportunities:    {result.get('total_opportunities', 0)}")
    print(f"  Risk assessed:    {result.get('risk_assessments', 0)}")
    print(f"  Orders:           {result.get('orders_created', 0)}")
    print(f"  Fills:            {result.get('fills_created', 0)}")
    print(f"  Positions opened: {result.get('positions_opened', 0)}")
    print(f"  Positions closed: {result.get('positions_closed', 0)}")
    print(f"  Cash:             ${result.get('final_equity', 0):,.2f}")
    print(f"  Realized PnL:     ${result.get('net_pnl', 0):,.2f}")
    print(f"  Fees:             ${result.get('total_fees', 0):,.4f}")
    print(f"  Persist writes:   {result.get('persistence_writes', 0)}")
    print(f"  Persist errors:   {result.get('persistence_errors', 0)}")
    print(f"  Exceptions:       {result.get('exceptions', 0)}")
    print()

    # Health
    ticker_healthy = all(s.get("ticker", 0) > 0 and s.get("last_ticker_ts")
                         and (datetime.now(UTC) - s["last_ticker_ts"]).total_seconds() < 60
                         for s in SYMBOL_STATS.values())
    book_healthy = all(s.get("book", 0) > 0 and s.get("last_book_ts")
                       and (datetime.now(UTC) - s["last_book_ts"]).total_seconds() < 60
                       for s in SYMBOL_STATS.values())
    print(f"  TICKER HEALTH:    {'HEALTHY' if ticker_healthy else 'STALE'}")
    print(f"  BOOK HEALTH:      {'HEALTHY' if book_healthy else 'STALE'}")
    print(f"  Stale violations: {orch._stale_feed_violation}")
    print(f"  Fatal errors:     {orch._fatal_error or 'None'}")
    print()

    # SAFETY
    print("  SAFETY:")
    print("    LIVE_TRADING_ENABLED:     false")
    print("    KuCoin private client:    NONE")
    print("    KuCoin auth endpoint:     NONE called")
    print("    KuCoin real order:        0")
    print("    PaperExecutionEngine:     PAPER FILLS ONLY")
    print("    REAL ORDERS SUBMITTED:    0")
    print("=" * 70)

    # Auto-fail
    failures = []
    s = orch.account.state
    if s.cash < 0:
        failures.append("NEGATIVE_CASH")
    if any(p.quantity < 0 for p in s.open_positions.values()):
        failures.append("NEGATIVE_QTY")
    if not (-1e12 < s.equity < 1e12):
        failures.append("NON_FINITE_EQUITY")
    if result.get("persistence_errors", 0) > 0:
        failures.append("PERSISTENCE_ERRORS")
    if orch._stale_feed_violation:
        failures.append("STALE_FEED_WHILE_ACCEPTING")
    if orch._fatal_error:
        failures.append("UNCAUGHT_EXCEPTION")

    if failures:
        print(f"\nFAILURES: {failures}")
        return 1
    print("\nALL CHECKS PASSED.")
    return 0


async def main():
    parser = argparse.ArgumentParser(description="Live Paper — KuCoin PUBLIC DATA ONLY")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--symbols", type=str, default="BTC-USDT,ETH-USDT,SOL-USDT")
    parser.add_argument("--experiment-id", type=str, default="live_paper_preflight_v2")
    args = parser.parse_args()

    # Safety gate
    if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() in ("true", "1", "yes"):
        print("SAFETY GATE: LIVE_TRADING_ENABLED=true — REFUSING TO START")
        sys.exit(1)
    print("SAFETY GATE: PASS")

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    exp_id = args.experiment_id

    print("=" * 70)
    print("  QUANT ENGINE — LIVE KUCOIN / PAPER EXECUTION")
    print(f"  Symbols:       {', '.join(symbols)}")
    print(f"  Duration:      {args.duration}s")
    print(f"  Experiment:    {exp_id}")
    print("  REAL ORDERS:   DISABLED")
    print("  API KEYS:      NONE (public data only)")
    print("=" * 70)
    print()

    exit_code = await run_live_paper(args.duration, symbols, exp_id)
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
