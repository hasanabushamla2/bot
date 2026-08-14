#!/usr/bin/env python3
# ruff: noqa: T201
"""LIVE PAPER TRADING — feed-budget-aware KuCoin liquid-universe scanner.

Connects to KuCoin, ranks current liquid USDT pairs, and feeds a bounded fresh
book universe through the existing orchestrator. ALL orders are PAPER ONLY.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time as time_module
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

SYMBOL_STATS: dict[str, dict] = {}

# Feed-budget defaults: broaden the liquid universe while scaling book batches
# so a larger selection is refreshed predictably. This is not a desired
# trades/hour setting.
DEFAULT_MAX_SYMBOLS = 300
DEFAULT_MIN_VOLUME_USD = 100_000.0
DEFAULT_MAX_SPREAD_BPS = 35.0
DEFAULT_BOOK_REFRESH_BUDGET_SECONDS = 30.0
DEFAULT_BOOK_BATCH_INTERVAL_SECONDS = 2.0


def _kucoin_safety_proof() -> None:
    from src.adapters.crypto.kucoin import KuCoinPublicAdapter
    a = KuCoinPublicAdapter()
    methods = {m for m in dir(a) if not m.startswith("__")}
    dangerous = {"place_order", "cancel_order", "submit_order", "create_order",
                 "withdraw", "transfer", "trade"}
    found = methods & dangerous
    if found:
        print(f"SAFETY FAIL: {found}")
        sys.exit(1)
    print(f"  KuCoin safety: {len(methods)} public methods, 0 dangerous")


async def _universe_feed(
    orch: Any, adapter: Any, symbols: list[str], stop_event: asyncio.Event
) -> None:
    """Batch-poll ALL symbols via KuCoin allTickers + per-symbol order books.

    Uses KuCoin's /api/v1/market/allTickers for efficient mass polling
    (~1 request for all prices), then fetches order books in batches.
    """
    # Scale the batch so every selected book is revisited inside the
    # freshness budget instead of making most of a broad universe stale.
    book_interval = DEFAULT_BOOK_BATCH_INTERVAL_SECONDS
    batches_per_budget = max(1, int(DEFAULT_BOOK_REFRESH_BUDGET_SECONDS / book_interval))
    batch_size = max(25, (len(symbols) + batches_per_budget - 1) // batches_per_budget)

    sym_list = list(symbols)
    book_idx = 0
    last_book_fetch = 0.0

    for raw in symbols:
        SYMBOL_STATS.setdefault(raw, {"ticker": 0, "book": 0, "last_price": 0,
                                       "last_ticker_ts": None, "last_book_ts": None})

    while not stop_event.is_set():
        now_ts = time_module.monotonic()

        # 1. Fetch ALL tickers in one request
        try:
            all_t = await adapter.get_all_tickers()
        except Exception:
            all_t = None

        if all_t:
            for raw in sym_list:
                if stop_event.is_set():
                    break
                t = all_t.get(raw)
                if t and t.get("last", 0) > 0:
                    orch.process_ticker(raw, t.get("bid", t["last"] * 0.999),
                                        t.get("ask", t["last"] * 1.001),
                                        t["last"], volume_24h=t.get("volume_24h_usd", 1_000_000))
                    s = SYMBOL_STATS[raw]
                    s["ticker"] += 1
                    s["last_price"] = t["last"]
                    s["last_ticker_ts"] = datetime.now(UTC)

        # 2. Fetch order books in batches
        if now_ts - last_book_fetch >= book_interval:
            batch = sym_list[book_idx:book_idx + batch_size]
            for raw in batch:
                if stop_event.is_set():
                    break
                try:
                    ob = await adapter.get_order_book(raw)
                    if ob and ob.get("bids") and ob.get("asks"):
                        orch.process_order_book(raw, ob["bids"], ob["asks"])
                        s = SYMBOL_STATS[raw]
                        s["book"] += 1
                        s["last_book_ts"] = datetime.now(UTC)
                except Exception:
                    pass
                await asyncio.sleep(0.02)
            book_idx = (book_idx + batch_size) % len(sym_list)
            last_book_fetch = now_ts

        await asyncio.sleep(2.0)  # poll cycle


async def run_live_paper(
    duration: int,
    symbols: list[str],
    experiment_id: str,
    activity_test: bool = False,
    was_auto_detected: bool = True,
    db_path: str | None = None,
    fresh_db: bool = False,
    initial_balance: float = 10_000.0,
    aggressive_paper: bool = True,
) -> int:
    from src.adapters.crypto.kucoin import KuCoinPublicAdapter
    from src.core.logging_config import setup_logging
    from src.paper.orchestrator import PaperTradingOrchestrator

    setup_logging(level="WARNING", fmt="json", log_dir="logs",
                  max_bytes=10 * 1024 * 1024, backup_count=5)
    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    db_path = db_path or f"data/{experiment_id}.db"
    if fresh_db:
        for candidate in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
            path = Path(candidate)
            if path.exists():
                path.unlink()
        print("LIVE-PAPER: fresh database requested; prior file removed")
    print(f"LIVE-PAPER: DB={db_path}")
    source_label = "auto-detected" if was_auto_detected else "specified"
    print(f"LIVE-PAPER: Symbols={len(symbols)} ({source_label})")
    print(f"LIVE-PAPER: Duration={duration}s")
    if activity_test:
        print("LIVE-PAPER: MODE = PAPER ACTIVITY TEST")

    _kucoin_safety_proof()

    adapter = KuCoinPublicAdapter()
    await adapter.connect()
    if not await adapter.health_check():
        print("ERROR: KuCoin API unreachable.")
        await adapter.disconnect()
        return 1

    print(f"  Connected to KuCoin — monitoring {len(symbols)} symbols")

    orch = PaperTradingOrchestrator(
        symbols=symbols, initial_balance=initial_balance,
        max_symbols=len(symbols), db_path=db_path,
        activity_test=activity_test, aggressive_paper=aggressive_paper,
    )

    wall_start = datetime.now(UTC)
    stop_event = asyncio.Event()
    feed_task = asyncio.create_task(_universe_feed(orch, adapter, symbols, stop_event))
    orch_task = asyncio.create_task(orch.start(duration_seconds=duration))

    # Report every 60 seconds
    report_interval = 60
    for _ in range(max(1, duration // report_interval)):
        await asyncio.sleep(report_interval)
        elapsed = (datetime.now(UTC) - wall_start).total_seconds()
        ticker_total = sum(s.get("ticker", 0) for s in SYMBOL_STATS.values())
        book_total = sum(s.get("book", 0) for s in SYMBOL_STATS.values())
        active = sum(1 for s in SYMBOL_STATS.values() if s.get("ticker", 0) > 0)
        print(f"  [{elapsed:.0f}s] events={orch.publish_count} consume={orch.consume_count} "
              f"ticker={ticker_total} book={book_total} active_symbols={active}/{len(symbols)} "
              f"sigs={orch._total_signals} opps={orch._total_opportunities} "
              f"ords={orch._orders_created} fills={orch._fills_created} "
              f"pos={orch._positions_opened_total}op/{orch._positions_closed}cl "
              f"eval={orch._strategy_evaluations}")

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

    # ── Results ──
    active = sum(1 for s in SYMBOL_STATS.values() if s.get("ticker", 0) > 0)
    print()
    print("=" * 70)
    print("  R26 — DYNAMIC UNIVERSE RESULTS")
    print("=" * 70)
    print(f"  Universe size:    {len(symbols)} symbols")
    print(f"  Active (ticker):  {active}")
    print(f"  Wall time:        {wall_secs:.1f}s")
    print(f"  Events:           {result.get('publish_count', 0)}")
    print(f"  Signals:          {result.get('total_signals', 0)}")
    print(f"  Opportunities:    {result.get('total_opportunities', 0)}")
    print(f"  Risk assessed:    {result.get('risk_assessments', 0)}")
    print(f"  Orders:           {result.get('orders_created', 0)}")
    print(f"  Fills:            {result.get('fills_created', 0)}")
    print(f"  Positions:        {result.get('positions_opened', 0)}op/{result.get('positions_closed', 0)}cl")
    print(f"  Persist writes:   {result.get('persistence_writes', 0)}")
    print(f"  Persist errors:   {result.get('persistence_errors', 0)}")
    print(f"  Exceptions:       {result.get('exceptions', 0)}")
    print()
    print("  REAL ORDERS: 0 | LIVE TRADING: DISABLED")
    print("=" * 70)

    # Run Database Analysis
    from scripts.analyze_paper_run import analyze_database, print_report
    if Path(db_path).exists():
        audit_res = analyze_database(db_path)
        print_report(audit_res)

    failures = []
    if orch._stale_feed_violation:
        failures.append("STALE_FEED")
    if orch._fatal_error:
        failures.append("FATAL_ERROR")
    if result.get("persistence_errors", 0) > 0:
        failures.append("PERSISTENCE_ERRORS")
    if result.get("status") == "FAILED":
        failures.append("ORCHESTRATOR_FAILED")
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("ALL CHECKS PASSED.")
    return 0


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live Paper — Dynamic KuCoin Universe")
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--symbols", type=str, default="")
    parser.add_argument("--experiment-id", type=str, default="r26_universe")
    parser.add_argument("--db-path", type=str, default="")
    parser.add_argument(
        "--fresh-db",
        action="store_true",
        help="Delete only the selected paper DB (plus WAL/SHM) before starting",
    )
    parser.add_argument("--activity-test", action="store_true")
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument(
        "--profile", choices=("safe", "aggressive-paper"), default="aggressive-paper",
        help="Paper-only profile that can deploy 100%% across diversified positions.",
    )
    parser.add_argument(
        "--max-symbols",
        type=int,
        default=DEFAULT_MAX_SYMBOLS,
        help="Maximum live-liquid symbols in the automatic universe; sized for book freshness.",
    )
    parser.add_argument(
        "--min-volume-usd",
        type=float,
        default=DEFAULT_MIN_VOLUME_USD,
        help="Minimum current 24h quote volume for automatic-universe candidates.",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=DEFAULT_MAX_SPREAD_BPS,
        help="Maximum current top-of-book spread for automatic-universe candidates.",
    )
    args = parser.parse_args()
    if args.max_symbols < 0:
        parser.error("--max-symbols must be zero (all eligible) or positive")
    if args.initial_balance <= 0:
        parser.error("--initial-balance must be positive")
    if args.min_volume_usd < 0:
        parser.error("--min-volume-usd must be non-negative")
    if args.max_spread_bps <= 0:
        parser.error("--max-spread-bps must be positive")

    if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() in ("true", "1", "yes"):
        print("SAFETY GATE: LIVE_TRADING_ENABLED=true — REFUSING TO START")
        sys.exit(1)
    print("SAFETY GATE: PASS")

    from src.adapters.crypto.kucoin import KuCoinPublicAdapter

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
        print(f"Using manual symbols: {len(symbols)}")
    else:
        # Dynamic universe: fetch all KuCoin USDT pairs
        print("Fetching KuCoin universe...")
        adapter = KuCoinPublicAdapter()
        await adapter.connect()
        if not await adapter.health_check():
            print("ERROR: KuCoin unreachable")
            await adapter.disconnect()
            return

        all_syms = await adapter.get_all_symbols()
        if not all_syms:
            print("ERROR: No symbols returned")
            await adapter.disconnect()
            return

        all_tickers = await adapter.get_all_tickers()
        if not all_tickers:
            print("ERROR: No current ticker universe returned")
            await adapter.disconnect()
            return
        symbols = adapter.rank_liquid_usdt_pairs(
            all_syms,
            all_tickers,
            min_volume_usd=args.min_volume_usd,
            max_symbols=args.max_symbols,
            max_spread_bps=args.max_spread_bps,
        )
        await adapter.disconnect()
        if not symbols:
            print("ERROR: No symbols passed current volume/spread liquidity selection")
            return
        print(
            f"Dynamic universe: {len(all_syms)} metadata → {len(symbols)} current-liquid USDT pairs "
            f"(cap={'all eligible' if args.max_symbols == 0 else args.max_symbols}, "
            f"min volume=${args.min_volume_usd:,.0f}, "
            f"max spread={args.max_spread_bps:.1f}bps)"
        )

    exp_id = args.experiment_id

    print("=" * 70)
    print("  QUANT ENGINE — DYNAMIC KUCOIN UNIVERSE")
    if args.activity_test:
        print("  *** PAPER ACTIVITY TEST MODE ***")
    print(f"  Symbols:       {len(symbols)} ({'specified' if args.symbols else 'auto-detected'})")
    print(f"  Duration:      {args.duration}s")
    print(f"  Experiment:    {exp_id}")
    print(f"  Profile:       {args.profile}")
    print(f"  Paper balance: ${args.initial_balance:,.2f}")
    print("  REAL ORDERS:   DISABLED")
    print("=" * 70)
    print()

    exit_code = await run_live_paper(
        args.duration,
        symbols,
        exp_id,
        was_auto_detected=not bool(args.symbols),
        activity_test=args.activity_test,
        db_path=args.db_path or None,
        fresh_db=args.fresh_db,
        initial_balance=args.initial_balance,
        aggressive_paper=args.profile == "aggressive-paper",
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
