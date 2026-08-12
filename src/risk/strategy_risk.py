"""Strategy-Level Risk & Performance Tracker.

Tracks:
- Trades, wins, losses, win rate
- Gross PnL, Net PnL, fees, slippage cost
- Average win, average loss, profit factor, expectancy
- Hard stops and trailing exits
- Consecutive losses and strategy-level circuit breakers/cooldowns
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyRiskConfig:
    max_strategy_consecutive_losses: int = 4  # Cooldown after 4 consecutive losses on a single strategy
    max_strategy_daily_loss_pct: float = 5.0  # Max 5% account equity loss per strategy
    strategy_cooldown_seconds: float = 600.0  # 10 min strategy cooldown


@dataclass
class StrategyState:
    strategy_id: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    hard_stop_count: int = 0
    trail_count: int = 0
    win_pnl_total: float = 0.0
    loss_pnl_total: float = 0.0
    cooldown_until: datetime | None = None

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades > 0 else 0.0

    @property
    def avg_win(self) -> float:
        return (self.win_pnl_total / self.wins) if self.wins > 0 else 0.0

    @property
    def avg_loss(self) -> float:
        return (self.loss_pnl_total / self.losses) if self.losses > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        return (self.win_pnl_total / abs(self.loss_pnl_total)) if self.loss_pnl_total < 0 else (99.0 if self.win_pnl_total > 0 else 1.0)

    @property
    def expectancy(self) -> float:
        if self.trades == 0:
            return 0.0
        return self.net_pnl / self.trades


class StrategyRiskManager:
    """Tracks and enforces strategy-level risk, performance, and cooldowns."""

    def __init__(self, config: StrategyRiskConfig | None = None) -> None:
        self.config = config or StrategyRiskConfig()
        self._states: dict[str, StrategyState] = {}
        self.strategy_cooldown_triggers_count: int = 0

    def get_strategy_state(self, strategy_id: str) -> StrategyState:
        if strategy_id not in self._states:
            self._states[strategy_id] = StrategyState(strategy_id=strategy_id)
        return self._states[strategy_id]

    def is_strategy_eligible(
        self, strategy_id: str, current_equity: float, now: datetime | None = None
    ) -> tuple[bool, str]:
        """Check if strategy is currently allowed to open new positions."""
        now_dt = now or datetime.now(UTC)
        st = self.get_strategy_state(strategy_id)

        # 1. Cooldown check
        if st.cooldown_until and now_dt < st.cooldown_until:
            rem_sec = (st.cooldown_until - now_dt).total_seconds()
            return False, f"STRATEGY_COOLDOWN_ACTIVE (remaining {rem_sec:.0f}s)"

        # 2. Daily loss limit check
        if current_equity > 0:
            daily_loss_limit = current_equity * (self.config.max_strategy_daily_loss_pct / 100.0)
            if st.net_pnl <= -daily_loss_limit:
                return False, f"STRATEGY_DAILY_LOSS_LIMIT_EXCEEDED (pnl: ${st.net_pnl:.2f} <= -${daily_loss_limit:.2f})"

        return True, "ELIGIBLE"

    def record_trade_exit(
        self,
        strategy_id: str,
        gross_pnl: float,
        net_pnl: float,
        fees: float,
        slippage: float,
        exit_reason: str,
        current_equity: float = 10000.0,
        exit_time: datetime | None = None,
    ) -> None:
        """Record trade exit for a strategy and trigger cooldown if limits exceeded."""
        now_dt = exit_time or datetime.now(UTC)
        st = self.get_strategy_state(strategy_id)

        st.trades += 1
        st.gross_pnl += gross_pnl
        st.net_pnl += net_pnl
        st.total_fees += fees
        st.total_slippage += slippage

        if exit_reason in ("hard_stop", "stop_loss"):
            st.hard_stop_count += 1
        elif exit_reason == "trail_hit":
            st.trail_count += 1

        if net_pnl > 0:
            st.wins += 1
            st.win_pnl_total += net_pnl
            st.consecutive_losses = 0
        else:
            st.losses += 1
            st.loss_pnl_total += net_pnl
            st.consecutive_losses += 1

        # Check consecutive losses or daily loss limit
        cooldown_sec = 0.0
        if st.consecutive_losses >= self.config.max_strategy_consecutive_losses:
            cooldown_sec = self.config.strategy_cooldown_seconds
        daily_loss_limit = current_equity * (self.config.max_strategy_daily_loss_pct / 100.0)
        if st.net_pnl <= -daily_loss_limit:
            cooldown_sec = max(cooldown_sec, self.config.strategy_cooldown_seconds)

        if cooldown_sec > 0:
            st.cooldown_until = now_dt + timedelta(seconds=cooldown_sec)
            self.strategy_cooldown_triggers_count += 1
            logger.warning(
                "strategy_cooldown_activated",
                strategy_id=strategy_id,
                consecutive_losses=st.consecutive_losses,
                net_pnl=round(st.net_pnl, 2),
                cooldown_seconds=cooldown_sec,
            )

    def get_summary(self) -> dict[str, dict[str, Any]]:
        """Return dict of per-strategy summary statistics."""
        res: dict[str, dict[str, Any]] = {}
        for sid, st in self._states.items():
            res[sid] = {
                "trades": st.trades,
                "wins": st.wins,
                "losses": st.losses,
                "win_rate": round(st.win_rate, 2),
                "gross_pnl": round(st.gross_pnl, 2),
                "net_pnl": round(st.net_pnl, 2),
                "fees": round(st.total_fees, 4),
                "slippage": round(st.total_slippage, 4),
                "avg_win": round(st.avg_win, 2),
                "avg_loss": round(st.avg_loss, 2),
                "profit_factor": round(st.profit_factor, 2),
                "expectancy": round(st.expectancy, 4),
                "hard_stops": st.hard_stop_count,
                "trails": st.trail_count,
            }
        return res
