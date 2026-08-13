#!/usr/bin/env python3
# ruff: noqa: T201
"""Fact-only report for a paper-trading SQLite database.

Usage:
    python scripts/analyze_paper_run.py data/full_soak_1h.db
    python scripts/analyze_paper_run.py data/full_soak_1h.db --json
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _value(row: sqlite3.Row | None, key: str, default: Any = 0.0) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _float(row: sqlite3.Row | None, key: str, default: float = 0.0) -> float:
    try:
        return float(_value(row, key, default))
    except (TypeError, ValueError):
        return default


def _holding_seconds(row: sqlite3.Row) -> float:
    stored = _float(row, "holding_seconds", -1.0)
    if stored >= 0:
        return stored
    try:
        return max(
            0.0,
            (
                datetime.fromisoformat(str(row["exit_time"]))
                - datetime.fromisoformat(str(row["entry_time"]))
            ).total_seconds(),
        )
    except (KeyError, TypeError, ValueError):
        return 0.0


def _slippage_stats(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "avg": sum(ordered) / len(ordered),
        "p50": statistics.median(ordered),
        "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "max": max(ordered),
    }


def _trade_metrics(rows: list[sqlite3.Row]) -> dict[str, Any]:
    """Cost-aware metrics for a database trade subset."""
    net_values = [_float(row, "net_pnl") for row in rows]
    gross_values = [_float(row, "gross_pnl") for row in rows]
    fee_values = [_float(row, "fees") for row in rows]
    slippage_values = [_float(row, "slippage_cost") for row in rows]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value <= 0]
    gross_loss = abs(sum(losses))
    profit_factor: float | None = sum(wins) / gross_loss if gross_loss > 0 else None
    mfe = [_float(row, "mfe_pct") for row in rows]
    mae = [_float(row, "mae_pct") for row in rows]
    holding = [_holding_seconds(row) for row in rows]
    return {
        "trade_count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(rows) * 100.0 if rows else 0.0,
        "gross_pnl": sum(gross_values),
        "fees": sum(fee_values),
        "slippage": sum(slippage_values),
        "net_pnl": sum(net_values),
        "profit_factor": profit_factor,
        "expectancy": sum(net_values) / len(rows) if rows else 0.0,
        "average_winner": statistics.mean(wins) if wins else 0.0,
        "average_loser": statistics.mean(losses) if losses else 0.0,
        "largest_winner": max(wins) if wins else 0.0,
        "largest_loser": min(losses) if losses else 0.0,
        "mfe": {
            "average_pct": statistics.mean(mfe) if mfe else 0.0,
            "median_pct": statistics.median(mfe) if mfe else 0.0,
            "p95_pct": _slippage_stats(mfe)["p95"],
        },
        "mae": {
            "average_pct": statistics.mean(mae) if mae else 0.0,
            "median_pct": statistics.median(mae) if mae else 0.0,
            "p95_pct": _slippage_stats(mae)["p95"],
        },
        "holding_duration": {
            "average_seconds": statistics.mean(holding) if holding else 0.0,
            "median_seconds": statistics.median(holding) if holding else 0.0,
            "p95_seconds": _slippage_stats(holding)["p95"],
        },
    }


def _group_metrics(rows: list[sqlite3.Row], field_name: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        groups[str(_value(row, field_name, "unknown") or "unknown")].append(row)
    return {name: _trade_metrics(group) for name, group in sorted(groups.items())}


def analyze_database(db_path: str) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row

    account = (
        conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        if _table_exists(conn, "paper_account")
        else None
    )
    initial_balance = _float(account, "initial_balance", 10_000.0)
    cash = _float(account, "cash", initial_balance)
    allocated = _float(account, "allocated", 0.0)
    unrealized_pnl = _float(account, "unrealized_pnl", 0.0)
    realized_pnl = _float(account, "realized_pnl", 0.0)
    final_equity = _float(account, "equity", cash + allocated + unrealized_pnl)
    if final_equity == 0.0 and account is not None:
        final_equity = cash + allocated + unrealized_pnl

    positions = (
        conn.execute("SELECT * FROM paper_positions WHERE is_open=1").fetchall()
        if _table_exists(conn, "paper_positions")
        else []
    )
    orders = (
        conn.execute("SELECT * FROM paper_orders ORDER BY created_at").fetchall()
        if _table_exists(conn, "paper_orders")
        else []
    )
    fills = (
        conn.execute("SELECT * FROM paper_fills ORDER BY filled_at").fetchall()
        if _table_exists(conn, "paper_fills")
        else []
    )
    trades = (
        conn.execute("SELECT * FROM paper_closed_trades ORDER BY exit_time").fetchall()
        if _table_exists(conn, "paper_closed_trades")
        else []
    )
    runtime_metrics = (
        {
            str(row["metric_name"]): float(row["metric_value"])
            for row in conn.execute(
                "SELECT metric_name,metric_value FROM paper_runtime_metrics"
            )
        }
        if _table_exists(conn, "paper_runtime_metrics")
        else {}
    )

    funnel_counter_names = (
        "raw_signals",
        "valid_signals",
        "inactive_signals",
        "opportunities_created",
        "opportunities_below_score_threshold",
        "confidence_rejections",
        "expected_edge_rejections",
        "cooldown_rejections",
        "reentry_rejections",
        "stale_market_rejections",
        "liquidity_rejections",
        "spread_rejections",
        "correlation_rejections",
        "risk_rejections",
        "capacity_rejections",
        "execution_attempts",
        "successful_entries",
        "qualified_opportunities",
        "approved_opportunities",
        "closed_trades",
    )
    funnel = {
        name: int(runtime_metrics.get(f"funnel_{name}", runtime_metrics.get(name, 0)))
        for name in funnel_counter_names
    }

    net_values = [_float(trade, "net_pnl") for trade in trades]
    gross_values = [_float(trade, "gross_pnl") for trade in trades]
    fee_values = [_float(trade, "fees") for trade in trades]
    trade_slippage_values = [_float(trade, "slippage_cost") for trade in trades]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value <= 0]
    gross_profit_for_factor = sum(wins)
    gross_loss_for_factor = abs(sum(losses))
    profit_factor: float | None = (
        gross_profit_for_factor / gross_loss_for_factor
        if gross_loss_for_factor > 0
        else None
    )

    holding_values = [_holding_seconds(trade) for trade in trades]
    exit_reasons = Counter(str(_value(trade, "exit_reason", "unknown")) for trade in trades)
    strategy_analysis = _group_metrics(trades, "strategy_id")
    symbol_analysis = _group_metrics(trades, "symbol")
    hard_stop_rows = [
        trade for trade in trades if str(_value(trade, "exit_reason", "")) in {"hard_stop", "stop_loss"}
    ]
    trail_rows = [
        trade for trade in trades if str(_value(trade, "exit_reason", "")) == "trail_hit"
    ]
    other_exit_rows = [trade for trade in trades if trade not in hard_stop_rows and trade not in trail_rows]
    exit_analysis = {
        "hard_stop": _trade_metrics(hard_stop_rows),
        "trail_hit": _trade_metrics(trail_rows),
        "other": _trade_metrics(other_exit_rows),
    }
    trades_per_symbol = Counter(str(_value(trade, "symbol", "unknown")) for trade in trades)
    pnl_per_symbol: dict[str, float] = defaultdict(float)
    for trade in trades:
        pnl_per_symbol[str(_value(trade, "symbol", "unknown"))] += _float(
            trade, "net_pnl"
        )

    entry_fills = [fill for fill in fills if str(_value(fill, "side", "")).lower() == "buy"]
    exit_fills = [fill for fill in fills if str(_value(fill, "side", "")).lower() == "sell"]
    entry_slippage_bps = [_float(fill, "slippage_bps") for fill in entry_fills]
    exit_slippage_bps = [_float(fill, "slippage_bps") for fill in exit_fills]
    all_slippage_bps = [_float(fill, "slippage_bps") for fill in fills]

    trade_count = len(trades)
    closed_gross_pnl = sum(gross_values)
    closed_fees = sum(fee_values)
    closed_slippage = sum(trade_slippage_values)
    closed_costs = closed_fees + closed_slippage
    closed_net_pnl = sum(net_values)
    total_fees = _float(account, "total_fees", sum(_float(fill, "fees") for fill in fills))
    total_slippage = _float(account, "total_slippage", closed_slippage)

    partial_orders = [
        order for order in orders if "PARTIAL" in str(_value(order, "status", "")).upper()
    ]
    nonterminal_orders = [
        order
        for order in orders
        if str(_value(order, "status", "")).upper()
        in {"NEW", "OPEN", "PARTIALLY_FILLED", "PENDING"}
    ]
    order_ids = {str(_value(order, "order_id", "")) for order in orders}
    unmatched_fills = [
        fill for fill in fills if str(_value(fill, "order_id", "")) not in order_ids
    ]
    trade_ids = [str(_value(trade, "trade_id", "")) for trade in trades]
    fill_ids = [str(_value(fill, "fill_id", "")) for fill in fills]
    orphan_trails = (
        conn.execute(
            "SELECT COUNT(*) FROM paper_trail WHERE position_id NOT IN "
            "(SELECT position_id FROM paper_positions WHERE is_open=1)"
        ).fetchone()[0]
        if _table_exists(conn, "paper_trail") and _table_exists(conn, "paper_positions")
        else 0
    )

    open_cost_basis = sum(
        _float(position, "entry_notional") + _float(position, "entry_fee")
        for position in positions
    )
    expected_cash = initial_balance + realized_pnl - open_cost_basis
    cash_diff = abs(cash - expected_cash)
    realized_diff = abs(realized_pnl - closed_net_pnl)
    trade_reconciliation_errors = sum(
        1
        for gross, fees, slippage, net in zip(
            gross_values, fee_values, trade_slippage_values, net_values, strict=True
        )
        if abs(gross - fees - slippage - net) > 1e-6
    )
    fill_fee_total = sum(_float(fill, "fees") for fill in fills)
    fee_diff = abs(total_fees - fill_fee_total)

    risk_row = (
        conn.execute("SELECT * FROM paper_risk WHERE id=1").fetchone()
        if _table_exists(conn, "paper_risk")
        else None
    )
    conn.close()

    result: dict[str, Any] = {
        "db_path": db_path,
        "account": {
            "initial_balance": initial_balance,
            "cash": cash,
            "allocated": allocated,
            "unrealized_pnl": unrealized_pnl,
            "realized_net_pnl": realized_pnl,
            "final_equity": final_equity,
            "net_pnl": final_equity - initial_balance,
            "peak_equity": _float(account, "peak_equity", initial_balance),
            "max_drawdown_pct": _float(account, "max_drawdown_pct", 0.0),
            "open_positions": len(positions),
        },
        "costs": {
            "gross_pnl_before_costs_closed": closed_gross_pnl,
            "closed_trade_fees": closed_fees,
            "closed_trade_slippage": closed_slippage,
            "closed_total_trading_costs": closed_costs,
            "net_realized_after_costs": closed_net_pnl,
            "account_total_fees_including_open_entries": total_fees,
            "account_total_slippage_including_open_entries": total_slippage,
            "account_total_costs_including_open_entries": total_fees + total_slippage,
        },
        "trading": {
            "trade_count": trade_count,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": len(wins) / trade_count * 100.0 if trade_count else 0.0,
            "profit_factor": profit_factor,
            "average_win": statistics.mean(wins) if wins else 0.0,
            "average_loss": statistics.mean(losses) if losses else 0.0,
            "largest_win": max(wins) if wins else 0.0,
            "largest_loss": min(losses) if losses else 0.0,
            "average_holding_seconds": (
                statistics.mean(holding_values) if holding_values else 0.0
            ),
            "median_holding_seconds": (
                statistics.median(holding_values) if holding_values else 0.0
            ),
            "exit_reason_distribution": dict(sorted(exit_reasons.items())),
            "trades_per_symbol": dict(sorted(trades_per_symbol.items())),
            "net_pnl_per_symbol": dict(sorted(pnl_per_symbol.items())),
        },
        "signal_funnel": {
            "raw_signals": funnel["raw_signals"],
            "qualified_signals": funnel["valid_signals"],
            "inactive_signals": funnel["inactive_signals"],
            "opportunities": funnel["opportunities_created"],
            "approved_opportunities": funnel["approved_opportunities"],
            "entries": funnel["successful_entries"],
            "closed_trades": funnel["closed_trades"] or trade_count,
            "counters": funnel,
        },
        "trade_performance": _trade_metrics(trades),
        "exit_analysis": exit_analysis,
        "strategy_analysis": strategy_analysis,
        "symbol_analysis": symbol_analysis,
        "rejection_breakdown": {
            name: {
                "count": count,
                "pct_of_named_rejections": (
                    count
                    / max(
                        1,
                        sum(
                            funnel[key]
                            for key in (
                                "opportunities_below_score_threshold",
                                "confidence_rejections",
                                "expected_edge_rejections",
                                "cooldown_rejections",
                                "reentry_rejections",
                                "stale_market_rejections",
                                "liquidity_rejections",
                                "correlation_rejections",
                                "risk_rejections",
                                "capacity_rejections",
                            )
                        ),
                    )
                    * 100.0
                ),
            }
            for name, count in funnel.items()
            if name.endswith("_rejections") or name == "opportunities_below_score_threshold"
        },
        "entry_protection": {
            "rejected_entries": int(runtime_metrics.get("rejected_entries", 0)),
            "rejected_cooldown": int(runtime_metrics.get("cooldown_rejections", 0)),
            "rejected_duplicate_or_stale_signal": int(
                runtime_metrics.get("duplicate_signal_rejections", 0)
            ),
            "rejected_insufficient_expected_edge": int(
                runtime_metrics.get("expected_edge_rejections", 0)
            ),
            "consecutive_loss_events": int(
                runtime_metrics.get("consecutive_loss_events", 0)
            ),
            "reentry_attempts_prevented": int(
                runtime_metrics.get("reentry_attempts_prevented", 0)
            ),
            "reentry_rejections": int(runtime_metrics.get("reentry_rejections", 0)),
            "early_reentries_allowed": int(runtime_metrics.get("early_reentries_allowed", 0)),
        },
        "execution": {
            "orders": len(orders),
            "fills": len(fills),
            "partial_orders": len(partial_orders),
            "nonterminal_orders": len(nonterminal_orders),
            "entry_slippage_bps": _slippage_stats(entry_slippage_bps),
            "exit_slippage_bps": _slippage_stats(exit_slippage_bps),
            "all_slippage_bps": _slippage_stats(all_slippage_bps),
        },
        "health": {
            "exceptions": int(runtime_metrics.get("exceptions", 0)),
            "persistence_errors": int(runtime_metrics.get("persistence_errors", 0)),
            "stale_feed_violation": bool(
                runtime_metrics.get("stale_feed_violation", 0)
            ),
            "stale_market_rejections": int(
                runtime_metrics.get("stale_market_rejections", 0)
            ),
            "risk_rejections": int(runtime_metrics.get("risk_rejections", 0)),
            "signals_generated": int(runtime_metrics.get("signals_generated", 0)),
            "opportunities_evaluated": int(
                runtime_metrics.get("opportunities_evaluated", 0)
            ),
        },
        "risk": {
            "circuit_breaker_active": bool(_value(risk_row, "breaker_active", 0)),
        },
        "integrity": {
            "orphan_trails": int(orphan_trails),
            "duplicate_trades": len(trade_ids) - len(set(trade_ids)),
            "duplicate_fills": len(fill_ids) - len(set(fill_ids)),
            "unmatched_fills": len(unmatched_fills),
            "nonterminal_orders": len(nonterminal_orders),
            "cash_expected": expected_cash,
            "cash_vs_expected_diff": cash_diff,
            "realized_vs_closed_trade_diff": realized_diff,
            "account_fees_vs_fill_fees_diff": fee_diff,
            "trade_reconciliation_errors": trade_reconciliation_errors,
            "accounting_invariants_pass": (
                cash_diff <= 0.01
                and realized_diff <= 0.01
                and fee_diff <= 0.01
                and trade_reconciliation_errors == 0
                and math.isfinite(final_equity)
            ),
        },
    }
    return result


def _money(value: float) -> str:
    return f"${value:,.4f}"


def print_report(report: dict[str, Any]) -> None:
    account = report["account"]
    costs = report["costs"]
    trading = report["trading"]
    protection = report["entry_protection"]
    execution = report["execution"]
    health = report["health"]
    integrity = report["integrity"]
    funnel = report.get("signal_funnel", {})
    rejection_breakdown = report.get("rejection_breakdown", {})
    trade_performance = report.get("trade_performance", trading)
    exits = report.get("exit_analysis", {})
    strategy_analysis = report.get("strategy_analysis", {})
    symbol_analysis = report.get("symbol_analysis", {})

    print("=" * 78)
    print("  PAPER TRADING SOAK REPORT — DATABASE FACTS ONLY")
    print(f"  Database: {report['db_path']}")
    print("=" * 78)
    print("\n[ ACCOUNT ]")
    print(f"  Initial balance:       {_money(account['initial_balance'])}")
    print(f"  Final equity:          {_money(account['final_equity'])}")
    print(f"  Net PnL (equity):      {_money(account['net_pnl'])}")
    print(f"  Realized net PnL:      {_money(account['realized_net_pnl'])}")
    print(f"  Unrealized PnL:        {_money(account['unrealized_pnl'])}")
    print(f"  Cash / allocated:      {_money(account['cash'])} / {_money(account['allocated'])}")
    print(f"  Open positions:        {account['open_positions']}")
    print(f"  Max drawdown:          {account['max_drawdown_pct']:.6f}%")

    print("\n[ SIGNAL FUNNEL ]")
    print(
        "  Raw → qualified → opportunities → approved → entries → closed: "
        f"{funnel.get('raw_signals', 0)} → {funnel.get('qualified_signals', 0)} → "
        f"{funnel.get('opportunities', 0)} → {funnel.get('approved_opportunities', 0)} → "
        f"{funnel.get('entries', 0)} → {funnel.get('closed_trades', 0)}"
    )
    print(f"  Inactive signals:      {funnel.get('inactive_signals', 0)}")

    print("\n[ REJECTION BREAKDOWN ]")
    for reason, details in sorted(rejection_breakdown.items()):
        print(
            f"  {reason}: {details.get('count', 0)} "
            f"({details.get('pct_of_named_rejections', 0.0):.2f}%)"
        )

    print("\n[ GROSS → COSTS → NET (CLOSED TRADES) ]")
    print(f"  Gross PnL before costs:{_money(costs['gross_pnl_before_costs_closed']):>16}")
    print(f"  Fees:                  {_money(costs['closed_trade_fees']):>16}")
    print(f"  Slippage:              {_money(costs['closed_trade_slippage']):>16}")
    print(f"  Total trading costs:   {_money(costs['closed_total_trading_costs']):>16}")
    print(f"  Net profit after costs:{_money(costs['net_realized_after_costs']):>16}")
    print("  (Account totals include costs already paid on any still-open entries.)")
    print(f"  Account total fees:    {_money(costs['account_total_fees_including_open_entries']):>16}")
    print(f"  Account total slippage:{_money(costs['account_total_slippage_including_open_entries']):>16}")

    print("\n[ TRADING ]")
    print(
        f"  Trades / wins / losses:{trading['trade_count']:>8} / "
        f"{trading['wins']} / {trading['losses']}"
    )
    print(f"  Win rate:              {trading['win_rate_pct']:.2f}%")
    pf = trading["profit_factor"]
    print(f"  Profit factor:         {'N/A (no gross loss)' if pf is None else f'{pf:.4f}'}")
    print(f"  Average win / loss:    {_money(trading['average_win'])} / {_money(trading['average_loss'])}")
    print(f"  Largest win / loss:    {_money(trading['largest_win'])} / {_money(trading['largest_loss'])}")
    print(f"  Avg / median hold:     {trading['average_holding_seconds']:.2f}s / {trading['median_holding_seconds']:.2f}s")
    print(
        "  Avg MFE / MAE:         "
        f"{trade_performance.get('mfe', {}).get('average_pct', 0.0):.4f}% / "
        f"{trade_performance.get('mae', {}).get('average_pct', 0.0):.4f}%"
    )
    print(f"  Exit reasons:          {trading['exit_reason_distribution']}")
    print(f"  Trades per symbol:     {trading['trades_per_symbol']}")

    print("\n[ ENTRY / CHURN PROTECTION ]")
    print(f"  Rejected entries:      {protection['rejected_entries']}")
    print(f"  Cooldown rejects:      {protection['rejected_cooldown']}")
    print(f"  Duplicate/stale:       {protection['rejected_duplicate_or_stale_signal']}")
    print(f"  Insufficient edge:     {protection['rejected_insufficient_expected_edge']}")
    print(f"  Re-entry prevented:    {protection['reentry_attempts_prevented']}")
    print(f"  Consecutive-loss events:{protection['consecutive_loss_events']:>7}")
    print(f"  Early re-entries allowed:{protection.get('early_reentries_allowed', 0):>6}")

    print("\n[ EXIT ANALYSIS ]")
    for reason, metrics in exits.items():
        print(
            f"  {reason}: trades={metrics.get('trade_count', 0)} "
            f"net={_money(float(metrics.get('net_pnl', 0.0)))} "
            f"expectancy={_money(float(metrics.get('expectancy', 0.0)))} "
            f"avg MFE/MAE={metrics.get('mfe', {}).get('average_pct', 0.0):.4f}%/"
            f"{metrics.get('mae', {}).get('average_pct', 0.0):.4f}%"
        )

    print("\n[ STRATEGY ANALYSIS ]")
    for strategy_id, metrics in strategy_analysis.items():
        print(
            f"  {strategy_id}: trades={metrics.get('trade_count', 0)} "
            f"win={metrics.get('win_rate_pct', 0.0):.2f}% "
            f"net={_money(float(metrics.get('net_pnl', 0.0)))} "
            f"PF={metrics.get('profit_factor', 'N/A')} "
            f"EV={_money(float(metrics.get('expectancy', 0.0)))}"
        )

    print("\n[ SYMBOL ANALYSIS ]")
    for symbol, metrics in symbol_analysis.items():
        print(
            f"  {symbol}: trades={metrics.get('trade_count', 0)} "
            f"net={_money(float(metrics.get('net_pnl', 0.0)))} "
            f"PF={metrics.get('profit_factor', 'N/A')}"
        )

    print("\n[ EXECUTION ]")
    print(f"  Orders / fills:        {execution['orders']} / {execution['fills']}")
    print(f"  Partial / nonterminal: {execution['partial_orders']} / {execution['nonterminal_orders']}")
    print(
        "  Slippage bps avg/max: "
        f"{execution['all_slippage_bps']['avg']:.4f} / "
        f"{execution['all_slippage_bps']['max']:.4f}"
    )

    print("\n[ RUNTIME HEALTH ]")
    print(f"  Exceptions:            {health['exceptions']}")
    print(f"  Persistence errors:    {health['persistence_errors']}")
    print(f"  Stale-feed violation:  {health['stale_feed_violation']}")
    print(f"  Stale-market rejects:  {health['stale_market_rejections']}")
    print(f"  Risk rejects:          {health['risk_rejections']}")
    print(
        f"  Signals / opportunities:{health['signals_generated']:>6} / "
        f"{health['opportunities_evaluated']}"
    )

    print("\n[ ACCOUNTING / INTEGRITY ]")
    print(f"  Cash reconciliation:  diff={_money(integrity['cash_vs_expected_diff'])}")
    print(f"  Realized reconciliation: diff={_money(integrity['realized_vs_closed_trade_diff'])}")
    print(f"  Fee reconciliation:   diff={_money(integrity['account_fees_vs_fill_fees_diff'])}")
    print(f"  Trade formula errors: {integrity['trade_reconciliation_errors']}")
    print(f"  Duplicate trades/fills:{integrity['duplicate_trades']} / {integrity['duplicate_fills']}")
    print(f"  Unmatched fills:       {integrity['unmatched_fills']}")
    print(f"  Orphan trails:         {integrity['orphan_trails']}")
    print(
        "  ACCOUNTING STATUS:    "
        + ("PASS" if integrity["accounting_invariants_pass"] else "FAIL")
    )
    print("=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze a paper-trading SQLite database")
    parser.add_argument("db_path", help="Path to SQLite database file")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the text report")
    args = parser.parse_args()
    try:
        results = analyze_database(args.db_path)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print_report(results)


if __name__ == "__main__":
    main()
