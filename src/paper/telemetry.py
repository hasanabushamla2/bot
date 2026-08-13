"""Structured, durable signal-to-entry telemetry for paper trading.

The paper runner deliberately separates the *funnel* (where an item made it)
from the *primary rejection reason* (why it did not proceed).  Funnel counters
are explicit rather than inferred from order counts, which makes a high raw
signal count auditable even when positions, capacity, or a safety gate prevent
execution.

Counters are cumulative for the persisted paper account.  A caller that needs
per-process rates can divide them by the current session duration; the
orchestrator exposes those rates in its final report.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


# These names are a stable reporting contract.  Keep every requested stage
# present even when its value is zero, so downstream dashboards never need to
# guess whether a stage was omitted or simply had no events.
FUNNEL_COUNTER_NAMES: tuple[str, ...] = (
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
    # Additional named stages prevent the former silent paths from being
    # hidden behind one of the required aggregate counters.
    "qualified_opportunities",
    "approved_opportunities",
    "closed_trades",
)


_REASON_TO_COUNTER: dict[str, tuple[str, ...]] = {
    "confidence": ("confidence_rejections",),
    "expected_edge": ("expected_edge_rejections",),
    "cooldown": ("cooldown_rejections",),
    "reentry": ("reentry_rejections",),
    "stale_market": ("stale_market_rejections",),
    "liquidity": ("liquidity_rejections",),
    # A wide spread is a liquidity failure as well as its own reportable
    # category.  The primary-reason breakdown remains non-overlapping.
    "spread": ("spread_rejections", "liquidity_rejections"),
    "correlation": ("correlation_rejections",),
    "risk": ("risk_rejections",),
    "capacity": ("capacity_rejections",),
    "below_score": ("opportunities_below_score_threshold",),
}


@dataclass
class SignalFunnelTelemetry:
    """Collect signal-funnel counts and explain every discarded candidate.

    ``rejection_reasons`` is intentionally a primary-reason ledger.  A spread
    rejection increments both its aggregate funnel counters, but occurs once
    in this ledger, making percentages add to 100%.
    """

    counters: Counter[str] = field(
        default_factory=lambda: Counter({name: 0 for name in FUNNEL_COUNTER_NAMES})
    )
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    rejection_by_strategy: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    rejection_by_symbol: dict[str, Counter[str]] = field(
        default_factory=lambda: defaultdict(Counter)
    )
    entry_rejections: int = 0

    def increment(self, counter: str, amount: int = 1) -> None:
        if amount <= 0:
            return
        self.counters[counter] += int(amount)

    def reject(
        self,
        reason: str,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        entry_attempt: bool = False,
    ) -> None:
        """Record one primary rejection plus any aggregate stage counters."""
        normalized = str(reason).strip().lower() or "unknown"
        self.rejection_reasons[normalized] += 1
        for counter in _REASON_TO_COUNTER.get(normalized, ()):
            self.increment(counter)
        if strategy_id:
            self.rejection_by_strategy[str(strategy_id)][normalized] += 1
        if symbol:
            self.rejection_by_symbol[str(symbol)][normalized] += 1
        if entry_attempt:
            self.entry_rejections += 1

    @property
    def total_rejections(self) -> int:
        return int(sum(self.rejection_reasons.values()))

    def funnel(self) -> dict[str, int]:
        """Return every stable funnel counter, including zero-valued stages."""
        return {name: int(self.counters.get(name, 0)) for name in FUNNEL_COUNTER_NAMES}

    def rejection_breakdown(self) -> dict[str, dict[str, float | int]]:
        total = self.total_rejections
        return {
            reason: {
                "count": int(count),
                "pct_of_all_rejections": round((count / total * 100.0) if total else 0.0, 4),
            }
            for reason, count in sorted(self.rejection_reasons.items())
        }

    def per_strategy_rejections(self) -> dict[str, dict[str, int]]:
        return {
            strategy: {reason: int(count) for reason, count in sorted(counter.items())}
            for strategy, counter in sorted(self.rejection_by_strategy.items())
        }

    def per_symbol_rejections(self) -> dict[str, dict[str, int]]:
        return {
            symbol: {reason: int(count) for reason, count in sorted(counter.items())}
            for symbol, counter in sorted(self.rejection_by_symbol.items())
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "counters": self.funnel(),
            "rejection_reasons": {k: int(v) for k, v in self.rejection_reasons.items()},
            "rejection_by_strategy": self.per_strategy_rejections(),
            "rejection_by_symbol": self.per_symbol_rejections(),
            "entry_rejections": int(self.entry_rejections),
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Restore a previous telemetry snapshot without dropping new keys."""
        self.counters = Counter({name: 0 for name in FUNNEL_COUNTER_NAMES})
        raw_counters = state.get("counters", {}) if isinstance(state, dict) else {}
        if isinstance(raw_counters, dict):
            for name, value in raw_counters.items():
                try:
                    self.counters[str(name)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue

        self.rejection_reasons = Counter()
        raw_reasons = state.get("rejection_reasons", {}) if isinstance(state, dict) else {}
        if isinstance(raw_reasons, dict):
            for reason, value in raw_reasons.items():
                try:
                    self.rejection_reasons[str(reason)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue

        self.rejection_by_strategy = defaultdict(Counter)
        raw_by_strategy = state.get("rejection_by_strategy", {}) if isinstance(state, dict) else {}
        if isinstance(raw_by_strategy, dict):
            for strategy, reasons in raw_by_strategy.items():
                if not isinstance(reasons, dict):
                    continue
                for reason, value in reasons.items():
                    try:
                        self.rejection_by_strategy[str(strategy)][str(reason)] = max(0, int(value))
                    except (TypeError, ValueError):
                        continue

        self.rejection_by_symbol = defaultdict(Counter)
        raw_by_symbol = state.get("rejection_by_symbol", {}) if isinstance(state, dict) else {}
        if isinstance(raw_by_symbol, dict):
            for symbol, reasons in raw_by_symbol.items():
                if not isinstance(reasons, dict):
                    continue
                for reason, value in reasons.items():
                    try:
                        self.rejection_by_symbol[str(symbol)][str(reason)] = max(0, int(value))
                    except (TypeError, ValueError):
                        continue

        try:
            self.entry_rejections = max(0, int(state.get("entry_rejections", 0)))
        except (AttributeError, TypeError, ValueError):
            self.entry_rejections = 0

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> SignalFunnelTelemetry:
        telemetry = cls()
        telemetry.restore(state)
        return telemetry
