#!/usr/bin/env python3
# ruff: noqa: T201
"""Global multi-exchange crypto paper runner.

Public market data only.  No exchange credentials and no order methods are used.
Each asset is assigned to one best venue at startup; total paper capital is
partitioned across venue orchestrators in proportion to selected symbols.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.adapters.crypto.ccxt_public import SUPPORTED_CCXT_EXCHANGES, CCXTPublicAdapter
from src.paper.orchestrator import PaperTradingOrchestrator
from src.scanner.multi_venue import MultiVenueUniverseBuilder


async def _venue_feed(
    orchestrator: PaperTradingOrchestrator,
    adapter: CCXTPublicAdapter,
    symbols: list[str],
    stop: asyncio.Event,
) -> None:
    book_index = 0
    batches_per_30_seconds = 15
    batch_size = max(10, (len(symbols) + batches_per_30_seconds - 1) // batches_per_30_seconds)
    while not stop.is_set():
        try:
            tickers = await adapter.get_all_tickers()
            for symbol in symbols:
                ticker = tickers.get(symbol)
                if ticker is None:
                    continue
                orchestrator.process_ticker(
                    symbol,
                    ticker.bid,
                    ticker.ask,
                    ticker.last,
                    volume_24h=ticker.quote_volume_24h,
                )
        except Exception:
            pass

        batch = symbols[book_index : book_index + batch_size]
        if not batch:
            book_index = 0
            batch = symbols[:batch_size]
        books = await asyncio.gather(
            *(adapter.get_order_book(symbol, depth=20) for symbol in batch),
            return_exceptions=True,
        )
        for symbol, book in zip(batch, books, strict=True):
            if isinstance(book, BaseException) or not book:
                continue
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                orchestrator.process_order_book(symbol, bids, asks)
        book_index = (book_index + batch_size) % max(1, len(symbols))
        await asyncio.sleep(2.0)


async def run(args: argparse.Namespace) -> int:
    if os.environ.get("LIVE_TRADING_ENABLED", "false").lower() in {"1", "true", "yes"}:
        print("SAFETY GATE: LIVE_TRADING_ENABLED=true — refusing global paper runner")
        return 1

    exchange_ids = list(dict.fromkeys(part.strip() for part in args.exchanges.split(",") if part.strip()))
    unsupported = sorted(set(exchange_ids) - set(SUPPORTED_CCXT_EXCHANGES))
    if unsupported:
        print(f"Unsupported exchanges: {unsupported}")
        return 2
    adapters = [CCXTPublicAdapter(exchange_id.strip()) for exchange_id in exchange_ids]
    builder = MultiVenueUniverseBuilder(
        adapters,
        min_volume_usd=args.min_volume_usd,
        max_spread_bps=args.max_spread_bps,
        max_symbols_per_venue=args.max_symbols_per_venue,
        max_global_symbols=args.max_global_symbols,
    )
    try:
        universe = await builder.build()
        if not universe.selections:
            print(f"No global markets selected. Errors: {universe.errors}")
            return 1
        print(f"Global universe: {universe.symbol_count} unique assets")
        for venue, symbols in sorted(universe.by_venue.items()):
            print(f"  {venue:8s}: {len(symbols)}")
        for venue, error in sorted(universe.errors.items()):
            print(f"  {venue:8s}: unavailable ({error[:120]})")

        adapters_by_id = {adapter.exchange_id: adapter for adapter in adapters}
        orchestrators: dict[str, PaperTradingOrchestrator] = {}
        stop = asyncio.Event()
        feed_tasks: list[asyncio.Task[Any]] = []
        run_tasks: list[asyncio.Task[Any]] = []
        for venue, symbols in universe.by_venue.items():
            venue_balance = args.initial_balance * len(symbols) / universe.symbol_count
            db_path = f"data/{args.experiment_id}_{venue}.db"
            if args.fresh_db:
                for suffix in ("", "-wal", "-shm"):
                    Path(f"{db_path}{suffix}").unlink(missing_ok=True)
            orchestrator = PaperTradingOrchestrator(
                symbols=symbols,
                initial_balance=venue_balance,
                max_symbols=len(symbols),
                db_path=db_path,
                aggressive_paper=True,
                exchange_name=venue,
            )
            orchestrators[venue] = orchestrator
            feed_tasks.append(
                asyncio.create_task(_venue_feed(orchestrator, adapters_by_id[venue], symbols, stop))
            )
            run_tasks.append(asyncio.create_task(orchestrator.start(duration_seconds=args.duration)))

        await asyncio.sleep(args.duration)
        stop.set()
        for orchestrator in orchestrators.values():
            orchestrator.stop()
        await asyncio.gather(*feed_tasks, return_exceptions=True)
        results = await asyncio.gather(*run_tasks, return_exceptions=True)

        print("\nGLOBAL PAPER RESULTS")
        total_entries = total_closed = 0
        for (venue, orchestrator), result in zip(orchestrators.items(), results, strict=True):
            if isinstance(result, BaseException):
                print(f"  {venue:8s}: ERROR {result}")
                continue
            entries = int(result.get("positions_opened", 0))
            closed = int(result.get("positions_closed", 0))
            total_entries += entries
            total_closed += closed
            print(
                f"  {venue:8s}: entries={entries} closed={closed} "
                f"equity=${orchestrator.account.state.equity:,.2f}"
            )
        print(f"  TOTAL: entries={total_entries} closed={total_closed}")
        print("  REAL ORDERS: 0 | PAPER ONLY")
        return 0
    finally:
        await builder.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Global multi-exchange crypto paper runner")
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--initial-balance", type=float, default=10_000.0)
    parser.add_argument("--experiment-id", default="global_v1")
    parser.add_argument("--fresh-db", action="store_true")
    parser.add_argument("--exchanges", default=",".join(SUPPORTED_CCXT_EXCHANGES))
    parser.add_argument("--min-volume-usd", type=float, default=250_000.0)
    parser.add_argument("--max-spread-bps", type=float, default=35.0)
    parser.add_argument("--max-symbols-per-venue", type=int, default=150)
    parser.add_argument("--max-global-symbols", type=int, default=500)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
