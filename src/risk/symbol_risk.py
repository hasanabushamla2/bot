"""Symbol-level loss budgets, cooldowns, and temporary churn lockouts."""

from __future__ import annotations

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

    @property
    def effective_loss_cooldown_seconds(self) -> float:
        return (
            self.symbol_cooldown_seconds
            if self.loss_cooldown_seconds is None
            else self.loss_cooldown_seconds
        )

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
    cooldown_until: datetime | None = None
    cooldown_multiplier: int = 1
    lockout_count: int = 0


class SymbolRiskManager:
    """Enforce per-symbol cooldown and temporary consecutive-loss protection."""

    def __init__(self, config: SymbolRiskConfig | None = None) -> None:
        self.config = config or SymbolRiskConfig()
        self._states: dict[str, SymbolState] = {}
        self.cooldown_triggers_count: int = 0
        self.reentry_blocks_count: int = 0
        self.consecutive_loss_events_count: int = 0

    def get_symbol_state(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]

    def is_symbol_eligible(
        self, symbol: str, current_equity: float, now: datetime | None = None
    ) -> tuple[bool, str]:
        now_dt = now or datetime.now(UTC)
        st = self.get_symbol_state(symbol)
        self._decay_loss_streak(st, now_dt)

        if st.cooldown_until and now_dt < st.cooldown_until:
            rem_sec = (st.cooldown_until - now_dt).total_seconds()
            self.reentry_blocks_count += 1
            return (
                False,
                f"SYMBOL_COOLDOWN_ACTIVE (remaining {rem_sec:.0f}s, "
                f"reason: {st.last_exit_reason})",
            )
        if st.cooldown_until and now_dt >= st.cooldown_until:
            st.cooldown_until = None

        if current_equity > 0:
            daily_limit = current_equity * (self.config.max_symbol_daily_loss_pct / 100.0)
            if st.daily_realized_pnl <= -daily_limit:
                return False, "SYMBOL_DAILY_LOSS_LIMIT_EXCEEDED"

            total_limit = current_equity * (self.config.max_symbol_total_loss_pct / 100.0)
            if st.total_realized_pnl <= -total_limit:
                return False, "SYMBOL_TOTAL_LOSS_LIMIT_EXCEEDED"

        return True, "ELIGIBLE"

    def record_trade_exit(
        self,
        symbol: str,
        net_pnl: float,
        exit_reason: str,
        slippage_bps: float = 0.0,
        current_equity: float = 10000.0,
        exit_time: datetime | None = None,
    ) -> None:
        now_dt = exit_time or datetime.now(UTC)
        st = self.get_symbol_state(symbol)
        self._decay_loss_streak(st, now_dt)

        st.trade_count += 1
        st.daily_realized_pnl += net_pnl
        st.total_realized_pnl += net_pnl
        st.last_exit_time = now_dt
        st.last_exit_reason = exit_reason
        st.last_slippage_bps = slippage_bps
        st.last_trade_profitable = net_pnl > 0

        if net_pnl > 0:
            st.win_count += 1
            st.consecutive_losses = 0
            st.stopout_count = 0
            st.cooldown_multiplier = 1
            cooldown_sec = self.config.win_cooldown_seconds
        else:
            st.loss_count += 1
            st.consecutive_losses += 1
            cooldown_sec = self.config.effective_loss_cooldown_seconds

        is_stop = exit_reason in ("hard_stop", "stop_loss")
        if is_stop:
            st.stopout_count += 1
            multiplier = min(8, 2 ** max(0, st.stopout_count - 1))
            st.cooldown_multiplier = multiplier
            cooldown_sec = max(
                cooldown_sec,
                self.config.effective_loss_cooldown_seconds * multiplier,
            )

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
                cooldown_seconds=cooldown_sec,
                cooldown_until=st.cooldown_until.isoformat(),
            )

    def _decay_loss_streak(self, state: SymbolState, now: datetime) -> None:
        if state.last_exit_time is None or state.consecutive_losses <= 0:
            return
        elapsed = (now - state.last_exit_time).total_seconds()
        if elapsed >= self.config.loss_streak_reset_seconds:
            state.consecutive_losses = 0
            state.stopout_count = 0
            state.cooldown_multiplier = 1

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
                "last_exit_time": (
                    state.last_exit_time.isoformat() if state.last_exit_time else None
                ),
                "last_exit_reason": state.last_exit_reason,
                "last_trade_profitable": state.last_trade_profitable,
                "last_slippage_bps": state.last_slippage_bps,
                "cooldown_until": (
                    state.cooldown_until.isoformat() if state.cooldown_until else None
                ),
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
            state.cooldown_until = self._parse_time(raw.get("cooldown_until"))
            state.cooldown_multiplier = int(raw.get("cooldown_multiplier", 1))
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
