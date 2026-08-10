#!/usr/bin/env python3
"""Safe Paper Trading Runner — end-to-end paper trading with LIVE public data.
NO real orders. NO API keys required.
Usage:
  python scripts/run_paper_trading.py --starting-balance 1000 --duration 300 --symbols BTCUSDT,ETHUSDT,SOLUSDT
"""
from __future__ import annotations
import argparse, asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

async def main() -> None:
    parser = argparse.ArgumentParser(description="Paper Trading Runner")
    parser.add_argument("--starting-balance", type=float, default=10000.0)
    parser.add_argument("--duration", type=int, default=300, help="Seconds to run")
    parser.add_argument("--symbols", type=str, default="BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT")
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--log-level", type=str, default="INFO")
    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    from src.paper.orchestrator import PaperTradingOrchestrator
    orch = PaperTradingOrchestrator(
        symbols=symbols, initial_balance=args.starting_balance,
        max_symbols=args.max_symbols, use_testnet=args.testnet,
    )
    print("=" * 60)
    print("  PAPER TRADING RUNNER")
    print(f"  Balance: ${args.starting_balance:,.0f} | Duration: {args.duration}s")
    print(f"  Symbols: {len(symbols)} | Mode: PAPER | Live Trading: DISABLED")
    print("=" * 60)
    result = await orch.start(duration_seconds=args.duration)
    print("\n" + "=" * 60)
    print("  FINAL REPORT")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
