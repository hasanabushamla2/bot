#!/usr/bin/env python3
# ruff: noqa: T201
"""Database Analysis Tool — analyze paper run from SQLite DB facts only.

Usage:
    python scripts/analyze_paper_run.py <db_path>
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def analyze_database(db_path: str) -> dict:
    if not Path(db_path).exists():
        print(f"ERROR: Database file not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # 1. ACCOUNT
    acct_row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    if not acct_row:
        initial_equity = 10000.0
        ending_equity = 10000.0
        realized_pnl = 0.0
        total_fees = 0.0
        total_slippage = 0.0
        max_drawdown = 0.0
        cash = 10000.0
    else:
        initial_equity = float(acct_row["initial_balance"])
        cash = float(acct_row["cash"])
        realized_pnl = float(acct_row["realized_pnl"])
        total_fees = float(acct_row["total_fees"])
        total_slippage = float(acct_row["total_slippage"])
        max_drawdown = float(acct_row["max_drawdown_pct"])
        # ending equity = cash + sum(open positions notional)
        open_pos_rows = conn.execute("SELECT * FROM paper_positions WHERE is_open=1").fetchall()
        open_notional = sum(float(r["entry_notional"]) for r in open_pos_rows)
        ending_equity = cash + open_notional

    # 2. ORDERS & FILLS & SLIPPAGE
    orders = conn.execute("SELECT * FROM paper_orders").fetchall()
    fills = conn.execute("SELECT * FROM paper_fills").fetchall()

    partial_fills = [o for o in orders if "PARTIAL" in str(o["status"]).upper()]
    nonterminal_orders = [
        o for o in orders if str(o["status"]).upper() in ("NEW", "OPEN", "PARTIALLY_FILLED", "PENDING")
    ]

    slippage_bps_list = [float(f["slippage_bps"]) for f in fills if f["slippage_bps"] is not None]
    if slippage_bps_list:
        sorted_slips = sorted(slippage_bps_list)
        avg_slip = sum(sorted_slips) / len(sorted_slips)
        p50_slip = sorted_slips[int(len(sorted_slips) * 0.50)]
        p95_slip = sorted_slips[int(len(sorted_slips) * 0.95)]
        max_slip = max(sorted_slips)
    else:
        avg_slip = p50_slip = p95_slip = max_slip = 0.0

    # 3. TRADING (CLOSED TRADES)
    trades = conn.execute("SELECT * FROM paper_closed_trades ORDER BY exit_time ASC").fetchall()
    trade_count = len(trades)
    wins = [t for t in trades if float(t["net_pnl"]) > 0]
    losses = [t for t in trades if float(t["net_pnl"]) <= 0]
    win_count = len(wins)
    loss_count = len(losses)
    win_rate = (win_count / trade_count * 100.0) if trade_count > 0 else 0.0

    gross_pnl_total = sum(float(t["gross_pnl"]) for t in trades)
    net_pnl_total = sum(float(t["net_pnl"]) for t in trades)
    win_pnl_total = sum(float(t["net_pnl"]) for t in wins)
    loss_pnl_total = sum(float(t["net_pnl"]) for t in losses)

    avg_win = (win_pnl_total / win_count) if win_count > 0 else 0.0
    avg_loss = (loss_pnl_total / loss_count) if loss_count > 0 else 0.0
    profit_factor = (
        (win_pnl_total / abs(loss_pnl_total))
        if loss_pnl_total < 0
        else (99.0 if win_pnl_total > 0 else 1.0)
    )
    expectancy = (net_pnl_total / trade_count) if trade_count > 0 else 0.0

    # 4. EXIT REASONS
    hard_stops = [t for t in trades if t["exit_reason"] in ("hard_stop", "stop_loss")]
    trailing_exits = [t for t in trades if t["exit_reason"] == "trail_hit"]
    other_exits = [
        t for t in trades if t["exit_reason"] not in ("hard_stop", "stop_loss", "trail_hit")
    ]
    hard_stop_pnl = sum(float(t["net_pnl"]) for t in hard_stops)
    trailing_pnl = sum(float(t["net_pnl"]) for t in trailing_exits)

    # 5. SYMBOLS
    sym_stats: dict[str, dict] = {}
    for t in trades:
        sym = t["symbol"]
        if sym not in sym_stats:
            sym_stats[sym] = {
                "trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0,
                "stopouts": 0, "slippages": [],
            }
        s = sym_stats[sym]
        s["trades"] += 1
        pnl = float(t["net_pnl"])
        s["net_pnl"] += pnl
        if pnl > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        if t["exit_reason"] in ("hard_stop", "stop_loss"):
            s["stopouts"] += 1

    for f in fills:
        sym = f["symbol"]
        if sym in sym_stats and f["slippage_bps"] is not None:
            sym_stats[sym]["slippages"].append(float(f["slippage_bps"]))

    sorted_syms = sorted(sym_stats.items(), key=lambda kv: kv[1]["net_pnl"])
    worst_symbols = sorted_syms[:5]
    best_symbols = sorted_syms[-5:][::-1] if len(sorted_syms) >= 5 else sorted_syms[::-1]
    largest_single_symbol_loss = min((s["net_pnl"] for s in sym_stats.values()), default=0.0)
    worst_symbol_name = worst_symbols[0][0] if worst_symbols else "NONE"

    # 6. STRATEGIES
    strat_stats: dict[str, dict] = {}
    for t in trades:
        sid = t["strategy_id"] or "unknown"
        if sid not in strat_stats:
            strat_stats[sid] = {"trades": 0, "wins": 0, "losses": 0, "net_pnl": 0.0}
        st = strat_stats[sid]
        st["trades"] += 1
        pnl = float(t["net_pnl"])
        st["net_pnl"] += pnl
        if pnl > 0:
            st["wins"] += 1
        else:
            st["losses"] += 1

    # 7. RISK
    risk_row = conn.execute("SELECT * FROM paper_risk WHERE id=1").fetchone()
    circuit_breaker_active = bool(risk_row["breaker_active"]) if risk_row else False

    # 8. INTEGRITY CHECKS
    # Orphan trails: trails with no open position
    orphan_trails = conn.execute(
        "SELECT position_id FROM paper_trail WHERE position_id NOT IN (SELECT position_id FROM paper_positions WHERE is_open=1)"
    ).fetchall()

    # Duplicate trade IDs
    trade_ids = [t["trade_id"] for t in trades]
    duplicate_trade_ids = len(trade_ids) - len(set(trade_ids))

    # Accounting consistency: cash == initial_equity - open_positions_cost_basis + sum(realized_pnl)
    open_pos_rows = conn.execute("SELECT * FROM paper_positions WHERE is_open=1").fetchall()
    open_cost_basis = sum(float(r["cost_basis"]) if "cost_basis" in r.keys() else float(r["entry_notional"]) + float(r.get("entry_fee", 0)) for r in open_pos_rows)
    expected_cash = initial_equity - open_cost_basis + net_pnl_total
    accounting_mismatch = abs(cash - expected_cash) > 0.01

    # Unmatched fills: fills without matching orders
    order_ids = {o["order_id"] for o in orders}
    unmatched_fills = [f for f in fills if f["order_id"] not in order_ids]

    conn.close()

    result = {
        "db_path": db_path,
        "account": {
            "initial_equity": initial_equity,
            "ending_equity": ending_equity,
            "cash": cash,
            "realized_pnl": realized_pnl,
            "net_pnl_from_trades": net_pnl_total,
            "total_fees": total_fees,
            "total_slippage": total_slippage,
            "max_drawdown_pct": max_drawdown,
        },
        "execution": {
            "orders": len(orders),
            "fills": len(fills),
            "partial_fills": len(partial_fills),
            "nonterminal_orders": len(nonterminal_orders),
            "average_slippage_bps": round(avg_slip, 2),
            "p50_slippage_bps": round(p50_slip, 2),
            "p95_slippage_bps": round(p95_slip, 2),
            "max_slippage_bps": round(max_slip, 2),
        },
        "trading": {
            "trades": trade_count,
            "wins": win_count,
            "losses": loss_count,
            "win_rate": round(win_rate, 2),
            "expectancy": round(expectancy, 4),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "gross_pnl": round(gross_pnl_total, 2),
            "net_pnl": round(net_pnl_total, 2),
        },
        "exit_reasons": {
            "hard_stops": len(hard_stops),
            "hard_stop_pnl": round(hard_stop_pnl, 2),
            "trailing_exits": len(trailing_exits),
            "trailing_pnl": round(trailing_pnl, 2),
            "other_exits": len(other_exits),
        },
        "symbols": {
            "worst_symbols": [
                {"symbol": k, "pnl": round(v["net_pnl"], 2), "trades": v["trades"], "stopouts": v["stopouts"]}
                for k, v in worst_symbols
            ],
            "best_symbols": [
                {"symbol": k, "pnl": round(v["net_pnl"], 2), "trades": v["trades"], "stopouts": v["stopouts"]}
                for k, v in best_symbols
            ],
            "largest_single_symbol_loss": round(largest_single_symbol_loss, 2),
            "worst_symbol": worst_symbol_name,
        },
        "strategies": {
            sid: {
                "trades": v["trades"], "wins": v["wins"], "losses": v["losses"],
                "win_rate": round(v["wins"] / v["trades"] * 100.0, 2) if v["trades"] > 0 else 0.0,
                "net_pnl": round(v["net_pnl"], 2),
                "expectancy": round(v["net_pnl"] / v["trades"], 4) if v["trades"] > 0 else 0.0,
            }
            for sid, v in strat_stats.items()
        },
        "risk": {
            "circuit_breaker_active": circuit_breaker_active,
        },
        "integrity": {
            "orphan_trails": len(orphan_trails),
            "duplicate_trades": duplicate_trade_ids,
            "unmatched_fills": len(unmatched_fills),
            "nonterminal_partial_orders": len(nonterminal_orders),
            "accounting_mismatch": accounting_mismatch,
            "cash_vs_expected_diff": round(abs(cash - expected_cash), 4),
        },
    }

    return result


def print_report(res: dict) -> None:
    print("=" * 70)
    print("  PAPER TRADING DATABASE AUDIT & ANALYSIS")
    print(f"  Database: {res['db_path']}")
    print("=" * 70)

    acct = res["account"]
    print("\n[ ACCOUNT ]")
    print(f"  Starting Equity:     ${acct['initial_equity']:,.2f}")
    print(f"  Ending Equity:       ${acct['ending_equity']:,.2f}")
    print(f"  Cash Balance:        ${acct['cash']:,.2f}")
    print(f"  Realized PnL:        ${acct['realized_pnl']:,.2f}")
    print(f"  Total Fees:          ${acct['total_fees']:,.4f}")
    print(f"  Total Slippage Cost: ${acct['total_slippage']:,.4f}")
    print(f"  Max Drawdown:        {acct['max_drawdown_pct']:.2f}%")

    exc = res["execution"]
    print("\n[ EXECUTION ]")
    print(f"  Orders Created:      {exc['orders']}")
    print(f"  Fills Created:       {exc['fills']}")
    print(f"  Partial Orders:      {exc['partial_fills']}")
    print(f"  Average Slippage:    {exc['average_slippage_bps']:.2f} bps")
    print(f"  P50 Slippage:        {exc['p50_slippage_bps']:.2f} bps")
    print(f"  P95 Slippage:        {exc['p95_slippage_bps']:.2f} bps")
    print(f"  Max Slippage:        {exc['max_slippage_bps']:.2f} bps")

    trd = res["trading"]
    print("\n[ TRADING ]")
    print(f"  Total Closed Trades: {trd['trades']}")
    print(f"  Winning Trades:      {trd['wins']}")
    print(f"  Losing Trades:       {trd['losses']}")
    print(f"  Win Rate:            {trd['win_rate']:.2f}%")
    print(f"  Profit Factor:       {trd['profit_factor']:.2f}")
    print(f"  Expectancy:          ${trd['expectancy']:.4f}")
    print(f"  Average Win:         ${trd['avg_win']:.2f}")
    print(f"  Average Loss:        ${trd['avg_loss']:.2f}")

    exits = res["exit_reasons"]
    print("\n[ EXIT REASONS ]")
    print(f"  Hard Stops:          {exits['hard_stops']} (PnL: ${exits['hard_stop_pnl']:,.2f})")
    print(f"  Trailing Exits:      {exits['trailing_exits']} (PnL: ${exits['trailing_pnl']:,.2f})")
    print(f"  Other Exits:         {exits['other_exits']}")

    syms = res["symbols"]
    print("\n[ SYMBOLS ]")
    print(f"  Largest Symbol Loss: ${syms['largest_single_symbol_loss']:,.2f} ({syms['worst_symbol']})")
    if syms["worst_symbols"]:
        print("  Worst Symbols:")
        for ws in syms["worst_symbols"]:
            print(f"    - {ws['symbol']}: PnL=${ws['pnl']:,.2f}, Trades={ws['trades']}, Stopouts={ws['stopouts']}")
    if syms["best_symbols"]:
        print("  Best Symbols:")
        for bs in syms["best_symbols"]:
            print(f"    - {bs['symbol']}: PnL=${bs['pnl']:,.2f}, Trades={bs['trades']}, Stopouts={bs['stopouts']}")

    strats = res["strategies"]
    print("\n[ STRATEGIES ]")
    for sid, sv in strats.items():
        print(f"  - {sid}: Trades={sv['trades']}, WinRate={sv['win_rate']}%, PnL=${sv['net_pnl']:,.2f}, Expectancy=${sv['expectancy']:.4f}")

    risk = res["risk"]
    print("\n[ RISK ]")
    print(f"  Circuit Breaker:     {'TRIPPED' if risk['circuit_breaker_active'] else 'NORMAL'}")

    integ = res["integrity"]
    print("\n[ INTEGRITY & RECONCILIATION ]")
    print(f"  Orphan Trails:       {integ['orphan_trails']} {'(PASS)' if integ['orphan_trails'] == 0 else '(FAIL)'}")
    print(f"  Duplicate Trades:    {integ['duplicate_trades']} {'(PASS)' if integ['duplicate_trades'] == 0 else '(FAIL)'}")
    print(f"  Unmatched Fills:     {integ['unmatched_fills']} {'(PASS)' if integ['unmatched_fills'] == 0 else '(FAIL)'}")
    print(f"  Nonterminal Orders:  {integ['nonterminal_partial_orders']} {'(PASS)' if integ['nonterminal_partial_orders'] == 0 else '(FAIL)'}")
    print(f"  Accounting Match:    {'PASS (Diff=$' + str(integ['cash_vs_expected_diff']) + ')' if not integ['accounting_mismatch'] else 'FAIL'}")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Analyze Paper Trading SQLite Database")
    parser.add_argument("db_path", help="Path to SQLite database file")
    args = parser.parse_args()

    results = analyze_database(args.db_path)
    print_report(results)


if __name__ == "__main__":
    main()
