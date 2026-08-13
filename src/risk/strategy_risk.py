"""Strategy-level risk, performance, and evidence-aware allocation telemetry.

Strategy allocation is never disabled from a handful of outcomes.  A reduction
is available only after a configurable sample size and a conservative negative
expectancy confidence bound.  It is a sizing reduction, not a global risk
relaxation or a strategy kill switch.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class StrategyRiskConfig:
    max_strategy_consecutive_losses: int = 4
    max_strategy_daily_loss_pct: float = 5.0
    strategy_cooldown_seconds: float = 600.0
    min_trades_for_allocation_adjustment: int = 30
    allocation_confidence_z: float = 1.64  # one-sided ~95% confidence
    minimum_allocation_multiplier: float = 0.50
    negative_expectancy_allocation_multiplier: float = 0.75


@dataclass
class StrategyState:
    strategy_id: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    net_pnl_sum_squares: float = 0.0
    total_fees: float = 0.0
    total_slippage: float = 0.0
    hard_stop_count: int = 0
    trail_count: int = 0
    win_pnl_total: float = 0.0
    loss_pnl_total: float = 0.0
    mfe_total_pct: float = 0.0
    mae_total_pct: float = 0.0
    holding_seconds_total: float = 0.0
    rejection_reasons: Counter[str] = field(default_factory=Counter)
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
        if self.loss_pnl_total < 0:
            return self.win_pnl_total / abs(self.loss_pnl_total)
        return 99.0 if self.win_pnl_total > 0 else 1.0

    @property
    def expectancy(self) -> float:
        return self.net_pnl / self.trades if self.trades > 0 else 0.0

    @property
    def average_mfe_pct(self) -> float:
        return self.mfe_total_pct / self.trades if self.trades else 0.0

    @property
    def average_mae_pct(self) -> float:
        return self.mae_total_pct / self.trades if self.trades else 0.0

    @property
    def average_holding_seconds(self) -> float:
        return self.holding_seconds_total / self.trades if self.trades else 0.0

    def expectancy_upper_confidence_bound(self, z_score: float) -> float | None:
        """One-sided upper bound of mean net PnL, or ``None`` without variance."""
        if self.trades < 2:
            return None
        mean = self.expectancy
        # Numerically safe sample variance from running sums.
        variance_numerator = self.net_pnl_sum_squares - self.trades * mean * mean
        variance = max(0.0, variance_numerator / max(1, self.trades - 1))
        standard_error = math.sqrt(variance / self.trades)
        return mean + z_score * standard_error


class StrategyRiskManager:
    """Tracks strategy-level safety and statistically cautious allocation data."""

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
        now_dt = now or datetime.now(UTC)
        st = self.get_strategy_state(strategy_id)
        if st.cooldown_until and now_dt < st.cooldown_until:
            rem_sec = (st.cooldown_until - now_dt).total_seconds()
            return False, f"STRATEGY_COOLDOWN_ACTIVE (remaining {rem_sec:.0f}s)"
        if st.cooldown_until and now_dt >= st.cooldown_until:
            st.cooldown_until = None
        if current_equity > 0:
            daily_loss_limit = current_equity * (self.config.max_strategy_daily_loss_pct / 100.0)
            if st.net_pnl <= -daily_loss_limit:
                return False, (
                    "STRATEGY_DAILY_LOSS_LIMIT_EXCEEDED "
                    f"(pnl: ${st.net_pnl:.2f} <= -${daily_loss_limit:.2f})"
                )
        return True, "ELIGIBLE"

    def record_rejection(self, strategy_id: str, reason: str) -> None:
        """Keep filtering telemetry with the strategy that generated a candidate."""
        if strategy_id:
            self.get_strategy_state(strategy_id).rejection_reasons[str(reason)] += 1

    def record_trade_exit(
        self,
        strategy_id: str,
        gross_pnl: float,
        net_pnl: float,
        fees: float,
        slippage: float,
        exit_reason: str,
        current_equity: float = 10_000.0,
        exit_time: datetime | None = None,
        *,
        mfe_pct: float = 0.0,
        mae_pct: float = 0.0,
        holding_seconds: float = 0.0,
    ) -> None:
        now_dt = exit_time or datetime.now(UTC)
        st = self.get_strategy_state(strategy_id)

        st.trades += 1
        st.gross_pnl += gross_pnl
        st.net_pnl += net_pnl
        st.net_pnl_sum_squares += net_pnl * net_pnl
        st.total_fees += fees
        st.total_slippage += slippage
        st.mfe_total_pct += mfe_pct
        st.mae_total_pct += mae_pct
        st.holding_seconds_total += max(0.0, holding_seconds)

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

        cooldown_sec = 0.0
        if st.consecutive_losses >= self.config.max_strategy_consecutive_losses:
            cooldown_sec = self.config.strategy_cooldown_seconds
        daily_loss_limit = current_equity * (self.config.max_strategy_daily_loss_pct / 100.0)
        if current_equity > 0 and st.net_pnl <= -daily_loss_limit:
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

    def allocation_multiplier(self, strategy_id: str) -> float:
        """Return a non-increasing sizing multiplier backed by enough evidence.

        No evidence, a small sample, neutral/positive performance, or an
        uncertain negative mean all return 1.0.  A proven-negative strategy is
        reduced but remains enabled, preserving exploration and avoiding a
        win-rate-only optimisation.
        """
        st = self.get_strategy_state(strategy_id)
        if st.trades < self.config.min_trades_for_allocation_adjustment:
            return 1.0
        upper_bound = st.expectancy_upper_confidence_bound(self.config.allocation_confidence_z)
        if upper_bound is None or upper_bound >= 0.0:
            return 1.0
        return max(
            self.config.minimum_allocation_multiplier,
            min(1.0, self.config.negative_expectancy_allocation_multiplier),
        )

    def allocation_evidence(self, strategy_id: str) -> dict[str, Any]:
        st = self.get_strategy_state(strategy_id)
        upper_bound = st.expectancy_upper_confidence_bound(self.config.allocation_confidence_z)
        enough = st.trades >= self.config.min_trades_for_allocation_adjustment
        return {
            "sample_size": st.trades,
            "minimum_sample_size": self.config.min_trades_for_allocation_adjustment,
            "expectancy_upper_confidence_bound": upper_bound,
            "statistically_actionable": bool(enough and upper_bound is not None and upper_bound < 0.0),
            "allocation_multiplier": self.allocation_multiplier(strategy_id),
        }

    def get_summary(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for sid, st in self._states.items():
            evidence = self.allocation_evidence(sid)
            result[sid] = {
                "trade_count": st.trades,
                "trades": st.trades,  # backwards-compatible alias
                "wins": st.wins,
                "losses": st.losses,
                "win_rate": round(st.win_rate, 2),
                "gross_pnl": round(st.gross_pnl, 4),
                "net_pnl": round(st.net_pnl, 4),
                "fees": round(st.total_fees, 4),
                "slippage": round(st.total_slippage, 4),
                "avg_win": round(st.avg_win, 4),
                "avg_loss": round(st.avg_loss, 4),
                "profit_factor": round(st.profit_factor, 4),
                "expected_value": round(st.expectancy, 6),
                "expectancy": round(st.expectancy, 6),
                "average_mfe_pct": round(st.average_mfe_pct, 6),
                "average_mae_pct": round(st.average_mae_pct, 6),
                "average_hold_seconds": round(st.average_holding_seconds, 4),
                "hard_stops": st.hard_stop_count,
                "trails": st.trail_count,
                "rejection_reasons": {
                    reason: int(count) for reason, count in sorted(st.rejection_reasons.items())
                },
                "allocation_evidence": evidence,
            }
        return result

    def get_state(self) -> dict[str, Any]:
        """Serialize strategy telemetry for paper-process restart continuity."""
        return {
            sid: {
                "trades": st.trades,
                "wins": st.wins,
                "losses": st.losses,
                "consecutive_losses": st.consecutive_losses,
                "gross_pnl": st.gross_pnl,
                "net_pnl": st.net_pnl,
                "net_pnl_sum_squares": st.net_pnl_sum_squares,
                "total_fees": st.total_fees,
                "total_slippage": st.total_slippage,
                "hard_stop_count": st.hard_stop_count,
                "trail_count": st.trail_count,
                "win_pnl_total": st.win_pnl_total,
                "loss_pnl_total": st.loss_pnl_total,
                "mfe_total_pct": st.mfe_total_pct,
                "mae_total_pct": st.mae_total_pct,
                "holding_seconds_total": st.holding_seconds_total,
                "rejection_reasons": dict(st.rejection_reasons),
                "cooldown_until": st.cooldown_until.isoformat() if st.cooldown_until else None,
            }
            for sid, st in self._states.items()
        }

    def restore_state(self, data: dict[str, Any]) -> None:
        self._states.clear()
        for strategy_id, raw in data.items():
            if not isinstance(raw, dict):
                continue
            st = self.get_strategy_state(str(strategy_id))
            st.trades = int(raw.get("trades", 0))
            st.wins = int(raw.get("wins", 0))
            st.losses = int(raw.get("losses", 0))
            st.consecutive_losses = int(raw.get("consecutive_losses", 0))
            st.gross_pnl = float(raw.get("gross_pnl", 0.0))
            st.net_pnl = float(raw.get("net_pnl", 0.0))
            st.net_pnl_sum_squares = float(raw.get("net_pnl_sum_squares", 0.0))
            st.total_fees = float(raw.get("total_fees", 0.0))
            st.total_slippage = float(raw.get("total_slippage", 0.0))
            st.hard_stop_count = int(raw.get("hard_stop_count", 0))
            st.trail_count = int(raw.get("trail_count", 0))
            st.win_pnl_total = float(raw.get("win_pnl_total", 0.0))
            st.loss_pnl_total = float(raw.get("loss_pnl_total", 0.0))
            st.mfe_total_pct = float(raw.get("mfe_total_pct", 0.0))
            st.mae_total_pct = float(raw.get("mae_total_pct", 0.0))
            st.holding_seconds_total = float(raw.get("holding_seconds_total", 0.0))
            reasons = raw.get("rejection_reasons", {})
            if isinstance(reasons, dict):
                st.rejection_reasons = Counter(
                    {str(reason): int(count) for reason, count in reasons.items()}
                )
            st.cooldown_until = self._parse_time(raw.get("cooldown_until"))

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            return None
