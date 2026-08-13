"""Paper-session reporting helpers.

The helpers are pure and operate on closed-trade objects, making session
reports reproducible and usable by both the orchestrator and offline tools.
"""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median
from typing import Any, Iterable

from src.paper.account import ClosedTrade


def _round(value: float, digits: int = 4) -> float:
    return round(float(value), digits)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile)))
    return ordered[index]


def trade_metrics(trades: Iterable[ClosedTrade]) -> dict[str, Any]:
    """Calculate cost-aware performance and excursion metrics for a trade set."""
    rows = list(trades)
    net = [float(t.net_pnl) for t in rows]
    gross = [float(t.gross_pnl) for t in rows]
    fees = [float(t.fees) for t in rows]
    slippage = [float(t.slippage_cost) for t in rows]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value <= 0]
    mfe = [float(t.max_favorable_excursion_pct) for t in rows]
    mae = [float(t.max_adverse_excursion_pct) for t in rows]
    hold = [max(0.0, float(t.holding_seconds)) for t in rows]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    return {
        "trade_count": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": _round(len(wins) / len(rows) * 100.0 if rows else 0.0, 4),
        "gross_pnl": _round(sum(gross)),
        "fees": _round(sum(fees)),
        "slippage": _round(sum(slippage)),
        "net_pnl": _round(sum(net)),
        "profit_factor": _round(profit_factor) if profit_factor is not None else None,
        "expectancy": _round(sum(net) / len(rows) if rows else 0.0, 6),
        "average_winner": _round(sum(wins) / len(wins) if wins else 0.0),
        "average_loser": _round(sum(losses) / len(losses) if losses else 0.0),
        "largest_winner": _round(max(wins) if wins else 0.0),
        "largest_loser": _round(min(losses) if losses else 0.0),
        "mfe": {
            "average_pct": _round(sum(mfe) / len(mfe) if mfe else 0.0, 6),
            "median_pct": _round(median(mfe) if mfe else 0.0, 6),
            "p95_pct": _round(_percentile(mfe, 0.95), 6),
        },
        "mae": {
            "average_pct": _round(sum(mae) / len(mae) if mae else 0.0, 6),
            "median_pct": _round(median(mae) if mae else 0.0, 6),
            "p95_pct": _round(_percentile(mae, 0.95), 6),
        },
        "holding_duration": {
            "average_seconds": _round(sum(hold) / len(hold) if hold else 0.0, 4),
            "median_seconds": _round(median(hold) if hold else 0.0, 4),
            "p95_seconds": _round(_percentile(hold, 0.95), 4),
        },
    }


def grouped_trade_metrics(trades: Iterable[ClosedTrade], field_name: str) -> dict[str, dict[str, Any]]:
    """Group closed trades by a dataclass field and calculate uniform metrics."""
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        value = getattr(trade, field_name, "")
        groups[str(value or "unknown")].append(trade)
    return {key: trade_metrics(value) for key, value in sorted(groups.items())}


def classify_hard_stop_entries(trades: Iterable[ClosedTrade]) -> dict[str, int]:
    """Classify stop-outs by observed excursion and relative holding time.

    This is post-trade diagnostics only; it does not feed future information
    back into the entry signal.  The short-hold boundary is a session-local
    lower quartile, avoiding a hard-coded number of seconds.
    """
    hard = [trade for trade in trades if trade.exit_reason in {"hard_stop", "stop_loss"}]
    if not hard:
        return {}
    hold_threshold = _percentile([max(0.0, trade.holding_seconds) for trade in hard], 0.25)
    labels: dict[str, int] = defaultdict(int)
    for trade in hard:
        mfe = max(0.0, trade.max_favorable_excursion_pct)
        mae = abs(min(0.0, trade.max_adverse_excursion_pct))
        short_lived = trade.holding_seconds <= hold_threshold
        if mfe <= 0.0:
            label = "short_lived_no_favorable_excursion" if short_lived else "no_favorable_excursion"
        elif mfe < mae:
            label = "short_lived_limited_follow_through" if short_lived else "limited_follow_through"
        else:
            label = "reversal_after_favorable_excursion"
        labels[label] += 1
    return dict(sorted(labels.items()))


def exit_analysis(trades: Iterable[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """Separate hard-stop, trail-hit, and all remaining exits."""
    rows = list(trades)
    hard = [trade for trade in rows if trade.exit_reason in {"hard_stop", "stop_loss"}]
    trail = [trade for trade in rows if trade.exit_reason == "trail_hit"]
    other = [trade for trade in rows if trade not in hard and trade not in trail]
    hard_metrics = trade_metrics(hard)
    hard_metrics["entry_classification"] = classify_hard_stop_entries(hard)
    return {
        "hard_stop": hard_metrics,
        "trail_hit": trade_metrics(trail),
        "other": trade_metrics(other),
    }


def format_session_report(report: dict[str, Any]) -> str:
    """Render the required end-of-paper-session diagnostics as readable text."""
    funnel = report.get("signal_funnel", {})
    performance = report.get("trade_performance", {})
    throughput = report.get("throughput", {})
    lines = [
        "=" * 78,
        "PAPER SESSION REPORT",
        "=" * 78,
        "SIGNAL FUNNEL",
        (
            "raw signals → qualified signals → opportunities → approved opportunities "
            "→ entries → closed trades: "
            f"{funnel.get('raw_signals', 0)} → {funnel.get('qualified_signals', 0)} → "
            f"{funnel.get('opportunities', 0)} → {funnel.get('approved_opportunities', 0)} → "
            f"{funnel.get('entries', 0)} → {funnel.get('closed_trades', 0)}"
        ),
        "REJECTION BREAKDOWN",
    ]
    for reason, value in report.get("rejection_breakdown", {}).items():
        lines.append(
            f"  {reason}: {value.get('count', 0)} "
            f"({value.get('pct_of_all_rejections', 0.0):.2f}%)"
        )
    lines.extend(
        [
            "TRADE PERFORMANCE",
            (
                f"  wins/losses={performance.get('wins', 0)}/{performance.get('losses', 0)} "
                f"win rate={performance.get('win_rate_pct', 0.0):.2f}% "
                f"gross=${performance.get('gross_pnl', 0.0):.4f} "
                f"fees=${performance.get('fees', 0.0):.4f} "
                f"slippage=${performance.get('slippage', 0.0):.4f} "
                f"net=${performance.get('net_pnl', 0.0):.4f} "
                f"PF={performance.get('profit_factor', 'N/A')} "
                f"expectancy=${performance.get('expectancy', 0.0):.4f}"
            ),
            (
                "  MFE/MAE avg: "
                f"{performance.get('mfe', {}).get('average_pct', 0.0):.4f}% / "
                f"{performance.get('mae', {}).get('average_pct', 0.0):.4f}% | "
                f"avg hold={performance.get('holding_duration', {}).get('average_seconds', 0.0):.2f}s"
            ),
            "EXIT ANALYSIS",
        ]
    )
    for reason, value in report.get("exit_analysis", {}).items():
        lines.append(
            f"  {reason}: trades={value.get('trade_count', 0)} "
            f"net=${value.get('net_pnl', 0.0):.4f} "
            f"expectancy=${value.get('expectancy', 0.0):.4f}"
        )
        if reason == "hard_stop" and value.get("entry_classification"):
            lines.append(f"    entry classification: {value['entry_classification']}")
    lines.append("STRATEGY ANALYSIS")
    for strategy_id, value in report.get("strategy_analysis", {}).items():
        lines.append(
            f"  {strategy_id}: trades={value.get('trade_count', value.get('trades', 0))} "
            f"net=${value.get('net_pnl', 0.0):.4f} PF={value.get('profit_factor', 'N/A')} "
            f"EV=${value.get('expected_value', value.get('expectancy', 0.0)):.4f}"
        )
    lines.append("SYMBOL ANALYSIS")
    for symbol, value in report.get("symbol_analysis", {}).items():
        lines.append(
            f"  {symbol}: trades={value.get('trade_count', 0)} "
            f"net=${value.get('net_pnl', 0.0):.4f} PF={value.get('profit_factor', 'N/A')}"
        )
    lines.extend(
        [
            "THROUGHPUT / RISK HEALTH",
            (
                f"  opportunities/hour={throughput.get('opportunities_per_hour', 0.0):.2f} "
                f"qualified/hour={throughput.get('qualified_opportunities_per_hour', 0.0):.2f} "
                f"entries/hour={throughput.get('entries_per_hour', 0.0):.2f} "
                f"net expectancy=${throughput.get('net_expectancy', 0.0):.4f} "
                f"PF={throughput.get('profit_factor', 'N/A')} "
                f"drawdown={throughput.get('max_drawdown_pct', 0.0):.4f}%"
            ),
            "=" * 78,
        ]
    )
    return "\n".join(lines)


def finite_report_value(value: Any) -> Any:
    """Convert non-finite float artifacts to ``None`` before JSON/report use."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_report_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [finite_report_value(item) for item in value]
    return value
