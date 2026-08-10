#!/usr/bin/env python3
"""Run a backtest from the command line.

Usage:
    python scripts/run_backtest.py --symbol BTC-USD --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.backtesting.engine import BacktestConfig, BacktestEngine, PeriodType


def dummy_strategy(data: pd.DataFrame) -> dict | None:
    """Placeholder — replace with real strategy."""

    if len(data) < 20:
        return None
    close = data["close"]
    sma_short = close.rolling(10).mean().iloc[-1]
    sma_long = close.rolling(20).mean().iloc[-1]
    if sma_short > sma_long:
        return {"direction": "long", "size_pct": 0.1}
    return None  # Only long signals — SPOT ONLY


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest")
    parser.add_argument("--symbol", default="BTC-USD")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--capital", type=float, default=10000.0)
    args = parser.parse_args()

    # Generate sample data (in production, load from database/CSV)
    import numpy as np
    import pandas as pd

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end)
    dates = pd.date_range(start, end, freq="1h")
    np.random.seed(42)
    price = 50000.0
    prices = []
    for _ in range(len(dates)):
        price *= np.exp(np.random.normal(0, 0.002))
        prices.append(price)
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": prices,
            "high": [p * 1.002 for p in prices],
            "low": [p * 0.998 for p in prices],
            "close": prices,
            "volume": np.random.uniform(1, 100, len(dates)),
        }
    )

    config = BacktestConfig(
        symbol=args.symbol,
        start_date=start,
        end_date=end,
        initial_capital=args.capital,
    )
    engine = BacktestEngine(config)
    result = engine.run(df, dummy_strategy, period_type=PeriodType.TEST)

    print(f"\nBacktest Results: {args.symbol} ({start.date()} → {end.date()})")
    print(f"{'=' * 50}")
    print(f"Total Trades:      {result.total_trades}")
    print(f"Win Rate:          {result.win_rate:.2%}")
    print(f"Net P&L:           ${result.net_pnl:,.2f}")
    print(f"Total Return:      {result.total_return_pct:.2f}%")
    print(f"Profit Factor:     {result.profit_factor:.2f}")
    print(f"Max Drawdown:      {result.max_drawdown_pct:.2f}%")
    print(f"Sharpe Ratio:      {result.sharpe_ratio:.2f}")
    print(f"Sortino Ratio:     {result.sortino_ratio:.2f}")
    print(f"Expectancy:        {result.expectancy:.4f}")
    print(f"Total Fees:        ${result.total_fees:,.2f}")
    print(f"Total Slippage:    ${result.total_slippage:,.2f}")
    print(f"Avg Trade Return:  {result.avg_trade_return_pct:.4f}%")


if __name__ == "__main__":
    main()
