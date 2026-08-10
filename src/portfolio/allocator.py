"""Capital Allocator / Portfolio Allocator — intelligent capital distribution.

This is the brain of portfolio-level decision making. It takes:
- Ranked opportunities (from Opportunity Engine)
- Risk assessments (from Risk Engine)
- Position capacities (from Capacity Estimator)
- Correlation matrix (from Correlation Tracker)
- Current portfolio state

...and produces allocation decisions that:
- Distribute capital across multiple independent opportunities.
- Respect liquidity constraints per instrument.
- Penalize correlated positions.
- Scale horizontally as equity grows rather than blindly increasing sizes.
- Rebalance periodically without unnecessarily closing profitable positions.

KEY PRINCIPLE:
Account equity growth must lead to SMARTER CAPITAL DISTRIBUTION,
NOT blindly larger individual trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger
from src.portfolio.capacity import PositionCapacity
from src.portfolio.correlation import CorrelationTracker
from src.portfolio.universe import UniverseManager

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class AllocatorConfig:
    """Configuration for the capital allocator."""

    # Capital reserves
    reserve_pct: float = 5.0  # % of equity kept as reserve
    max_single_position_pct: float = 10.0  # Max % of equity in one position
    max_single_asset_pct: float = 20.0  # Max % of equity in one asset
    max_single_strategy_pct: float = 25.0  # Max % of equity in one strategy
    max_single_exchange_pct: float = 40.0  # Max % of equity in one exchange
    max_correlated_exposure_pct: float = 30.0  # Max % in correlated group

    # Diversification
    min_positions_target: int = 1  # Minimum positions to maintain
    max_positions_limit: int = 50  # Hard cap on concurrent positions
    diversification_bonus: float = 0.05  # Score bonus for uncorrelated additions

    # Sizing
    sizing_method: str = "risk_budget"  # "risk_budget", "equal_risk", "score_weighted"
    risk_budget_fraction: float = 0.25  # Fraction of full Risk-Budget to use
    min_position_size_usd: float = 50.0  # Don't allocate less than this

    # Rebalancing
    rebalance_interval_seconds: float = 300.0  # 5 minutes
    close_winners: bool = False  # Don't close winning positions for rebalance


@dataclass
class PortfolioState:
    """Current portfolio snapshot for allocation decisions."""

    total_equity: float = 10_000.0
    available_cash: float = 10_000.0
    reserved_cash: float = 0.0

    # Current exposures
    positions: dict[str, float] = field(default_factory=dict)  # symbol → notional
    asset_exposure: dict[str, float] = field(default_factory=dict)  # asset → notional
    strategy_exposure: dict[str, float] = field(default_factory=dict)  # strategy → notional
    exchange_exposure: dict[str, float] = field(default_factory=dict)  # exchange → notional

    # Active position symbols (for correlation checks)
    active_symbols: set[str] = field(default_factory=set)

    # Performance
    unrealized_pnl: float = 0.0
    daily_pnl: float = 0.0

    # Derived
    total_exposure_pct: float = 0.0
    capital_utilization_pct: float = 0.0

    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class AllocationDecision:
    """A single capital allocation decision for one opportunity."""

    symbol: str
    strategy_id: str
    exchange: str

    # Sizing
    requested_capital: float = 0.0  # What the strategy wants
    allocated_capital: float = 0.0  # What we actually allocate
    max_efficient_size: float = 0.0  # Liquidity cap

    # Scores
    opportunity_score: float = 0.0
    allocation_score: float = 0.0  # Final weighted score for this allocation
    correlation_penalty: float = 0.0
    diversification_bonus: float = 0.0
    liquidity_discount: float = 0.0

    # Status
    is_allocated: bool = False
    rejection_reason: str = ""
    sizing_method: str = ""

    # Risk
    stop_loss_price: float | None = None
    take_profit_price: float | None = None

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------


class CapitalAllocator:
    """Allocates capital across eligible opportunities.

    The allocation process:
    1. Assess current portfolio state.
    2. For each opportunity, compute max efficient size from liquidity.
    3. Apply concentration limits (per asset, strategy, exchange).
    4. Apply correlation penalties.
    5. Compute allocation scores.
    6. Distribute available capital using the chosen sizing method.
    7. Produce allocation decisions sorted by allocation score.
    """

    def __init__(
        self,
        config: AllocatorConfig | None = None,
        correlation_tracker: CorrelationTracker | None = None,
        universe: UniverseManager | None = None,
    ) -> None:
        self.config = config or AllocatorConfig()
        self.correlation = correlation_tracker or CorrelationTracker()
        self.universe = universe or UniverseManager()

    # ------------------------------------------------------------------
    # Main allocation entry point
    # ------------------------------------------------------------------

    def allocate(
        self,
        portfolio: PortfolioState,
        opportunities: list[tuple[Any, Any, PositionCapacity]],
        # Each tuple: (EvaluatedOpportunity, RiskAssessment, PositionCapacity)
    ) -> list[AllocationDecision]:
        """Allocate capital across a batch of approved opportunities.

        Args:
            portfolio: Current portfolio state.
            opportunities: List of (opportunity, risk_assessment, capacity) tuples.

        Returns:
            List of AllocationDecisions, both allocated and rejected.
        """
        if not opportunities or portfolio.total_equity <= 0:
            return []

        # --- Step 1: Compute available capital ---
        reserve = portfolio.total_equity * self.config.reserve_pct / 100.0
        available = max(
            0.0,
            portfolio.total_equity
            - portfolio.total_exposure_pct / 100.0 * portfolio.total_equity
            - reserve,
        )
        available = min(available, portfolio.available_cash)
        available = max(0.0, available)

        if available < self.config.min_position_size_usd:
            logger.debug("allocator_insufficient_capital", available=available)
            return []

        # --- Step 2: Build preliminary decisions ---
        decisions: list[AllocationDecision] = []
        for opp, risk, capacity in opportunities:
            dec = self._pre_evaluate(opp, risk, capacity, portfolio)
            decisions.append(dec)

        # --- Step 3: Compute allocation scores ---
        for dec in decisions:
            dec.allocation_score = self._compute_allocation_score(dec)

        # Sort by allocation score descending
        decisions.sort(key=lambda d: d.allocation_score, reverse=True)

        # --- Step 4: Distribute capital ---
        self._distribute_capital(decisions, available, portfolio)

        # --- Step 5: Log summary ---
        allocated = [d for d in decisions if d.is_allocated]
        rejected = [d for d in decisions if not d.is_allocated]
        total_allocated = sum(d.allocated_capital for d in allocated)

        logger.info(
            "allocation_complete",
            total_opportunities=len(decisions),
            allocated=len(allocated),
            rejected=len(rejected),
            total_allocated=round(total_allocated, 2),
            available=round(available, 2),
            capital_utilization_pct=round(total_allocated / portfolio.total_equity * 100, 2)
            if portfolio.total_equity > 0
            else 0.0,
        )

        return decisions

    # ------------------------------------------------------------------
    # Pre-evaluation
    # ------------------------------------------------------------------

    def _pre_evaluate(
        self,
        opp: Any,  # EvaluatedOpportunity
        risk: Any,  # RiskAssessment
        capacity: PositionCapacity,
        portfolio: PortfolioState,
    ) -> AllocationDecision:
        """Build a preliminary allocation decision."""
        signal = opp.signal
        dec = AllocationDecision(
            symbol=signal.symbol or "unknown",
            strategy_id=signal.strategy_id,
            exchange=signal.exchange or "unknown",
            requested_capital=signal.required_capital or 0.0,
            max_efficient_size=capacity.max_efficient_size,
            opportunity_score=opp.score.final_score,
            stop_loss_price=risk.stop_loss_price,
            take_profit_price=risk.take_profit_price,
        )

        # --- Rejection: Non-viable capacity ---
        if not capacity.is_viable:
            dec.rejection_reason = capacity.viability_reason
            return dec

        # --- Correlation penalty ---
        corr_penalty = self.correlation.correlation_penalty(dec.symbol, portfolio.active_symbols)
        dec.correlation_penalty = corr_penalty

        if corr_penalty >= 0.9:
            dec.rejection_reason = f"Excessive correlation penalty ({corr_penalty:.2f})"
            return dec

        # --- Diversification bonus ---
        if portfolio.active_symbols and dec.symbol not in portfolio.active_symbols:
            div_score = self.correlation.diversification_score(portfolio.active_symbols, dec.symbol)
            dec.diversification_bonus = self.config.diversification_bonus * div_score

        # --- Liquidity discount ---
        if dec.max_efficient_size > 0 and dec.requested_capital > 0:
            ratio = min(dec.requested_capital, dec.max_efficient_size) / dec.requested_capital
            dec.liquidity_discount = max(0.0, 1.0 - ratio)
        else:
            dec.liquidity_discount = 0.0

        return dec

    # ------------------------------------------------------------------
    # Allocation scoring
    # ------------------------------------------------------------------

    def _compute_allocation_score(self, dec: AllocationDecision) -> float:
        """Compute the final allocation score for ranking.

        Weights:
        - Opportunity score: 35%
        - Diversification bonus: 15%
        - Correlation penalty: -15%
        - Liquidity: 20%
        - Capacity headroom: 15%
        """
        if dec.rejection_reason:
            return -1.0

        # Capacity headroom: how much room before hitting max efficient
        if dec.max_efficient_size > 0 and dec.requested_capital > 0:
            headroom = min(1.0, dec.max_efficient_size / dec.requested_capital)
        else:
            headroom = 0.5

        score = (
            dec.opportunity_score * 0.35
            + dec.diversification_bonus * 0.15
            - dec.correlation_penalty * 0.15
            + (1.0 - dec.liquidity_discount) * 0.20
            + headroom * 0.15
        )
        return score

    # ------------------------------------------------------------------
    # Capital distribution
    # ------------------------------------------------------------------

    def _distribute_capital(
        self,
        decisions: list[AllocationDecision],
        available: float,
        portfolio: PortfolioState,
    ) -> None:
        """Distribute available capital among ranked decisions.

        Respects concentration limits cumulatively.
        """
        remaining = available
        allocated_count = 0

        # Running exposure trackers
        asset_exposure: dict[str, float] = dict(portfolio.asset_exposure)
        strategy_exposure: dict[str, float] = dict(portfolio.strategy_exposure)
        exchange_exposure: dict[str, float] = dict(portfolio.exchange_exposure)

        for dec in decisions:
            if dec.rejection_reason:
                continue

            if allocated_count >= self.config.max_positions_limit:
                dec.rejection_reason = "Max position limit reached"
                continue

            if remaining < self.config.min_position_size_usd:
                dec.rejection_reason = "Insufficient remaining capital"
                continue

            # --- Determine allocation size ---
            size = self._size_position(dec, remaining, portfolio)

            if size < self.config.min_position_size_usd:
                dec.rejection_reason = f"Computed size ${size:,.2f} below minimum ${self.config.min_position_size_usd:,.2f}"
                continue

            # --- Concentration checks ---
            base_asset = self._extract_base_asset(dec.symbol)

            # Per-asset limit
            current_asset = asset_exposure.get(base_asset, 0.0)
            max_asset = portfolio.total_equity * self.config.max_single_asset_pct / 100.0
            if current_asset + size > max_asset:
                size = max(0.0, max_asset - current_asset)
                if size < self.config.min_position_size_usd:
                    dec.rejection_reason = f"Asset concentration limit ({base_asset})"
                    continue

            # Per-strategy limit
            current_strategy = strategy_exposure.get(dec.strategy_id, 0.0)
            max_strategy = portfolio.total_equity * self.config.max_single_strategy_pct / 100.0
            if current_strategy + size > max_strategy:
                size = max(0.0, max_strategy - current_strategy)
                if size < self.config.min_position_size_usd:
                    dec.rejection_reason = f"Strategy concentration limit ({dec.strategy_id})"
                    continue

            # Per-exchange limit
            current_exchange = exchange_exposure.get(dec.exchange, 0.0)
            max_exchange = portfolio.total_equity * self.config.max_single_exchange_pct / 100.0
            if current_exchange + size > max_exchange:
                size = max(0.0, max_exchange - current_exchange)
                if size < self.config.min_position_size_usd:
                    dec.rejection_reason = f"Exchange concentration limit ({dec.exchange})"
                    continue

            # --- Final check: can't exceed available ---
            if size > remaining:
                size = remaining

            # --- Allocate ---
            dec.allocated_capital = round(size, 2)
            dec.is_allocated = True
            dec.sizing_method = self.config.sizing_method
            remaining -= size
            allocated_count += 1

            # Update running exposures
            asset_exposure[base_asset] = asset_exposure.get(base_asset, 0.0) + size
            strategy_exposure[dec.strategy_id] = strategy_exposure.get(dec.strategy_id, 0.0) + size
            exchange_exposure[dec.exchange] = exchange_exposure.get(dec.exchange, 0.0) + size

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def _size_position(
        self,
        dec: AllocationDecision,
        available: float,
        portfolio: PortfolioState,
    ) -> float:
        """Compute position size using the configured method."""
        if self.config.sizing_method == "equal_risk":
            return self._size_equal_risk(dec, portfolio)
        elif self.config.sizing_method == "score_weighted":
            return self._size_score_weighted(dec, available)
        else:  # risk_budget_fractional (default)
            return self._size_risk_budget_fractional(dec, portfolio)

    def _size_risk_budget_fractional(
        self, dec: AllocationDecision, portfolio: PortfolioState
    ) -> float:
        """Fractional Risk-Budget position sizing.

        Risk-Budget fraction f* = edge / variance_estimate
        We use a conservative fraction of full Risk-Budget.

        For trading: approximated as win_rate - (loss_rate / win_loss_ratio)
        """
        equity = portfolio.total_equity

        # Base: max single position as fraction of equity
        base_size = equity * self.config.max_single_position_pct / 100.0

        # Apply Risk-Budget fraction
        risk_budget_size = base_size * self.config.risk_budget_fraction

        # Cap by max efficient size from liquidity
        if dec.max_efficient_size > 0:
            risk_budget_size = min(risk_budget_size, dec.max_efficient_size)

        # Cap by requested capital (don't allocate more than needed)
        if dec.requested_capital > 0:
            risk_budget_size = min(risk_budget_size, dec.requested_capital)

        # Floor
        return max(0.0, risk_budget_size)

    def _size_equal_risk(self, dec: AllocationDecision, portfolio: PortfolioState) -> float:
        """Equal risk contribution sizing — divide risk budget equally."""
        equity = portfolio.total_equity
        max_positions = max(1, self.config.max_positions_limit)
        return equity * self.config.max_single_position_pct / 100.0 / max_positions

    def _size_score_weighted(self, dec: AllocationDecision, available: float) -> float:
        """Score-weighted sizing — higher score gets proportionally more."""
        # This is a simplified version; in practice would normalize across
        # all decisions in the batch
        base = available * self.config.max_single_position_pct / 100.0
        score_mult = max(0.1, dec.allocation_score) if dec.allocation_score > 0 else 0.1
        return base * score_mult

    # ------------------------------------------------------------------
    # Rebalancing assessment
    # ------------------------------------------------------------------

    def should_rebalance(self, last_rebalance: datetime | None = None) -> bool:
        """Check if enough time has passed for a rebalance."""
        if last_rebalance is None:
            return True
        elapsed = (datetime.now(UTC) - last_rebalance).total_seconds()
        return elapsed >= self.config.rebalance_interval_seconds

    def rebalance_check(
        self, portfolio: PortfolioState, current_positions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Check which positions should be adjusted.

        Returns list of recommended adjustments.
        Does NOT close winning positions unless configured to.
        """
        adjustments: list[dict[str, Any]] = []

        for pos in current_positions:
            is_profitable = pos.get("unrealized_pnl", 0) > 0

            # Don't close winners by default
            if is_profitable and not self.config.close_winners:
                continue

            # Check concentration drift
            symbol = pos.get("symbol", "")
            notional = pos.get("notional", 0)
            if portfolio.total_equity > 0:
                concentration = notional / portfolio.total_equity * 100
                if concentration > self.config.max_single_asset_pct:
                    adjustments.append(
                        {
                            "symbol": symbol,
                            "action": "reduce",
                            "current_pct": round(concentration, 2),
                            "target_pct": self.config.max_single_asset_pct,
                            "reason": "concentration_drift",
                        }
                    )

        return adjustments

    # ------------------------------------------------------------------
    # Capacity report
    # ------------------------------------------------------------------

    def generate_capacity_report(
        self,
        decisions: list[AllocationDecision],
        portfolio: PortfolioState,
    ) -> dict[str, Any]:
        """Generate a comprehensive capacity and allocation report."""
        allocated = [d for d in decisions if d.is_allocated]
        rejected = [d for d in decisions if not d.is_allocated]

        by_asset: dict[str, float] = {}
        by_strategy: dict[str, float] = {}
        by_exchange: dict[str, float] = {}
        for d in allocated:
            asset = self._extract_base_asset(d.symbol)
            by_asset[asset] = by_asset.get(asset, 0.0) + d.allocated_capital
            by_strategy[d.strategy_id] = by_strategy.get(d.strategy_id, 0.0) + d.allocated_capital
            by_exchange[d.exchange] = by_exchange.get(d.exchange, 0.0) + d.allocated_capital

        total_allocated = sum(d.allocated_capital for d in allocated)

        return {
            "portfolio_equity": portfolio.total_equity,
            "total_opportunities": len(decisions),
            "allocated_count": len(allocated),
            "rejected_count": len(rejected),
            "total_allocated": round(total_allocated, 2),
            "capital_utilization_pct": round(total_allocated / portfolio.total_equity * 100, 2)
            if portfolio.total_equity > 0
            else 0.0,
            "unused_capital": round(portfolio.total_equity - total_allocated, 2),
            "by_asset": by_asset,
            "by_strategy": by_strategy,
            "by_exchange": by_exchange,
            "avg_allocation_score": float(
                sum(d.allocation_score for d in allocated) / len(allocated)
            )
            if allocated
            else 0.0,
            "rejection_reasons": {
                d.symbol: d.rejection_reason for d in rejected if d.rejection_reason
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_base_asset(symbol: str) -> str:
        """Extract base asset from symbol like 'BTC-USD' → 'BTC'."""
        if "-" in symbol:
            return symbol.split("-")[0]
        return symbol
