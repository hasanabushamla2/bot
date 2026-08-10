"""Latency, Slippage, and Execution Health Monitors for micro-live trading."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class LatencyRecord:
    symbol: str = ""
    side: str = ""
    t0_decision: datetime = field(default_factory=lambda: datetime.now(UTC))
    t1_submit: datetime | None = None
    t2_ack: datetime | None = None
    t3_first_fill: datetime | None = None
    t4_final_fill: datetime | None = None
    decision_to_submit_ms: float = 0.0
    submit_to_ack_ms: float = 0.0
    ack_to_first_fill_ms: float = 0.0
    total_execution_ms: float = 0.0


@dataclass
class SlippageRecord:
    symbol: str = ""
    side: str = ""
    strategy_id: str = ""
    expected_price: float = 0.0
    actual_fill_price: float = 0.0
    quantity: float = 0.0
    slippage_bps: float = 0.0
    spread_at_submit: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class StopExecutionRecord:
    symbol: str = ""
    entry_price: float = 0.0
    target_stop_price: float = 0.0
    target_stop_pct: float = 0.30
    trigger_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    actual_fill_price: float = 0.0
    actual_price_move_pct: float = 0.0
    slippage_pct: float = 0.0
    fees: float = 0.0
    net_realized_loss: float = 0.0
    latency_ms: float = 0.0


class LatencyMonitor:
    def __init__(self, max_samples: int = 1000) -> None:
        self._records: deque[LatencyRecord] = deque(maxlen=max_samples)

    def start_measure(self, symbol: str, side: str) -> LatencyRecord:
        rec = LatencyRecord(symbol=symbol, side=side)
        self._records.append(rec)
        return rec

    def avg_total_ms(self) -> float:
        vals = [r.total_execution_ms for r in self._records if r.total_execution_ms > 0]
        return sum(vals) / len(vals) if vals else 0.0

    def p95_total_ms(self) -> float:
        vals = sorted([r.total_execution_ms for r in self._records if r.total_execution_ms > 0])
        return vals[int(len(vals) * 0.95)] if vals else 0.0

    def worst_ms(self) -> float:
        vals = [r.total_execution_ms for r in self._records if r.total_execution_ms > 0]
        return max(vals) if vals else 0.0

    def count(self) -> int:
        return len(self._records)


class SlippageMonitor:
    def __init__(self, max_samples: int = 1000) -> None:
        self._records: deque[SlippageRecord] = deque(maxlen=max_samples)

    def record(
        self,
        symbol: str,
        side: str,
        expected: float,
        actual: float,
        qty: float,
        strategy: str = "",
        spread: float = 0.0,
    ) -> SlippageRecord:
        bps = (actual - expected) / expected * 10000 if expected > 0 else 0
        if side == "buy":
            bps = abs(bps)
        rec = SlippageRecord(
            symbol=symbol,
            side=side,
            strategy_id=strategy,
            expected_price=expected,
            actual_fill_price=actual,
            quantity=qty,
            slippage_bps=bps,
            spread_at_submit=spread,
        )
        self._records.append(rec)
        return rec

    def avg_bps(self) -> float:
        vals = [r.slippage_bps for r in self._records]
        return sum(vals) / len(vals) if vals else 0.0

    def worst_bps(self) -> float:
        vals = [r.slippage_bps for r in self._records]
        return max(vals) if vals else 0.0

    def count(self) -> int:
        return len(self._records)


class StopExecutionAudit:
    def __init__(self, max_samples: int = 500) -> None:
        self._records: deque[StopExecutionRecord] = deque(maxlen=max_samples)

    def record(
        self,
        symbol: str,
        entry: float,
        target_stop: float,
        actual_fill: float,
        fees: float = 0.0,
        latency_ms: float = 0.0,
    ) -> StopExecutionRecord:
        target_pct = abs(entry - target_stop) / entry * 100 if entry > 0 else 0
        actual_pct = (actual_fill - entry) / entry * 100 if entry > 0 else 0
        slip = abs(actual_fill - target_stop) / entry * 100 if entry > 0 else 0
        rec = StopExecutionRecord(
            symbol=symbol,
            entry_price=entry,
            target_stop_price=target_stop,
            target_stop_pct=target_pct,
            actual_fill_price=actual_fill,
            actual_price_move_pct=actual_pct,
            slippage_pct=slip,
            fees=fees,
            net_realized_loss=abs(actual_pct) + fees,
            latency_ms=latency_ms,
        )
        self._records.append(rec)
        return rec

    def avg_slippage_pct(self) -> float:
        vals = [r.slippage_pct for r in self._records]
        return sum(vals) / len(vals) if vals else 0.0

    def worst_slippage_pct(self) -> float:
        vals = [r.slippage_pct for r in self._records]
        return max(vals) if vals else 0.0

    def count(self) -> int:
        return len(self._records)

    def all_records(self) -> list[StopExecutionRecord]:
        return list(self._records)


class CircuitBreakerState:
    def __init__(self) -> None:
        self.active = False
        self.reason = ""
        self.tripped_at: datetime | None = None
        self.consecutive_rejections = 0
        self.duplicate_anomalies = 0
        self.balance_mismatches = 0
        self.stop_slippage_violations = 0
        self.latency_violations = 0

    def trip(self, reason: str) -> None:
        self.active = True
        self.reason = reason
        self.tripped_at = datetime.now(UTC)

    def reset(self) -> None:
        self.active = False
        self.reason = ""
        self.tripped_at = None
        self.consecutive_rejections = 0
        self.duplicate_anomalies = 0
        self.balance_mismatches = 0
        self.stop_slippage_violations = 0
        self.latency_violations = 0
