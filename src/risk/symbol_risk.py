"""Symbol-Level Risk Budget & Re-entry Cooldown Engine.

Enforces:
1. Max daily & total loss limits per symbol.
2. Max consecutive losses & hard-stop limits per symbol.
3. Temporary symbol cooldowns with exponential backoff.
4. Immediate re-entry prevention after hard stops or large slippage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SymbolRiskConfig:
    max_symbol_daily_loss_pct: float = 1.5  # Max 1.5% account equity loss per day per symbol
    max_symbol_total_loss_pct: float = 3.0  # Max 3.0% account equity total loss per symbol
    max_consecutive_symbol_losses: int = 2  # Cooldown after 2 consecutive losses on one symbol
    max_symbol_stopouts: int = 2  # Extended cooldown after 2 stopouts
    symbol_cooldown_seconds: float = 300.0  # 5 min basic cooldown
    symbol_extended_cooldown_seconds: float = 1800.0  # 30 min extended cooldown
    max_slippage_for_cooldown_bps: float = 30.0  # Large slippage exit triggers cooldown


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
    last_slippage_bps: float = 0.0
    cooldown_until: datetime | None = None
    cooldown_multiplier: int = 1


class SymbolRiskManager:
    """Tracks and enforces symbol-level loss budgets, stopout limits, and re-entry cooldowns."""

    def __init__(self, config: SymbolRiskConfig | None = None) -> None:
        self.config = config or SymbolRiskConfig()
        self._states: dict[str, SymbolState] = {}
        self.cooldown_triggers_count: int = 0
        self.reentry_blocks_count: int = 0

    def get_symbol_state(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]

    def is_symbol_eligible(
        self, symbol: str, current_equity: float, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Check if symbol is currently eligible to open a new position."""
        now_dt = now or datetime.now(UTC)
        st = self.get_symbol_state(symbol)

        # 1. Cooldown check
        if st.cooldown_until and now_dt < st.cooldown_until:
            rem_sec = (st.cooldown_until - now_dt).total_seconds()
            self.reentry_blocks_count += 1
            return False, f"SYMBOL_COOLDOWN_ACTIVE (remaining {rem_sec:.0f}s, reason: {st.last_exit_reason})"

        # 2. Daily loss limit check
        if current_equity > 0:
            daily_loss_limit = current_equity * (self.config.max_symbol_daily_loss_pct / 100.0)
            if st.daily_realized_pnl <= -daily_loss_limit:
                return False, f"SYMBOL_DAILY_LOSS_LIMIT_EXCEEDED (pnl: ${st.daily_realized_pnl:.2f} <= -${daily_loss_limit:.2f})"

            total_loss_limit = current_equity * (self.config.max_symbol_total_loss_pct / 100.0)
            if st.total_realized_pnl <= -total_loss_limit:
                return False, f"SYMBOL_TOTAL_LOSS_LIMIT_EXCEEDED (pnl: ${st.total_realized_pnl:.2f} <= -${total_loss_limit:.2f})"

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
        """Record trade exit and trigger cooldowns if appropriate."""
        now_dt = exit_time or datetime.now(UTC)
        st = self.get_symbol_state(symbol)

        st.trade_count += 1
        st.daily_realized_pnl += net_pnl
        st.total_realized_pnl += net_pnl
        st.last_exit_time = now_dt
        st.last_exit_reason = exit_reason
        st.last_slippage_bps = slippage_bps

        if net_pnl > 0:
            st.win_count += 1
            st.consecutive_losses = 0
            st.cooldown_multiplier = 1
        else:
            st.loss_count += 1
            st.consecutive_losses += 1

        is_stop = exit_reason in ("hard_stop", "stop_loss")
        if is_stop:
            st.stopout_count += 1

        # Determine if cooldown should be triggered
        cooldown_sec = 0.0
        if is_stop or slippage_bps > self.config.max_slippage_for_cooldown_bps:
            # Exponential backoff on repeated stopouts
            mult = min(8, 2 ** (st.stopout_count - 1)) if st.stopout_count > 0 else 1
            cooldown_sec = self.config.symbol_cooldown_seconds * mult
            st.cooldown_multiplier = mult
        elif st.consecutive_losses >= self.config.max_consecutive_symbol_losses:
            cooldown_sec = self.config.symbol_extended_cooldown_seconds

        # Daily loss limit breach triggers extended cooldown
        daily_loss_limit = current_equity * (self.config.max_symbol_daily_loss_pct / 100.0)
        if st.daily_realized_pnl <= -daily_loss_limit:
            cooldown_sec = max(cooldown_sec, self.config.symbol_extended_cooldown_seconds)

        if cooldown_sec > 0:
            st.cooldown_until = now_dt + timedelta(seconds=cooldown_sec)
            self.cooldown_triggers_count += 1
            logger.warning(
                "symbol_cooldown_activated",
                symbol=symbol,
                reason=exit_reason,
                consecutive_losses=st.consecutive_losses,
                stopouts=st.stopout_count,
                cooldown_seconds=cooldown_sec,
                cooldown_until=st.cooldown_until.isoformat(),
            )

    def reset_daily(self) -> None:
        """Reset daily PnL counters (e.g. at midnight UTC)."""
        for st in self._states.values():
            st.daily_realized_pnl = 0.0

    def get_state(self) -> dict[str, Any]:
        """Serialize state for persistence."""
        res: dict[str, Any] = {}
        for sym, st in self._states.items():
            res[sym] = {
                "daily_realized_pnl": st.daily_realized_pnl,
                "total_realized_pnl": st.total_realized_pnl,
                "consecutive_losses": st.consecutive_losses,
                "stopout_count": st.stopout_count,
                "trade_count": st.trade_count,
                "loss_count": st.loss_count,
                "win_count": st.win_count,
                "last_exit_reason": st.last_exit_reason,
                "cooldown_until": st.cooldown_until.isoformat() if st.cooldown_until else None,
            }
        return res

    def restore_state(self, data: dict[str, Any]) -> None:
        """Restore state from persistence."""
        for sym, d in data.items():
            st = self.get_symbol_state(sym)
            st.daily_realized_pnl = d.get("daily_realized_pnl", 0.0)
            st.total_realized_pnl = d.get("total_realized_pnl", 0.0)
            st.consecutive_losses = d.get("consecutive_losses", 0)
            st.stopout_count = d.get("stopout_count", 0)
            st.trade_count = d.get("trade_count", 0)
            st.loss_count = d.get("loss_count", 0)
            st.win_count = d.get("win_count", 0)
            st.last_exit_reason = d.get("last_exit_reason", "")
            cd_str = d.get("cooldown_until")
            if cd_str:
                try:
                    st.cooldown_until = datetime.fromisoformat(cd_str)
                except Exception:
                    st.cooldown_until = None
