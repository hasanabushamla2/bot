"""Auto-Sweep — simulated profit-sweeping policy engine.

IMPORTANT: Auto-sweep execution is DISABLED. This module generates
SweepRecommendations only. No real withdrawals occur.

DEFAULT RULE (Level 4 only):
  active_capital_cap = $5,000,000
  excess_capital = max(0, total_balance - active_capital_cap)
  daily_positive_profit = max(0, daily_realized_profit)
  sweep_eligible = min(excess_capital, daily_positive_profit)

RULES:
- Never sweep unrealized profit
- Never sweep borrowed funds
- Never sweep capital required for open orders/positions
- Never execute automatically (approval_required = True)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class DestinationType(str, Enum):
    """Approved destination types for swept capital.

    These are ABSTRACTIONS — no real transfers occur.
    Real withdrawal requires: separate config, whitelist destination,
    additional approval gate, separate permission, audit log, risk checks.
    """

    CASH_RESERVE = "cash_reserve"
    SECURE_WALLET = "secure_wallet"
    EXTERNAL_TREASURY = "external_treasury"
    GOLD_ALLOCATION = "gold_allocation"
    FX_RESERVE = "fx_reserve"
    MANUAL_REVIEW = "manual_review"


class SweepStatus(str, Enum):
    """Current status of a sweep recommendation."""

    PENDING = "pending"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class SweepPolicyConfig:
    """Configuration for auto-sweep policy."""

    active_capital_cap: float = 5_000_000.0  # Capital ceiling
    min_sweep_amount: float = 100.0  # Don't sweep trivial amounts
    sweep_frequency_days: int = 1  # How often to evaluate
    auto_execute: bool = False  # MUST be False for safety
    default_destination: DestinationType = DestinationType.MANUAL_REVIEW
    approval_required: bool = True  # MUST be True for safety


@dataclass
class SweepRecommendation:
    """A sweep recommendation — NOT an actual withdrawal.

    Contains all information needed for a human operator to review
    and potentially approve a sweep. This is an AUDIT record, not
    an executable instruction.
    """

    sweep_id: str = ""
    eligible_amount: float = 0.0
    reason: str = ""
    destination: DestinationType = DestinationType.MANUAL_REVIEW
    status: SweepStatus = SweepStatus.PENDING
    approval_required: bool = True

    # Portfolio context
    total_balance: float = 0.0
    active_capital: float = 0.0
    excess_capital: float = 0.0
    daily_realized_profit: float = 0.0
    unreserved_balance: float = 0.0

    # Audit
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None
    approved_by: str = ""

    metadata: dict[str, Any] = field(default_factory=dict)


class SweepEngine:
    """Generates sweep recommendations for capital above the ceiling.

    SIMULATION ONLY. No real withdrawals. No exchange transfers.
    approval_required is always True.

    The engine:
    1. Checks if total_balance exceeds the capital cap.
    2. Computes sweep-eligible amount (realized profit only).
    3. Generates a SweepRecommendation for human review.
    """

    def __init__(self, config: SweepPolicyConfig | None = None) -> None:
        self.config = config or SweepPolicyConfig()
        self._recommendations: list[SweepRecommendation] = []
        self._last_evaluation: datetime | None = None

    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        total_balance: float,
        daily_realized_profit: float = 0.0,
        active_positions_value: float = 0.0,
        open_orders_value: float = 0.0,
    ) -> SweepRecommendation | None:
        """Evaluate whether a sweep is warranted.

        Args:
            total_balance: Current total portfolio value.
            daily_realized_profit: Today's realized net P&L.
            active_positions_value: Capital tied up in open positions.
            open_orders_value: Capital reserved for open orders.

        Returns:
            SweepRecommendation if sweep is warranted, None otherwise.
        """
        cfg = self.config

        # --- Only Level 4 (above cap) triggers sweep ---
        if total_balance <= cfg.active_capital_cap:
            return None

        # --- Excess capital ---
        excess = max(0.0, total_balance - cfg.active_capital_cap)

        # --- Only sweep realized profit, never unrealized ---
        positive_profit = max(0.0, daily_realized_profit)

        # --- Cannot sweep capital needed for positions/orders ---
        reserved = active_positions_value + open_orders_value
        available_excess = max(0.0, excess - reserved)

        eligible = min(available_excess, positive_profit)

        if eligible < cfg.min_sweep_amount:
            return None

        rec = SweepRecommendation(
            sweep_id=f"SWEEP-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            eligible_amount=round(eligible, 2),
            reason=f"Capital cap ${cfg.active_capital_cap:,.0f} exceeded. "
            f"Excess: ${excess:,.0f}, Realized profit: ${positive_profit:,.0f}",
            destination=cfg.default_destination,
            status=SweepStatus.PENDING,
            approval_required=cfg.approval_required,
            total_balance=total_balance,
            active_capital=cfg.active_capital_cap,
            excess_capital=excess,
            daily_realized_profit=daily_realized_profit,
            unreserved_balance=max(0.0, total_balance - reserved),
        )

        self._recommendations.append(rec)
        self._last_evaluation = datetime.now(UTC)

        logger.info(
            "sweep_recommendation_generated",
            sweep_id=rec.sweep_id,
            amount=rec.eligible_amount,
            destination=rec.destination.value,
        )

        return rec

    # ------------------------------------------------------------------
    # Safe override evaluation for testing / all tiers
    # ------------------------------------------------------------------

    def evaluate_for_tier(
        self,
        total_balance: float,
        daily_realized_profit: float = 0.0,
        capital_cap_override: float | None = None,
    ) -> SweepRecommendation | None:
        """Evaluate sweep with an overridden capital cap (for testing)."""
        original_cap = self.config.active_capital_cap
        if capital_cap_override is not None:
            self.config.active_capital_cap = capital_cap_override

        result = self.evaluate(total_balance, daily_realized_profit)
        self.config.active_capital_cap = original_cap
        return result

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_pending(self) -> list[SweepRecommendation]:
        return [r for r in self._recommendations if r.status == SweepStatus.PENDING]

    def get_history(self, limit: int = 50) -> list[SweepRecommendation]:
        return self._recommendations[-limit:]

    def mark_approved(self, sweep_id: str, approved_by: str = "operator") -> bool:
        """Mark a recommendation as approved (human action)."""
        for rec in self._recommendations:
            if rec.sweep_id == sweep_id and rec.status == SweepStatus.PENDING:
                rec.status = SweepStatus.APPROVED
                rec.approved_at = datetime.now(UTC)
                rec.approved_by = approved_by
                return True
        return False

    def mark_rejected(self, sweep_id: str) -> bool:
        for rec in self._recommendations:
            if rec.sweep_id == sweep_id and rec.status == SweepStatus.PENDING:
                rec.status = SweepStatus.REJECTED
                return True
        return False
