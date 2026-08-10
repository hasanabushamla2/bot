#!/usr/bin/env python3
"""Live Market Data Demo — connects to Binance, subscribes to instruments,
receives live data, maintains order books, computes metrics, displays summary.

NO ORDERS are placed. Public data only. API keys NOT required.

Usage:
    python scripts/run_market_data.py [--duration 60] [--symbols BTCUSDT,ETHUSDT]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure src is on path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


async def _ticker_printer(symbol: str, adapter: Any) -> None:
    """Print ticker updates for one symbol."""
    async for ticker in adapter.subscribe_ticker(symbol):
        now = datetime.now(timezone.utc)
        spread = ticker.ask - ticker.bid
        spread_bps = (spread / ticker.last * 10000) if ticker.last > 0 else 0
        print(
            f"[{now.strftime('%H:%M:%S')}] {symbol:12s}  "
            f"bid={ticker.bid:,.2f}  ask={ticker.ask:,.2f}  "
            f"last={ticker.last:,.2f}  spread={spread_bps:.1f}bps  "
            f"vol=${ticker.volume_24h:,.0f}"
        )


async def _book_printer(symbol: str, adapter: Any) -> None:
    """Print order book snapshots for one symbol."""
    async for book in adapter.subscribe_order_book(symbol):
        bb = book.bids[0].price if book.bids else 0
        ba = book.asks[0].price if book.asks else 0
        depth_bid = sum(b.quantity for b in book.bids[:5])
        depth_ask = sum(a.quantity for a in book.asks[:5])
        mid = (bb + ba) / 2 if bb > 0 and ba > 0 else 0
        spread_bps = (ba - bb) / mid * 10000 if mid > 0 else 0
        print(
            f"  BOOK {symbol:10s}  "
            f"bid={bb:,.2f}({depth_bid:.2f})  ask={ba:,.2f}({depth_ask:.2f})  "
            f"spread={spread_bps:.1f}bps  levels={len(book.bids)}+{len(book.asks)}"
        )


async def _trade_printer(symbol: str, adapter: Any) -> None:
    """Print trade stream for one symbol."""
    async for trade in adapter.subscribe_trades(symbol):
        side = "BUY " if trade.side.value == "buy" else "SELL"
        print(f"  TRADE {symbol:10s}  {side}  {trade.quantity:8.4f} @ {trade.price:,.2f}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Live Market Data Demo")
    parser.add_argument(
        "--duration", type=int, default=30,
        help="Run duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT",
        help="Comma-separated symbols (default: BTCUSDT,ETHUSDT,SOLUSDT)",
    )
    parser.add_argument(
        "--streams", type=str, default="ticker,book",
        help="Streams to subscribe: ticker,book,trades (default: ticker,book)",
    )
    parser.add_argument(
        "--testnet", action="store_true",
        help="Use Binance testnet instead of production",
    )
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    streams = [s.strip().lower() for s in args.streams.split(",")]

    from src.adapters.crypto.binance import BinanceAdapter

    adapter = BinanceAdapter(
        exchange_name="binance-testnet" if args.testnet else "binance",
        use_testnet=args.testnet,
    )

    print("=" * 75)
    print(f"  QUANT ENGINE — LIVE MARKET DATA DEMO")
    print(f"  Exchange: {'Binance Testnet' if args.testnet else 'Binance'}")
    print(f"  Symbols:  {', '.join(symbols)}")
    print(f"  Streams:  {', '.join(streams)}")
    print(f"  Duration: {args.duration}s")
    print(f"  Mode:     PUBLIC DATA — NO ORDERS")
    print("=" * 75)

    await adapter.connect()

    # Verify connectivity
    if not await adapter.health_check():
        print("ERROR: Cannot reach Binance API. Check network or try --testnet.")
        await adapter.disconnect()
        return

    server_time = await adapter.get_server_time()
    local_time = datetime.now(timezone.utc)
    offset_ms = (server_time - local_time).total_seconds() * 1000
    print(f"\n  Server time: {server_time.isoformat()}")
    print(f"  Local time:  {local_time.isoformat()}")
    print(f"  Clock offset: {offset_ms:+.1f}ms")
    print()

    # Create tasks for each subscription
    tasks: list[asyncio.Task[Any]] = []
    for symbol in symbols:
        if "ticker" in streams:
            tasks.append(asyncio.create_task(_ticker_printer(symbol, adapter)))
        if "book" in streams:
            tasks.append(asyncio.create_task(_book_printer(symbol, adapter)))
        if "trades" in streams:
            tasks.append(asyncio.create_task(_trade_printer(symbol, adapter)))

    print(f"  Listening for {args.duration} seconds...\n")

    try:
        await asyncio.sleep(args.duration)
    except KeyboardInterrupt:
        print("\n  Interrupted by user.")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.disconnect()

    print(f"\n  Done. {len(tasks)} streams closed cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
