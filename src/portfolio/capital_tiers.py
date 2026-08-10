"""Capital Tiers — dynamic capital-management layer for multi-level accounts.

Detects the current capital tier based on total balance and dynamically
controls trading slots, capital allocation, and asset-universe selection.

TIERS (configurable):
- LEVEL_1 (SMALL):  ≤ $5,000  → 2 slots,   all qualified Spot
- LEVEL_2 (MEDIUM): ≤ $100,000 → 5 slots,  diversified allocation
- LEVEL_3 (LARGE):  ≤ $5,000,000 → 10 slots, deep-liquidity focus
- LEVEL_4 (CAP):    > $5,000,000 → 20 slots, auto-sweep eligible

The system MUST NOT blindly force capital into any specific asset class.
It ranks all qualified opportunities and selects the best ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.core.logging_config import get_logger
from src.portfolio.markets import AssetQualityReport, QualityTier

logger = get_logger(__name__)


class CapitalLevel(str, Enum):
    LEVEL_1 = "level_1"  # Small account: ≤ $5,000
    LEVEL_2 = "level_2"  # Medium account: $5,001-$100,000
    LEVEL_3 = "level_3"  # Large account: $100,001-$5,000,000
    LEVEL_4 = "level_4"  # Capital cap: > $5,000,000


@dataclass
class CapitalTierConfig:
    """Configuration for capital-tier thresholds and slot counts.

    All values are configurable. These are POLICY DEFAULTS.
    """

    # Balance boundaries
    level_1_cap: float = 5_000.0
    level_2_cap: float = 100_000.0
    level_3_cap: float = 5_000_000.0

    # Slot counts per tier
    slots_level_1: int = 2
    slots_level_2: int = 5
    slots_level_3: int = 10
    slots_level_4: int = 20

    # Active capital: percentage of balance to deploy
    active_capital_pct: float = 100.0  # Default: 100%

    # Capital cap for Level 4 (beyond this → sweep)
    active_capital_cap: float = 5_000_000.0

    # Allowed tiers per capital level
    allowed_tiers_level_1: list[QualityTier] = field(
        default_factory=lambda: [
            QualityTier.TIER_A,
            QualityTier.TIER_B,
            QualityTier.TIER_C,
        ]
    )
    allowed_tiers_level_2: list[QualityTier] = field(
        default_factory=lambda: [
            QualityTier.TIER_A,
            QualityTier.TIER_B,
            QualityTier.TIER_C,
        ]
    )
    allowed_tiers_level_3: list[QualityTier] = field(
        default_factory=lambda: [QualityTier.TIER_A, QualityTier.TIER_B]
    )
    allowed_tiers_level_4: list[QualityTier] = field(
        default_factory=lambda: [QualityTier.TIER_A, QualityTier.TIER_B]
    )

    # Maximum percentage of slots that can be TIER_C (for Levels 1/2)
    max_tier_c_slot_pct: float = 40.0  # Max 40% of slots can be TIER_C


@dataclass
class TierState:
    """Current capital-tier state snapshot."""

    level: CapitalLevel = CapitalLevel.LEVEL_1
    total_balance: float = 0.0
    active_capital: float = 0.0
    sweep_eligible: float = 0.0
    target_slots: int = 0
    actual_slots: int = 0
    provisional_slot_size: float = 0.0

    # Universe
    eligible_markets: int = 0
    qualified_assets: int = 0
    tier_a_count: int = 0
    tier_b_count: int = 0
    tier_c_count: int = 0
    tier_d_count: int = 0

    # Allocation guidance
    allowed_tiers: list[QualityTier] = field(default_factory=list)
    max_tier_c_slots: int = 0


class CapitalTierManager:
    """Detects capital tier, controls slots, and provides allocation guidance.

    This module WORKS WITH the existing CapitalAllocator — it does not
    replace it. The CapitalAllocator still handles the detailed allocation
    math. This module provides the STRATEGIC framework.
    """

    def __init__(self, config: CapitalTierConfig | None = None) -> None:
        self.config = config or CapitalTierConfig()
        self._state = TierState()

    # ------------------------------------------------------------------
    # Tier determination
    # ------------------------------------------------------------------

    def determine_tier(self, total_balance: float) -> TierState:
        """Determine the current capital tier and compute slot/sizing guidance.

        Call this on every balance update (deposit, withdrawal, P&L change).
        """
        state = TierState(total_balance=total_balance)
        cfg = self.config

        # --- Determine level ---
        if total_balance <= cfg.level_1_cap:
            state.level = CapitalLevel.LEVEL_1
            state.target_slots = cfg.slots_level_1
            state.allowed_tiers = list(cfg.allowed_tiers_level_1)
        elif total_balance <= cfg.level_2_cap:
            state.level = CapitalLevel.LEVEL_2
            state.target_slots = cfg.slots_level_2
            state.allowed_tiers = list(cfg.allowed_tiers_level_2)
        elif total_balance <= cfg.level_3_cap:
            state.level = CapitalLevel.LEVEL_3
            state.target_slots = cfg.slots_level_3
            state.allowed_tiers = list(cfg.allowed_tiers_level_3)
        else:
            state.level = CapitalLevel.LEVEL_4
            state.target_slots = cfg.slots_level_4
            state.allowed_tiers = list(cfg.allowed_tiers_level_4)

        # --- Active capital ---
        if state.level == CapitalLevel.LEVEL_4:
            state.active_capital = min(total_balance, cfg.active_capital_cap)
            state.sweep_eligible = max(0.0, total_balance - cfg.active_capital_cap)
        else:
            state.active_capital = total_balance * cfg.active_capital_pct / 100.0
            state.sweep_eligible = 0.0

        # --- Provisional slot size ---
        state.provisional_slot_size = (
            state.active_capital / state.target_slots if state.target_slots > 0 else 0.0
        )

        # --- Tier C slot limit ---
        state.max_tier_c_slots = max(1, int(state.target_slots * cfg.max_tier_c_slot_pct / 100.0))

        self._state = state

        logger.info(
            "capital_tier_determined",
            level=state.level.value,
            balance=round(total_balance, 2),
            active_capital=round(state.active_capital, 2),
            target_slots=state.target_slots,
            sweep_eligible=round(state.sweep_eligible, 2),
        )

        return state

    # ------------------------------------------------------------------
    # Slot allocation guidance
    # ------------------------------------------------------------------

    def compute_slot_allocation(
        self,
        qualified_assets: list[AssetQualityReport],
    ) -> dict[str, Any]:
        """Given qualified assets and current tier, compute slot allocation
        guidance for the CapitalAllocator.

        Returns:
            dict with slot sizing guidelines, tier constraints, and
            provisional allocations.
        """
        state = self._state

        # Classify by tier
        by_tier: dict[QualityTier, list[AssetQualityReport]] = {}
        for asset in qualified_assets:
            by_tier.setdefault(asset.tier, []).append(asset)

        # Update universe counts
        state.eligible_markets = len(qualified_assets)
        state.tier_a_count = len(by_tier.get(QualityTier.TIER_A, []))
        state.tier_b_count = len(by_tier.get(QualityTier.TIER_B, []))
        state.tier_c_count = len(by_tier.get(QualityTier.TIER_C, []))
        state.tier_d_count = 0  # Already filtered

        # Slot distribution by tier
        # First allocate to A, then B, then C (subject to limits)
        remaining_slots = state.target_slots
        tier_c_limit = state.max_tier_c_slots

        slots_a = min(remaining_slots, state.tier_a_count)
        remaining_slots -= slots_a

        slots_b = min(remaining_slots, state.tier_b_count)
        remaining_slots -= slots_b

        slots_c = min(remaining_slots, tier_c_limit, state.tier_c_count)
        remaining_slots -= slots_c

        state.actual_slots = slots_a + slots_b + slots_c
        state.qualified_assets = state.tier_a_count + state.tier_b_count + state.tier_c_count

        return {
            "level": state.level.value,
            "target_slots": state.target_slots,
            "actual_slots": state.actual_slots,
            "provisional_slot_size": state.provisional_slot_size,
            "active_capital": state.active_capital,
            "sweep_eligible": state.sweep_eligible,
            # Slot distribution
            "slots_a": slots_a,
            "slots_b": slots_b,
            "slots_c": slots_c,
            "tier_c_slot_limit": tier_c_limit,
            # Universe stats
            "eligible_markets": state.eligible_markets,
            "tier_a_count": state.tier_a_count,
            "tier_b_count": state.tier_b_count,
            "tier_c_count": state.tier_c_count,
            "tier_d_count": state.tier_d_count,
        }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_state(self) -> TierState:
        return self._state

    def get_allowed_tiers(self) -> list[QualityTier]:
        return self._state.allowed_tiers

    def get_target_slots(self) -> int:
        return self._state.target_slots


# ---------------------------------------------------------------------------
# Daily report generator
# ---------------------------------------------------------------------------


@dataclass
class CapitalDailyReport:
    """Daily capital-allocation report."""

    portfolio_level: str = ""
    total_balance: float = 0.0
    active_capital: float = 0.0
    sweep_eligible: float = 0.0
    target_slots: int = 0
    actual_slots: int = 0
    provisional_slot_size: float = 0.0
    avg_order_allocation: float = 0.0

    eligible_markets: int = 0
    qualified_assets: int = 0
    tier_a_count: int = 0
    tier_b_count: int = 0
    tier_c_count: int = 0
    tier_d_count: int = 0

    allocation_by_asset: dict[str, float] = field(default_factory=dict)
    allocation_by_strategy: dict[str, float] = field(default_factory=dict)

    daily_realized_pnl: float = 0.0
    daily_fees: float = 0.0
    daily_slippage: float = 0.0
    unused_capital: float = 0.0

    mode: str = "PAPER"


def generate_daily_report(
    tier_manager: CapitalTierManager,
    total_balance: float,
    daily_pnl: float = 0.0,
    daily_fees: float = 0.0,
    daily_slippage: float = 0.0,
    allocation_by_asset: dict[str, float] | None = None,
    allocation_by_strategy: dict[str, float] | None = None,
    avg_order_allocation: float = 0.0,
    mode: str = "PAPER",
) -> CapitalDailyReport:
    """Build a daily capital-allocation report."""
    state = tier_manager.get_state()

    return CapitalDailyReport(
        portfolio_level=state.level.value.upper(),
        total_balance=total_balance,
        active_capital=state.active_capital,
        sweep_eligible=state.sweep_eligible,
        target_slots=state.target_slots,
        actual_slots=state.actual_slots,
        provisional_slot_size=state.provisional_slot_size,
        avg_order_allocation=avg_order_allocation,
        eligible_markets=state.eligible_markets,
        qualified_assets=state.qualified_assets,
        tier_a_count=state.tier_a_count,
        tier_b_count=state.tier_b_count,
        tier_c_count=state.tier_c_count,
        tier_d_count=state.tier_d_count,
        allocation_by_asset=allocation_by_asset or {},
        allocation_by_strategy=allocation_by_strategy or {},
        daily_realized_pnl=daily_pnl,
        daily_fees=daily_fees,
        daily_slippage=daily_slippage,
        unused_capital=max(
            0.0,
            state.active_capital - sum(allocation_by_asset.values())
            if allocation_by_asset
            else 0.0,
        ),
        mode=mode,
    )
