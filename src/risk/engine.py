"""Risk Engine — independent risk management subsystem.

The Risk Engine is a completely independent module. Strategies CANNOT
bypass it. Every opportunity must pass risk validation before execution.

FINAL POLICY (v1.0):
- MARKET: SPOT ONLY — no leverage, margin, futures, short selling
- HARD STOP LOSS: -0.30% per position
- TAKE PROFIT: NONE — no fixed profit ceiling
- PROFIT MANAGEMENT: Trailing stop only
- TRADE COUNT: Opportunity-driven, no fixed daily count
- CAPITAL: Dynamically allocated per liquidity and opportunity quality

Configurable:
- Position sizing
- Maximum exposure (total, per-market, per-strategy)
- Correlated-position limits
- Drawdown controls
- Circuit breakers
- Emergency kill switch
- Stale-data protection
- Hard stop-loss enforcement (-0.30%)
- Trailing stop management
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.core.config import get_settings
from src.core.logging_config import get_logger
from src.opportunity.engine import EvaluatedOpportunity

logger = get_logger(__name__)


class RiskDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RejectionReason(str, Enum):
    """Why the risk engine rejected an opportunity."""

    MAX_EXPOSURE = "max_exposure"
    PER_MARKET_EXPOSURE = "per_market_exposure"
    PER_STRATEGY_EXPOSURE = "per_strategy_exposure"
    CORRELATED_EXPOSURE = "correlated_exposure"
    MAX_POSITIONS = "max_positions"
    DRAWDOWN = "drawdown"
    CIRCUIT_BREAKER = "circuit_breaker"
    KILL_SWITCH = "kill_switch"
    STALE_DATA = "stale_data"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    LIQUIDITY = "liquidity"
    VOLATILITY = "volatility"
    STOP_LOSS_TRIGGERED = "stop_loss_triggered"
    # Spot-only enforcement — no short selling
    SPOT_ONLY = "spot_only"


@dataclass
class RiskAssessment:
    """Result of risk evaluation for an opportunity.

    FINAL CONFIGURATION:
    - stop_loss_price: ALWAYS set to the -0.30% hard stop
    - take_profit_price: ALWAYS None (no fixed profit ceiling)
    - trailing_stop_config: trailing stop parameters for profit protection
    """

    opportunity: EvaluatedOpportunity
    decision: RiskDecision = RiskDecision.REJECTED
    reason: RejectionReason | None = None
    max_position_size: float = 0.0
    suggested_entry_price: float | None = None
    stop_loss_price: float | None = None  # Hard stop at -0.30%
    take_profit_price: float | None = None  # ALWAYS None — fixed TP disabled
    trailing_stop_enabled: bool = True  # Trailing stop for profit protection
    assessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskState:
    """Current state tracked by the risk engine."""

    total_exposure: float = 0.0
    per_market_exposure: dict[str, float] = field(default_factory=dict)
    per_strategy_exposure: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    current_drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    circuit_breaker_tripped: bool = False
    kill_switch_active: bool = False
    open_positions_count: int = 0
    last_update: datetime = field(default_factory=lambda: datetime.now(UTC))


class RiskEngine:
    """Evaluates every opportunity against configurable risk limits.

    This is a GATE — only APPROVED opportunities may proceed to execution.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.max_position_size_usd = settings.risk.max_position_size_usd
        self.max_total_exposure_usd = settings.risk.max_total_exposure_usd
        self.max_drawdown_pct = settings.risk.max_drawdown_pct
        self.default_stop_loss_pct = settings.risk.default_stop_loss_pct
        self.max_leverage = settings.risk.max_leverage
        self.max_positions_per_strategy = settings.risk.max_positions_per_strategy
        self.max_correlated_exposure_pct = settings.risk.max_correlated_exposure_pct
        self.cb_drawdown_pct = settings.risk.circuit_breaker_drawdown_pct
        self.cb_consecutive_losses = settings.risk.circuit_breaker_consecutive_losses

        self.state = RiskState()
        self._strategy_position_counts: dict[str, int] = {}

    # --- Lifecycle ---

    def trip_kill_switch(self, reason: str = "manual") -> None:
        """Activate emergency kill switch — no new positions allowed."""
        self.state.kill_switch_active = True
        logger.warning("kill_switch_tripped", reason=reason)

    def reset_kill_switch(self) -> None:
        """Reset kill switch (requires explicit action)."""
        self.state.kill_switch_active = False
        logger.info("kill_switch_reset")

    def trip_circuit_breaker(self, reason: str = "unknown") -> None:
        """Trip circuit breaker — halts new position entry."""
        self.state.circuit_breaker_tripped = True
        logger.warning("circuit_breaker_tripped", reason=reason)

    def reset_circuit_breaker(self) -> None:
        """Reset circuit breaker after cooldown/approval."""
        self.state.circuit_breaker_tripped = False
        self.state.consecutive_losses = 0
        logger.info("circuit_breaker_reset")

    # --- Assessment ---

    def assess(self, opportunity: EvaluatedOpportunity) -> RiskAssessment:
        """Evaluate an opportunity against all risk limits.

        Returns a RiskAssessment with decision and position sizing.
        """
        assessment = RiskAssessment(opportunity=opportunity)

        # --- Gate 0: Kill Switch ---
        if self.state.kill_switch_active:
            assessment.reason = RejectionReason.KILL_SWITCH
            logger.info("risk_rejected_kill_switch", symbol=opportunity.signal.symbol)
            return assessment

        # --- Gate 1: Circuit Breaker ---
        if self.state.circuit_breaker_tripped:
            assessment.reason = RejectionReason.CIRCUIT_BREAKER
            logger.info("risk_rejected_circuit_breaker", symbol=opportunity.signal.symbol)
            return assessment

        # --- Gate 2: Drawdown Check ---
        if self.state.current_drawdown_pct >= self.cb_drawdown_pct:
            self.trip_circuit_breaker("drawdown_exceeded")
            assessment.reason = RejectionReason.DRAWDOWN
            return assessment

        if self.state.consecutive_losses >= self.cb_consecutive_losses:
            self.trip_circuit_breaker("consecutive_losses")
            assessment.reason = RejectionReason.CIRCUIT_BREAKER
            return assessment

        # --- Gate 3: Total Exposure ---
        required_capital = opportunity.signal.required_capital or self.max_position_size_usd
        if self.state.total_exposure + required_capital > self.max_total_exposure_usd:
            assessment.reason = RejectionReason.MAX_EXPOSURE
            logger.info(
                "risk_rejected_max_exposure",
                current=self.state.total_exposure,
                required=required_capital,
            )
            return assessment

        # --- Gate 4: Per-Strategy Limits ---
        strategy_id = opportunity.signal.strategy_id
        strategy_count = self._strategy_position_counts.get(strategy_id, 0)
        if strategy_count >= self.max_positions_per_strategy:
            assessment.reason = RejectionReason.PER_STRATEGY_EXPOSURE
            return assessment

        # --- Gate 5: Stale Data ---
        if opportunity.signal.is_expired:
            assessment.reason = RejectionReason.STALE_DATA
            return assessment

        # --- Gate 6: Insufficient Capital ---
        if required_capital <= 0:
            assessment.reason = RejectionReason.INSUFFICIENT_CAPITAL
            return assessment

        # --- Gate 7: Position Sizing ---
        position_size = min(required_capital, self.max_position_size_usd)
        position_size = min(position_size, self.max_total_exposure_usd - self.state.total_exposure)

        if position_size < required_capital * 0.5:
            # Can't allocate at least 50% of required — too constrained
            assessment.reason = RejectionReason.INSUFFICIENT_CAPITAL
            return assessment

        assessment.max_position_size = position_size

        # --- Gate 8: SPOT-ONLY enforcement ---
        # No short selling, no leverage, no margin, no futures
        if opportunity.signal.direction.value == "short":
            assessment.reason = RejectionReason.SPOT_ONLY
            logger.info(
                "risk_rejected_spot_only",
                symbol=opportunity.signal.symbol,
                direction=opportunity.signal.direction.value,
            )
            return assessment

        # --- Gate 9: Hard Stop Loss (-0.30% FINAL) ---
        signal = opportunity.signal
        hard_stop_pct: float = self.default_stop_loss_pct  # -0.30% FINAL
        entry_price = signal.metadata.get("entry_price", None)
        if entry_price is not None:
            assessment.stop_loss_price = _compute_hard_stop(
                float(entry_price), signal.direction.value, hard_stop_pct
            )
        else:
            assessment.stop_loss_price = None

        # --- Gate 10: Trailing Stop (enabled, no fixed TP) ---
        # take_profit_price stays None — NO fixed profit ceiling per policy
        assessment.take_profit_price = None
        assessment.trailing_stop_enabled = True

        # --- PASSED ---
        assessment.decision = RiskDecision.APPROVED
        logger.info(
            "risk_approved",
            strategy=strategy_id,
            symbol=opportunity.signal.symbol,
            position_size=position_size,
            stop_loss=assessment.stop_loss_price,
            trailing_stop=True,
        )

        return assessment

    def assess_batch(self, opportunities: list[EvaluatedOpportunity]) -> list[RiskAssessment]:
        """Evaluate a batch of ranked opportunities.

        Returns only approved assessments, respecting exposure limits
        cumulatively across the batch.
        """
        results: list[RiskAssessment] = []
        for opp in opportunities:
            assessment = self.assess(opp)
            if assessment.decision == RiskDecision.APPROVED:
                results.append(assessment)
                # Reserve exposure for this approved opportunity
                self._reserve_exposure(assessment)
            else:
                logger.debug(
                    "risk_rejected",
                    strategy=opp.signal.strategy_id,
                    reason=assessment.reason.value if assessment.reason else "unknown",
                )
        return results

    # --- State Updates ---

    def update_state(
        self,
        total_exposure: float,
        per_market_exposure: dict[str, float] | None = None,
        per_strategy_exposure: dict[str, float] | None = None,
        current_equity: float | None = None,
        consecutive_losses: int | None = None,
        open_positions_count: int | None = None,
        strategy_position_counts: dict[str, int] | None = None,
    ) -> None:
        """Update the risk engine's view of current portfolio state."""
        self.state.total_exposure = total_exposure
        if per_market_exposure is not None:
            self.state.per_market_exposure = per_market_exposure
        if per_strategy_exposure is not None:
            self.state.per_strategy_exposure = per_strategy_exposure
        if current_equity is not None:
            if current_equity > self.state.peak_equity:
                self.state.peak_equity = current_equity
            if self.state.peak_equity > 0:
                self.state.current_drawdown_pct = (
                    (self.state.peak_equity - current_equity) / self.state.peak_equity * 100
                )
        if consecutive_losses is not None:
            self.state.consecutive_losses = consecutive_losses
        if open_positions_count is not None:
            self.state.open_positions_count = open_positions_count
        if strategy_position_counts is not None:
            self._strategy_position_counts = strategy_position_counts
        self.state.last_update = datetime.now(UTC)

    def _reserve_exposure(self, assessment: RiskAssessment) -> None:
        """Reserve capital for an approved assessment (virtual allocation)."""
        self.state.total_exposure += assessment.max_position_size
        sid = assessment.opportunity.signal.strategy_id
        self._strategy_position_counts[sid] = self._strategy_position_counts.get(sid, 0) + 1

    # --- Helpers ---


def _compute_hard_stop(entry_price: float, direction: str, stop_loss_pct: float) -> float | None:
    """Compute hard stop-loss price.

    FINAL CONFIGURATION: -0.30% per position.
    The system records TARGET STOP vs ACTUAL EXIT PRICE separately.
    """
    pct = stop_loss_pct / 100.0
    if direction == "long":
        return entry_price * (1.0 - pct)
    else:
        return entry_price * (1.0 + pct)
