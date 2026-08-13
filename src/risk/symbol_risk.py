"""Symbol-level loss budgets and adaptive, evidence-aware re-entry protection.

The manager keeps hard-stop protection intentionally non-bypassable.  A fresh
high-quality signal may only shorten the *profit-trailing* cooldown, and only
when it is materially stronger and the live structure still supports the same
direction.  Daily/total loss budgets remain absolute safety controls.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SymbolRiskConfig:
    max_symbol_daily_loss_pct: float = 1.5
    max_symbol_total_loss_pct: float = 3.0
    max_consecutive_symbol_losses: int = 2
    max_symbol_stopouts: int = 2
    symbol_cooldown_seconds: float = 300.0
    symbol_extended_cooldown_seconds: float = 1800.0
    max_slippage_for_cooldown_bps: float = 30.0

    # Explicit lifecycle settings.  Optional values retain compatibility with
    # callers that use the original field names above.
    loss_cooldown_seconds: float | None = None
    win_cooldown_seconds: float = 30.0
    max_consecutive_losses_per_symbol: int | None = None
    symbol_lockout_seconds: float | None = None
    loss_streak_reset_seconds: float = 21600.0

    # Adaptive re-entry controls.  These are normalized quality requirements,
    # not symbol-specific price constants.
    profit_trail_early_reentry_enabled: bool = True
    material_confidence_improvement: float = 0.10
    base_market_structure_score: float = 0.55
    reference_volatility_pct: float = 0.30
    volatility_confidence_scale: float = 0.08
    volatility_structure_scale: float = 0.08
    hard_stop_volatility_scale: float = 0.50
    hard_stop_loss_return_reference_pct: float = 0.30
    max_cooldown_multiplier: float = 8.0

    @property
    def effective_loss_cooldown_seconds(self) -> float:
        return self.symbol_cooldown_seconds if self.loss_cooldown_seconds is None else self.loss_cooldown_seconds

    @property
    def effective_max_consecutive_losses(self) -> int:
        return (
            self.max_consecutive_symbol_losses
            if self.max_consecutive_losses_per_symbol is None
            else self.max_consecutive_losses_per_symbol
        )

    @property
    def effective_lockout_seconds(self) -> float:
        return (
            self.symbol_extended_cooldown_seconds
            if self.symbol_lockout_seconds is None
            else self.symbol_lockout_seconds
        )


@dataclass
class ReentryContext:
    """Facts available *at entry time* for an adaptive re-entry decision."""

    symbol: str
    direction: str
    strategy_id: str
    confidence: float
    signal_id: str = ""
    signal_sequence: int = 0
    fresh_signal: bool = False
    market_structure_score: float = 0.0
    market_volatility_pct: float = 0.0


@dataclass
class ReentryDecision:
    allowed: bool
    reason: str
    remaining_seconds: float = 0.0
    early_reentry: bool = False
    required_confidence: float = 0.0
    required_market_structure: float = 0.0


@dataclass
class SymbolState:
    symbol: str
    daily_realized_pnl: float = 0.0
    total_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    stopout_count: int = 0
    trade_count: int = 0
    loss_count: int = 0
    win_count: int = 0
    last_exit_time: datetime | None = None
    last_exit_reason: str = ""
    last_trade_profitable: bool | None = None
    last_slippage_bps: float = 0.0
    last_exit_net_pnl: float = 0.0
    last_exit_return_pct: float = 0.0
    last_direction: str = ""
    last_strategy_id: str = ""
    last_entry_confidence: float | None = None
    last_signal_id: str = ""
    last_signal_sequence: int = 0
    last_market_volatility_pct: float = 0.0
    cooldown_until: datetime | None = None
    cooldown_multiplier: float = 1.0
    lockout_count: int = 0


class SymbolRiskManager:
    """Enforce per-symbol budgets and adaptive re-entry protection.

    ``is_symbol_eligible`` remains a backward-compatible pure safety check.
    New paper-entry code should call ``evaluate_entry`` with a
    :class:`ReentryContext`, which can distinguish a normal fresh setup from a
    repeated setup during a profit cooldown.
    """

    def __init__(self, config: SymbolRiskConfig | None = None) -> None:
        self.config = config or SymbolRiskConfig()
        self._states: dict[str, SymbolState] = {}
        self.cooldown_triggers_count: int = 0
        self.reentry_blocks_count: int = 0
        self.early_reentries_allowed_count: int = 0
        self.consecutive_loss_events_count: int = 0

    def get_symbol_state(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]

    def is_symbol_eligible(
        self, symbol: str, current_equity: float, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Backward-compatible safety eligibility without an early override."""
        decision = self._base_eligibility(symbol, current_equity, now)
        return decision.allowed, decision.reason

    def evaluate_entry(
        self,
        context: ReentryContext,
        current_equity: float,
        now: datetime | None = None,
    ) -> ReentryDecision:
        """Evaluate a specific proposed entry against adaptive cooldown rules.

        A hard-stop/loss cooldown cannot be bypassed.  For a profitable trailing
        exit, the ordinary short cooldown is bypassable only by a new sequence
        that is materially stronger than the last accepted signal and remains
        structurally aligned after volatility normalization.
        """
        now_dt = now or datetime.now(UTC)
        st = self.get_symbol_state(context.symbol)
        self._decay_loss_streak(st, now_dt)

        budget_decision = self._budget_eligibility(st, current_equity)
        if not budget_decision.allowed:
            return budget_decision

        if not st.cooldown_until or now_dt >= st.cooldown_until:
            if st.cooldown_until and now_dt >= st.cooldown_until:
                st.cooldown_until = None
            return ReentryDecision(True, "ELIGIBLE")

        remaining = max(0.0, (st.cooldown_until - now_dt).total_seconds())
        last_exit_is_hard_loss = (
            st.last_exit_reason in ("hard_stop", "stop_loss") or st.last_trade_profitable is False
        )
        if last_exit_is_hard_loss:
            self.reentry_blocks_count += 1
            return ReentryDecision(
                False,
                f"HARD_STOP_COOLDOWN_ACTIVE (remaining {remaining:.0f}s, reason: {st.last_exit_reason})",
                remaining_seconds=remaining,
            )

        is_trailing_profit = st.last_trade_profitable is True and st.last_exit_reason in {
            "trail_hit",
            "trailing_stop",
        }
        if not is_trailing_profit or not self.config.profit_trail_early_reentry_enabled:
            self.reentry_blocks_count += 1
            return ReentryDecision(
                False,
                f"SYMBOL_COOLDOWN_ACTIVE (remaining {remaining:.0f}s, reason: {st.last_exit_reason})",
                remaining_seconds=remaining,
            )

        volatility_ratio = self._volatility_ratio(context.market_volatility_pct)
        previous_confidence = st.last_entry_confidence if st.last_entry_confidence is not None else 0.0
        strategy_penalty = 0.02 if st.last_strategy_id and context.strategy_id != st.last_strategy_id else 0.0
        required_confidence = min(
            1.0,
            previous_confidence
            + self.config.material_confidence_improvement
            + strategy_penalty
            + volatility_ratio * self.config.volatility_confidence_scale,
        )
        required_structure = min(
            1.0,
            self.config.base_market_structure_score
            + volatility_ratio * self.config.volatility_structure_scale,
        )
        same_direction = bool(st.last_direction) and context.direction == st.last_direction
        new_sequence = self._is_genuinely_new_sequence(st, context)

        if (
            context.fresh_signal
            and new_sequence
            and same_direction
            and context.confidence >= required_confidence
            and context.market_structure_score >= required_structure
        ):
            self.early_reentries_allowed_count += 1
            logger.info(
                "adaptive_profit_reentry_allowed",
                symbol=context.symbol,
                strategy_id=context.strategy_id,
                confidence=round(context.confidence, 4),
                required_confidence=round(required_confidence, 4),
                structure=round(context.market_structure_score, 4),
                required_structure=round(required_structure, 4),
                remaining_cooldown_seconds=round(remaining, 2),
            )
            return ReentryDecision(
                True,
                "EARLY_REENTRY_NEW_HIGH_QUALITY_SEQUENCE",
                remaining_seconds=remaining,
                early_reentry=True,
                required_confidence=required_confidence,
                required_market_structure=required_structure,
            )

        self.reentry_blocks_count += 1
        failed: list[str] = []
        if not context.fresh_signal or not new_sequence:
            failed.append("new_signal_sequence")
        if not same_direction:
            failed.append("direction")
        if context.confidence < required_confidence:
            failed.append("confidence")
        if context.market_structure_score < required_structure:
            failed.append("market_structure")
        return ReentryDecision(
            False,
            "REENTRY_QUALITY_NOT_MET ("
            + ",".join(failed or ["profit_cooldown"])
            + f"; remaining {remaining:.0f}s)",
            remaining_seconds=remaining,
            required_confidence=required_confidence,
            required_market_structure=required_structure,
        )

    def record_trade_exit(
        self,
        symbol: str,
        net_pnl: float,
        exit_reason: str,
        slippage_bps: float = 0.0,
        current_equity: float = 10_000.0,
        exit_time: datetime | None = None,
        *,
        return_pct: float = 0.0,
        direction: str = "",
        strategy_id: str = "",
        entry_confidence: float | None = None,
        signal_id: str = "",
        signal_sequence: int = 0,
        market_volatility_pct: float = 0.0,
    ) -> None:
        """Record an exit and derive a cooldown from realized risk facts."""
        now_dt = exit_time or datetime.now(UTC)
        st = self.get_symbol_state(symbol)
        self._decay_loss_streak(st, now_dt)

        st.trade_count += 1
        st.daily_realized_pnl += net_pnl
        st.total_realized_pnl += net_pnl
        st.last_exit_time = now_dt
        st.last_exit_reason = exit_reason
        st.last_slippage_bps = slippage_bps
        st.last_exit_net_pnl = net_pnl
        st.last_exit_return_pct = return_pct
        st.last_direction = direction or st.last_direction
        st.last_strategy_id = strategy_id or st.last_strategy_id
        st.last_entry_confidence = entry_confidence
        st.last_signal_id = signal_id or st.last_signal_id
        st.last_signal_sequence = max(0, int(signal_sequence))
        st.last_market_volatility_pct = max(0.0, market_volatility_pct)
        st.last_trade_profitable = net_pnl > 0

        if net_pnl > 0:
            st.win_count += 1
            st.consecutive_losses = 0
            st.stopout_count = 0
            st.cooldown_multiplier = 1.0
            cooldown_sec = self.config.win_cooldown_seconds
        else:
            st.loss_count += 1
            st.consecutive_losses += 1
            cooldown_sec = self.config.effective_loss_cooldown_seconds

        is_stop = exit_reason in ("hard_stop", "stop_loss")
        if is_stop:
            st.stopout_count += 1
            stopout_multiplier = min(
                self.config.max_cooldown_multiplier,
                2.0 ** max(0, st.stopout_count - 1),
            )
            loss_severity = self._loss_severity_multiplier(return_pct)
            volatility_multiplier = 1.0 + self._volatility_ratio(market_volatility_pct) * self.config.hard_stop_volatility_scale
            st.cooldown_multiplier = stopout_multiplier * loss_severity * volatility_multiplier
            cooldown_sec = max(
                cooldown_sec,
                self.config.effective_loss_cooldown_seconds * st.cooldown_multiplier,
            )
        elif net_pnl <= 0:
            # A non-stop loss still has a meaningful cooldown, but does not
            # receive a hidden global lockout merely because it was a small,
            # controlled exit.
            st.cooldown_multiplier = self._loss_severity_multiplier(return_pct)
            cooldown_sec *= st.cooldown_multiplier

        threshold = self.config.effective_max_consecutive_losses
        if st.consecutive_losses >= threshold:
            cooldown_sec = max(cooldown_sec, self.config.effective_lockout_seconds)
            st.lockout_count += 1
            if st.consecutive_losses == threshold:
                self.consecutive_loss_events_count += 1

        if current_equity > 0:
            daily_limit = current_equity * (self.config.max_symbol_daily_loss_pct / 100.0)
            if st.daily_realized_pnl <= -daily_limit:
                cooldown_sec = max(cooldown_sec, self.config.effective_lockout_seconds)

        if cooldown_sec > 0:
            new_until = now_dt + timedelta(seconds=cooldown_sec)
            if st.cooldown_until is None or new_until > st.cooldown_until:
                st.cooldown_until = new_until
            self.cooldown_triggers_count += 1
            logger.warning(
                "symbol_cooldown_activated",
                symbol=symbol,
                reason=exit_reason,
                profitable=st.last_trade_profitable,
                consecutive_losses=st.consecutive_losses,
                stopouts=st.stopout_count,
                cooldown_seconds=round(cooldown_sec, 3),
                cooldown_until=st.cooldown_until.isoformat(),
            )

    def _base_eligibility(
        self, symbol: str, current_equity: float, now: datetime | None
    ) -> ReentryDecision:
        now_dt = now or datetime.now(UTC)
        st = self.get_symbol_state(symbol)
        self._decay_loss_streak(st, now_dt)
        budget = self._budget_eligibility(st, current_equity)
        if not budget.allowed:
            return budget
        if st.cooldown_until and now_dt < st.cooldown_until:
            remaining = (st.cooldown_until - now_dt).total_seconds()
            self.reentry_blocks_count += 1
            return ReentryDecision(
                False,
                f"SYMBOL_COOLDOWN_ACTIVE (remaining {remaining:.0f}s, reason: {st.last_exit_reason})",
                remaining_seconds=remaining,
            )
        if st.cooldown_until and now_dt >= st.cooldown_until:
            st.cooldown_until = None
        return ReentryDecision(True, "ELIGIBLE")

    def _budget_eligibility(self, state: SymbolState, current_equity: float) -> ReentryDecision:
        if current_equity <= 0:
            return ReentryDecision(True, "ELIGIBLE")
        daily_limit = current_equity * (self.config.max_symbol_daily_loss_pct / 100.0)
        if state.daily_realized_pnl <= -daily_limit:
            return ReentryDecision(False, "SYMBOL_DAILY_LOSS_LIMIT_EXCEEDED")
        total_limit = current_equity * (self.config.max_symbol_total_loss_pct / 100.0)
        if state.total_realized_pnl <= -total_limit:
            return ReentryDecision(False, "SYMBOL_TOTAL_LOSS_LIMIT_EXCEEDED")
        return ReentryDecision(True, "ELIGIBLE")

    def _is_genuinely_new_sequence(self, state: SymbolState, context: ReentryContext) -> bool:
        # Guard sequences are scoped to strategy+symbol.  Compare sequence
        # numbers only within the same strategy; a different strategy must
        # still present a different durable signal id and pays the configured
        # confidence penalty in ``evaluate_entry``.
        if context.strategy_id == state.last_strategy_id:
            if context.signal_sequence > 0 and state.last_signal_sequence > 0:
                return context.signal_sequence > state.last_signal_sequence
        return bool(context.signal_id and context.signal_id != state.last_signal_id)

    def _volatility_ratio(self, volatility_pct: float) -> float:
        if not math.isfinite(volatility_pct) or volatility_pct <= 0:
            return 0.0
        reference = max(self.config.reference_volatility_pct, 1e-9)
        # Cap only the *adjustment*, not the observed value itself.  This keeps
        # a one-off data spike from yielding an unbounded wall-clock lockout.
        return min(2.0, volatility_pct / reference)

    def _loss_severity_multiplier(self, return_pct: float) -> float:
        if not math.isfinite(return_pct) or return_pct >= 0:
            return 1.0
        reference = max(self.config.hard_stop_loss_return_reference_pct, 1e-9)
        return min(2.0, max(1.0, abs(return_pct) / reference))

    def _decay_loss_streak(self, state: SymbolState, now: datetime) -> None:
        if state.last_exit_time is None or state.consecutive_losses <= 0:
            return
        elapsed = (now - state.last_exit_time).total_seconds()
        if elapsed >= self.config.loss_streak_reset_seconds:
            state.consecutive_losses = 0
            state.stopout_count = 0
            state.cooldown_multiplier = 1.0

    def reset_daily(self) -> None:
        for state in self._states.values():
            state.daily_realized_pnl = 0.0

    def get_state(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for symbol, state in self._states.items():
            result[symbol] = {
                "daily_realized_pnl": state.daily_realized_pnl,
                "total_realized_pnl": state.total_realized_pnl,
                "consecutive_losses": state.consecutive_losses,
                "stopout_count": state.stopout_count,
                "trade_count": state.trade_count,
                "loss_count": state.loss_count,
                "win_count": state.win_count,
                "last_exit_time": state.last_exit_time.isoformat() if state.last_exit_time else None,
                "last_exit_reason": state.last_exit_reason,
                "last_trade_profitable": state.last_trade_profitable,
                "last_slippage_bps": state.last_slippage_bps,
                "last_exit_net_pnl": state.last_exit_net_pnl,
                "last_exit_return_pct": state.last_exit_return_pct,
                "last_direction": state.last_direction,
                "last_strategy_id": state.last_strategy_id,
                "last_entry_confidence": state.last_entry_confidence,
                "last_signal_id": state.last_signal_id,
                "last_signal_sequence": state.last_signal_sequence,
                "last_market_volatility_pct": state.last_market_volatility_pct,
                "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
                "cooldown_multiplier": state.cooldown_multiplier,
                "lockout_count": state.lockout_count,
            }
        return result

    def restore_state(self, data: dict[str, Any]) -> None:
        self._states.clear()
        for symbol, raw in data.items():
            state = self.get_symbol_state(symbol)
            state.daily_realized_pnl = float(raw.get("daily_realized_pnl", 0.0))
            state.total_realized_pnl = float(raw.get("total_realized_pnl", 0.0))
            state.consecutive_losses = int(raw.get("consecutive_losses", 0))
            state.stopout_count = int(raw.get("stopout_count", 0))
            state.trade_count = int(raw.get("trade_count", 0))
            state.loss_count = int(raw.get("loss_count", 0))
            state.win_count = int(raw.get("win_count", 0))
            state.last_exit_time = self._parse_time(raw.get("last_exit_time"))
            state.last_exit_reason = str(raw.get("last_exit_reason", ""))
            profitable = raw.get("last_trade_profitable")
            state.last_trade_profitable = profitable if isinstance(profitable, bool) else None
            state.last_slippage_bps = float(raw.get("last_slippage_bps", 0.0))
            state.last_exit_net_pnl = float(raw.get("last_exit_net_pnl", 0.0))
            state.last_exit_return_pct = float(raw.get("last_exit_return_pct", 0.0))
            state.last_direction = str(raw.get("last_direction", ""))
            state.last_strategy_id = str(raw.get("last_strategy_id", ""))
            entry_confidence = raw.get("last_entry_confidence")
            state.last_entry_confidence = (
                float(entry_confidence) if entry_confidence is not None else None
            )
            state.last_signal_id = str(raw.get("last_signal_id", ""))
            state.last_signal_sequence = int(raw.get("last_signal_sequence", 0))
            state.last_market_volatility_pct = float(raw.get("last_market_volatility_pct", 0.0))
            state.cooldown_until = self._parse_time(raw.get("cooldown_until"))
            state.cooldown_multiplier = float(raw.get("cooldown_multiplier", 1.0))
            state.lockout_count = int(raw.get("lockout_count", 0))

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
