"""Durable signal freshness and consumed-signal protection.

Strategies in this project are predicate based: while a condition remains true
``analyze`` can return a new Python object on every scan.  Object timestamps are
therefore not sufficient trade identity.  This guard turns one continuous true
condition into one durable signal regime and only rearms it after the strategy
has observed the condition as false (or an event-driven strategy supplies a
new explicit ``signal_id``).

The guard also carries a monotonically increasing sequence and observation
count.  Adaptive re-entry uses those fields to distinguish a genuinely new
setup from a repeated predicate that merely happens to have a new object.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.strategies.base import StrategySignal


@dataclass
class SignalRegimeState:
    key: str
    active: bool = False
    direction: str = ""
    current_signal_id: str = ""
    activated_at: datetime | None = None
    sequence: int = 0
    observations: int = 0
    last_consumed_signal_id: str = ""
    last_consumed_sequence: int = 0
    last_consumed_at: datetime | None = None


class EntrySignalGuard:
    """Assign signal-regime ids and reject a regime already used for entry."""

    def __init__(self) -> None:
        self._states: dict[str, SignalRegimeState] = {}

    @staticmethod
    def key(strategy_id: str, symbol: str | None) -> str:
        return f"{strategy_id}|{symbol or 'unknown'}"

    def observe_cycle(
        self,
        signals: Iterable[StrategySignal],
        evaluated_keys: Iterable[str],
        now: datetime | None = None,
    ) -> list[StrategySignal]:
        """Observe one complete strategy-evaluation cycle.

        A key that was evaluated but produced no signal is explicitly rearmed.
        Keys not evaluated (for example because market data was unavailable)
        are left unchanged and cannot accidentally become "fresh".
        """
        now_dt = now or datetime.now(UTC)
        observed: list[StrategySignal] = []
        seen: set[str] = set()

        for signal in signals:
            signal_key = self.key(signal.strategy_id, signal.symbol)
            seen.add(signal_key)
            state = self._states.setdefault(signal_key, SignalRegimeState(key=signal_key))
            direction = signal.direction.value
            guard_assigned = bool(signal.metadata.get("_guard_assigned_signal_id"))
            explicit_id = "" if guard_assigned else signal.signal_id

            is_new_regime = not state.active or state.direction != direction
            if explicit_id and explicit_id != state.current_signal_id:
                # Event-driven plugins may provide a stable event identity.
                is_new_regime = True

            if is_new_regime:
                state.sequence += 1
                state.observations = 1
                state.active = True
                state.direction = direction
                state.activated_at = signal.timestamp or now_dt
                state.current_signal_id = explicit_id or self._make_signal_id(
                    signal_key, direction, state.sequence, state.activated_at
                )
            else:
                # Repeated objects from a still-true predicate belong to the
                # same sequence.  This count is a live persistence signal, not
                # a future-candle confirmation.
                state.observations += 1

            signal.signal_id = state.current_signal_id
            signal.metadata["signal_id"] = state.current_signal_id
            signal.metadata["signal_sequence"] = state.sequence
            signal.metadata["signal_observations"] = state.observations
            signal.metadata["signal_timestamp"] = (
                state.activated_at.isoformat() if state.activated_at else now_dt.isoformat()
            )
            signal.metadata["_guard_assigned_signal_id"] = True
            observed.append(signal)

        for signal_key in set(evaluated_keys) - seen:
            state = self._states.setdefault(signal_key, SignalRegimeState(key=signal_key))
            state.active = False
            state.direction = ""
            state.current_signal_id = ""
            state.activated_at = None
            state.observations = 0

        return observed

    def is_consumed(self, signal: StrategySignal) -> bool:
        state = self._states.get(self.key(signal.strategy_id, signal.symbol))
        return bool(
            state
            and signal.signal_id
            and state.last_consumed_signal_id == signal.signal_id
        )

    def can_enter(self, signal: StrategySignal) -> tuple[bool, str]:
        if not signal.signal_id:
            return False, "SIGNAL_ID_MISSING"
        if self.is_consumed(signal):
            return False, "DUPLICATE_OR_STALE_SIGNAL"
        return True, "FRESH_SIGNAL"

    def is_new_sequence(self, signal: StrategySignal) -> bool:
        """Return whether this signal sequence is newer than the consumed one."""
        state = self._states.get(self.key(signal.strategy_id, signal.symbol))
        if state is None or not signal.signal_id:
            return False
        try:
            sequence = int(signal.metadata.get("signal_sequence", state.sequence))
        except (TypeError, ValueError):
            return signal.signal_id != state.last_consumed_signal_id
        return sequence > state.last_consumed_sequence and signal.signal_id != state.last_consumed_signal_id

    def record_consumed(
        self, signal: StrategySignal, consumed_at: datetime | None = None
    ) -> None:
        if not signal.signal_id:
            raise ValueError("Cannot consume a signal without signal_id")
        signal_key = self.key(signal.strategy_id, signal.symbol)
        state = self._states.setdefault(signal_key, SignalRegimeState(key=signal_key))
        state.last_consumed_signal_id = signal.signal_id
        try:
            state.last_consumed_sequence = int(signal.metadata.get("signal_sequence", state.sequence))
        except (TypeError, ValueError):
            state.last_consumed_sequence = state.sequence
        state.last_consumed_at = consumed_at or datetime.now(UTC)

    def get_state(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, state in self._states.items():
            result[key] = {
                "active": state.active,
                "direction": state.direction,
                "current_signal_id": state.current_signal_id,
                "activated_at": state.activated_at.isoformat() if state.activated_at else None,
                "sequence": state.sequence,
                "observations": state.observations,
                "last_consumed_signal_id": state.last_consumed_signal_id,
                "last_consumed_sequence": state.last_consumed_sequence,
                "last_consumed_at": (
                    state.last_consumed_at.isoformat() if state.last_consumed_at else None
                ),
            }
        return result

    def restore_state(self, data: dict[str, dict[str, Any]]) -> None:
        self._states.clear()
        for key, raw in data.items():
            self._states[key] = SignalRegimeState(
                key=key,
                active=bool(raw.get("active", False)),
                direction=str(raw.get("direction", "")),
                current_signal_id=str(raw.get("current_signal_id", "")),
                activated_at=self._parse_time(raw.get("activated_at")),
                sequence=int(raw.get("sequence", 0)),
                observations=int(raw.get("observations", 0)),
                last_consumed_signal_id=str(raw.get("last_consumed_signal_id", "")),
                last_consumed_sequence=int(raw.get("last_consumed_sequence", 0)),
                last_consumed_at=self._parse_time(raw.get("last_consumed_at")),
            )

    @staticmethod
    def _make_signal_id(
        key: str, direction: str, sequence: int, activated_at: datetime
    ) -> str:
        raw = f"{key}|{direction}|{sequence}|{activated_at.isoformat()}"
        return f"sig-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
