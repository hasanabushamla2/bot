"""Opportunity Engine — the heart of opportunity evaluation.

Consumes signals from ALL strategies and ALL connected markets,
computes an Opportunity Score for each, rejects unqualified ones,
and ranks the rest by risk-adjusted expected net value.

The Opportunity Score is NOT simply the largest advertised percentage.
It accounts for fees, spread, slippage, liquidity, fill probability,
correlation, and strategy historical expectancy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger
from src.db.models import OpportunityStatus, RejectionReason
from src.strategies.base import StrategySignal

logger = get_logger(__name__)


@dataclass
class OpportunityScore:
    """Breakdown of how an opportunity score was computed."""

    gross_return: float = 0.0
    fees: float = 0.0  # maker + taker for entry + exit
    spread_cost: float = 0.0
    slippage: float = 0.0
    net_return: float = 0.0
    fill_probability: float = 1.0
    liquidity_discount: float = 1.0
    correlation_penalty: float = 0.0
    strategy_expectancy_bonus: float = 0.0
    final_score: float = 0.0


@dataclass
class EvaluatedOpportunity:
    """A signal that has been evaluated and scored."""

    signal: StrategySignal
    score: OpportunityScore = field(default_factory=OpportunityScore)
    status: OpportunityStatus = OpportunityStatus.DETECTED
    rejection_reason: RejectionReason | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Opportunity metadata
    available_liquidity: float | None = None
    correlation_with_positions: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class OpportunityEngine:
    """Evaluates, scores, and ranks trading opportunities.

    The engine:
    1. Receives raw signals from strategy plugins.
    2. Computes expected net return (gross - fees - spread - slippage).
    3. Adjusts for fill probability, liquidity, correlation, and strategy performance.
    4. Produces a final opportunity score.
    5. Rejects opportunities that don't meet minimum thresholds.
    6. Ranks remaining opportunities for the Risk Engine.
    """

    def __init__(
        self,
        min_confidence: float = 0.5,
        min_net_return_pct: float = 0.05,
        min_fill_probability: float = 0.5,
        max_correlation: float = 0.7,
    ) -> None:
        self.min_confidence = min_confidence
        self.min_net_return_pct = min_net_return_pct
        self.min_fill_probability = min_fill_probability
        self.max_correlation = max_correlation

        # Strategy historical performance (updated periodically)
        self._strategy_performance: dict[str, float] = {}  # strategy_id -> expectancy

    def update_strategy_performance(self, strategy_id: str, expectancy: float) -> None:
        """Update the historical expectancy for a strategy."""
        self._strategy_performance[strategy_id] = expectancy

    def evaluate(self, signal: StrategySignal) -> EvaluatedOpportunity:
        """Evaluate a single signal and produce a scored opportunity.

        Args:
            signal: Raw strategy signal.

        Returns:
            EvaluatedOpportunity with score and status.
        """
        opp = EvaluatedOpportunity(signal=signal)

        # --- Stage 0: Expiration check ---
        if signal.is_expired:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.EXPIRED_SIGNAL
            logger.debug("opportunity_rejected_expired", strategy=signal.strategy_id)
            return opp

        # --- Stage 1: Confidence gate ---
        if signal.confidence < self.min_confidence:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.OTHER
            logger.debug("opportunity_rejected_low_confidence",
                         strategy=signal.strategy_id, confidence=signal.confidence)
            return opp

        # --- Stage 2: Compute expected net return ---
        gross = signal.estimated_return or 0.0
        fees = self._estimate_fees(signal)
        spread = self._estimate_spread(signal)
        slippage = self._estimate_slippage(signal)
        net = gross - fees - spread - slippage

        score = OpportunityScore(
            gross_return=gross,
            fees=fees,
            spread_cost=spread,
            slippage=slippage,
            net_return=net,
        )

        # --- Stage 3: Liquidity discount ---
        liquidity = signal.metadata.get("available_liquidity", None)
        if liquidity is not None and signal.required_capital:
            fill_ratio = min(liquidity / signal.required_capital, 1.0)
            score.fill_probability = fill_ratio
            score.liquidity_discount = 1.0 - fill_ratio

        opp.available_liquidity = liquidity

        # --- Stage 4: Strategy performance bonus ---
        expectancy = self._strategy_performance.get(signal.strategy_id, 0.0)
        score.strategy_expectancy_bonus = max(expectancy, 0.0) * 0.1  # 10% weight

        # --- Stage 5: Correlation penalty (placeholder — RISK engine provides) ---
        score.correlation_penalty = 0.0  # Computed by Risk Engine

        # --- Stage 6: Final score ---
        # Weighted combination: net return (60%) + fill prob (20%) +
        #   strategy bonus (10%) - correlation penalty (10%)
        score.final_score = (
            (score.net_return * 0.6)
            + (score.fill_probability * 0.2)
            + (score.strategy_expectancy_bonus * 0.1)
            - (score.correlation_penalty * 0.1)
        )

        opp.score = score

        # --- Stage 7: Minimum thresholds ---
        if score.net_return < self.min_net_return_pct:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.OTHER
            return opp

        if score.fill_probability < self.min_fill_probability:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.LIQUIDITY
            return opp

        # --- Passed all checks ---
        opp.status = OpportunityStatus.RANKED
        return opp

    def evaluate_batch(self, signals: list[StrategySignal]) -> list[EvaluatedOpportunity]:
        """Evaluate multiple signals and return ranked results.

        Only RANKED opportunities are returned, sorted by score descending.
        Rejected opportunities are logged but not returned.
        """
        evaluated: list[EvaluatedOpportunity] = []
        for signal in signals:
            opp = self.evaluate(signal)
            if opp.status == OpportunityStatus.RANKED:
                evaluated.append(opp)
            else:
                logger.debug(
                    "opportunity_rejected",
                    strategy=signal.strategy_id,
                    symbol=signal.symbol,
                    reason=opp.rejection_reason.value if opp.rejection_reason else "unknown",
                )

        # Sort by score descending
        evaluated.sort(key=lambda o: o.score.final_score, reverse=True)
        return evaluated

    # --- Cost estimation methods (overridable, configurable) ---

    def _estimate_fees(self, signal: StrategySignal) -> float:
        """Estimate round-trip fees as percentage of trade value."""
        # Default: 0.1% taker fee each way = 0.2% round trip
        # Override with exchange-specific fee data when available
        taker_fee = signal.metadata.get("taker_fee", 0.001)
        return taker_fee * 2  # entry + exit

    def _estimate_spread(self, signal: StrategySignal) -> float:
        """Estimate spread cost as percentage."""
        return signal.metadata.get("spread_pct", 0.0005)  # default 5 bps

    def _estimate_slippage(self, signal: StrategySignal) -> float:
        """Estimate slippage as percentage of trade value."""
        # Base + size-dependent component
        base = 0.0005  # 5 bps
        size_multiplier = 0.0
        if signal.required_capital and signal.required_capital > 5000:
            size_multiplier = 0.0005  # additional 5 bps for larger orders
        return base + size_multiplier
