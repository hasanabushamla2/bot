#!/usr/bin/env python3
# ruff: noqa: T201
"""R18: LIVE PAPER TRADING — Real market data, simulated paper orders.

Connects to live exchange public APIs.
Feeds real ticker + order-book depth through the EXISTING orchestrator.
ALL orders are PAPER ONLY. Real exchange order placement is IMPOSSIBLE.

Usage:
    python scripts/run_live_paper.py --duration 300 --symbols BTC-USDT,ETH-USDT,SOL-USDT

Architecture:
    LIVE PUBLIC API (Binance WS or KuCoin REST)
    → process_ticker/process_order_book
    → orchestrator.start()
    → strategies → risk engine → PaperExecutionEngine
    → persistence → dashboard

SAFETY:
    - No exchange API keys required
    - REAL order placement is NOT possible
    - Live-trading gate refuses to start if LIVE_TRADING_ENABLED=true
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _safety_gate() -> None:
    """Refuse to start if any dangerous configuration is detected."""
    danger_flags = [
        ("LIVE_TRADING_ENABLED", os.environ.get("LIVE_TRADING_ENABLED", "false").lower()),
    ]
    for flag, value in danger_flags:
        if value in ("true", "1", "yes"):
            print(f"SAFETY GATE: {flag}={value} — REFUSING TO START")
            sys.exit(1)
    print("SAFETY GATE: All checks passed — live paper mode is safe.")


async def _kucoin_feed(orch, adapter, symbols: list[str], stop_event: asyncio.Event):
    """Poll KuCoin REST API for real ticker + order book data."""
    ticker_count = 0
    book_count = 0
    while not stop_event.is_set():
        for raw in symbols:
            if stop_event.is_set():
                break
            # Ticker (best bid/ask)
            t = await adapter.get_ticker(raw)
            if t and t.get("last", 0) > 0:
                orch.process_ticker(
                    raw, t["bid"], t["ask"], t["last"],
                    volume_24h=t.get("bid_size", 0) * 10000,
                )
                ticker_count += 1

            # Order book depth
            ob = await adapter.get_order_book(raw, depth=20)
            if ob:
                orch.process_order_book(raw, ob["bids"], ob["asks"])
                book_count += 1

            await asyncio.sleep(0.05)  # yield between symbols

        await asyncio.sleep(1.0)  # ~1s poll interval

    return ticker_count, book_count


async def _binance_feed(orch, adapter, symbols: list[str], stop_event: asyncio.Event):
    """Feed Binance WebSocket data through orchestrator."""
    # Ticker streams
    ticker_tasks = []
    for raw in symbols:
        async def _ticker_loop(raw=raw):
            async for ticker in adapter.subscribe_ticker(raw):
                if stop_event.is_set():
                    break
                orch.process_ticker(raw, ticker.bid, ticker.ask, ticker.last, ticker.volume_24h)
                await asyncio.sleep(0.0)

        async def _book_loop(raw=raw):
            async for book in adapter.subscribe_order_book(raw):
                if stop_event.is_set():
                    break
                bids = [(b.price, b.quantity) for b in book.bids[:20]]
                asks = [(a.price, a.quantity) for a in book.asks[:20]]
                orch.process_order_book(raw, bids, asks)
                await asyncio.sleep(0.0)

        ticker_tasks.append(asyncio.create_task(_ticker_loop()))
        ticker_tasks.append(asyncio.create_task(_book_loop()))

    await stop_event.wait()
    for t in ticker_tasks:
        t.cancel()
    await asyncio.gather(*ticker_tasks, return_exceptions=True)


async def run_live_paper(
    duration: int, symbols: list[str], experiment_id: str,
    exchange: str = "auto",
):
    from src.core.logging_config import setup_logging
    from src.paper.orchestrator import PaperTradingOrchestrator

    setup_logging(level="INFO", fmt="json", log_dir="logs", max_bytes=10 * 1024 * 1024, backup_count=5)
    os.environ["PAPER_EXPERIMENT_ID"] = experiment_id

    db_path = f"data/{experiment_id}.db"
    print(f"LIVE-PAPER: DB={db_path}")
    print(f"LIVE-PAPER: Symbols={symbols}")
    print(f"LIVE-PAPER: Duration={duration}s")
    print(f"LIVE-PAPER: Exchange={exchange}")

    # Try Binance WebSocket first, fall back to KuCoin REST
    wall_start = datetime.now(UTC)
    stop_event = asyncio.Event()
    feed_task = None
    active_exchange = "unknown"

    if exchange in ("auto", "binance"):
        try:
            from src.adapters.crypto.binance import BinanceAdapter
            adapter = BinanceAdapter(exchange_name="binance", use_testnet=False)
            await adapter.connect()
            if await adapter.health_check():
                active_exchange = "binance"
                print("  Connected to Binance (WebSocket)")
                server_time = await adapter.get_server_time()
                offset = (server_time - datetime.now(UTC)).total_seconds() * 1000
                print(f"  Clock offset: {offset:+.1f}ms")
                feed_task = asyncio.create_task(
                    _binance_feed(orch := PaperTradingOrchestrator(
                        symbols=symbols, initial_balance=10000,
                        max_symbols=len(symbols), db_path=db_path,
                    ), adapter, symbols, stop_event)
                )
            else:
                await adapter.disconnect()
        except Exception as e:
            print(f"  Binance unavailable: {e}")

    if feed_task is None and exchange in ("auto", "kucoin"):
        try:
            from src.adapters.crypto.kucoin import KuCoinPublicAdapter
            adapter = KuCoinPublicAdapter()
            await adapter.connect()
            if await adapter.health_check():
                active_exchange = "kucoin"
                print("  Connected to KuCoin (REST polling)")
                orch = PaperTradingOrchestrator(
                    symbols=symbols, initial_balance=10000,
                    max_symbols=len(symbols), db_path=db_path,
                )
                feed_task = asyncio.create_task(
                    _kucoin_feed(orch, adapter, symbols, stop_event)
                )
            else:
                await adapter.disconnect()
        except Exception as e:
            print(f"  KuCoin unavailable: {e}")

    if feed_task is None:
        print("ERROR: No exchange connection available.")
        return 1

    print(f"  Exchange: {active_exchange}")
    print(f"  Running for {duration}s...\n")

    # Run orchestrator
    orch_task = asyncio.create_task(orch.start(duration_seconds=duration))

    # Wait for duration
    await asyncio.sleep(duration)

    # Stop
    stop_event.set()
    orch.stop()
    await asyncio.gather(feed_task, return_exceptions=True)
    result = await orch_task
    if hasattr(adapter, "disconnect"):
        await adapter.disconnect()

    wall_end = datetime.now(UTC)
    wall_secs = (wall_end - wall_start).total_seconds()

    # ── Print results ──
    print()
    print("=" * 60)
    print("  LIVE PAPER PRE-FLIGHT RESULTS")
    print("=" * 60)
    print(f"  Exchange:         {active_exchange}")
    print(f"  Symbols:          {', '.join(symbols)}")
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
    print(f"  Open now:         {result.get('positions_currently_open', 0)}")
    print(f"  Cash:             ${result.get('final_equity', 0):,.2f}")
    print(f"  Realized PnL:     ${result.get('net_pnl', 0):,.2f}")
    print(f"  Fees:             ${result.get('total_fees', 0):,.4f}")
    print(f"  Slippage:         ${result.get('total_slippage', 0):,.4f}")
    print(f"  Persist writes:   {result.get('persistence_writes', 0)}")
    print(f"  Persist errors:   {result.get('persistence_errors', 0)}")
    print(f"  Exceptions:       {result.get('exceptions', 0)}")
    print(f"  RSS:              {result.get('rss_start_mb', 0):.0f}/{result.get('rss_peak_mb', 0):.0f} MB")
    print()
    print("  REAL EXCHANGE ORDERS SUBMITTED: 0")
    print("  LIVE_TRADING_ENABLED: false")
    print(f"  MODE: LIVE MARKET DATA ({active_exchange}) / PAPER EXECUTION")
    print("=" * 60)

    # ── Auto-fail ──
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
    print("\nALL CHECKS PASSED — No violations.")
    return 0


async def main():
    parser = argparse.ArgumentParser(description="Live Paper Trading — PUBLIC DATA ONLY")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--symbols", type=str, default="BTC-USDT,ETH-USDT,SOL-USDT")
    parser.add_argument("--experiment-id", type=str, default="live_paper_preflight")
    parser.add_argument("--exchange", type=str, default="auto",
                        choices=["auto", "binance", "kucoin"])
    args = parser.parse_args()

    _safety_gate()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    exp_id = args.experiment_id

    print("=" * 60)
    print("  QUANT ENGINE — LIVE MARKET / PAPER EXECUTION")
    print(f"  Symbols:       {', '.join(symbols)}")
    print(f"  Duration:      {args.duration}s")
    print(f"  Experiment:    {exp_id}")
    print("  REAL ORDERS:   DISABLED")
    print("  API KEYS:      NOT REQUIRED (public data only)")
    print("=" * 60)
    print()

    exit_code = await run_live_paper(args.duration, symbols, exp_id, args.exchange)
    sys.exit(exit_code)


if __name__ == "__main__":
    asyncio.run(main())
