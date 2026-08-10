"""Opportunity Engine — unified decimal-fraction unit contract (Round 7).

ALL internal return/cost values use DECIMAL FRACTIONS.
  0.0025 = 0.25%
  0.001 = 0.10%
  0.0005 = 0.05%

Field renamed: min_net_return_pct → min_net_return (decimal fraction, not %).

Economic threshold (default 0.002):
  Must cover: fees(0.002) + spread(~0.0005) + slippage(~0.0005) ≈ 0.003
  Default 0.002 means we require approximately breakeven + small buffer.
  Configurable via config or constructor.
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
    gross_return: float = 0.0
    fees: float = 0.0
    spread_cost: float = 0.0
    slippage: float = 0.0
    net_return: float = 0.0
    fill_probability: float = 1.0
    liquidity_discount: float = 1.0
    correlation_penalty: float = 0.0
    strategy_expectancy_bonus: float = 0.0
    final_score: float = 0.0
    market_impact_bps: float = 0.0
    max_efficient_size: float = 0.0
    capacity_utilization: float = 0.0
    diversification_score: float = 1.0


@dataclass
class EvaluatedOpportunity:
    signal: StrategySignal
    score: OpportunityScore = field(default_factory=OpportunityScore)
    status: OpportunityStatus = OpportunityStatus.DETECTED
    rejection_reason: RejectionReason | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    available_liquidity: float | None = None
    correlation_with_positions: float = 0.0
    max_efficient_capital: float = 0.0
    expected_fill_pct: float = 100.0
    expected_market_impact_bps: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class OpportunityEngine:
    """Evaluates, scores, and ranks trading opportunities.

    ROUND 7: Unified decimal-fraction contract.
    min_net_return is a decimal fraction (0.002 = 0.2% minimum net edge).
    Renamed from min_net_return_pct to remove misleading _pct suffix.
    """

    def __init__(
        self,
        min_confidence: float = 0.5,
        min_net_return: float = 0.002,
        min_fill_probability: float = 0.5,
        max_correlation: float = 0.7,
    ) -> None:
        self.min_confidence = min_confidence
        self.min_net_return = min_net_return  # R7: decimal fraction
        self.min_fill_probability = min_fill_probability
        self.max_correlation = max_correlation
        self._strategy_performance: dict[str, float] = {}

    def update_strategy_performance(self, strategy_id: str, expectancy: float) -> None:
        self._strategy_performance[strategy_id] = expectancy

    def evaluate(self, signal: StrategySignal) -> EvaluatedOpportunity:
        opp = EvaluatedOpportunity(signal=signal)

        if signal.is_expired:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.EXPIRED_SIGNAL
            return opp

        if signal.confidence < self.min_confidence:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.OTHER
            return opp

        gross = signal.estimated_return or 0.0
        fees = self._estimate_fees(signal)
        spread = self._estimate_spread(signal)
        slippage = self._estimate_slippage(signal)
        net = gross - fees - spread - slippage

        score = OpportunityScore(
            gross_return=gross, fees=fees, spread_cost=spread, slippage=slippage, net_return=net
        )

        liquidity = signal.metadata.get("available_liquidity")
        if liquidity is not None and signal.required_capital:
            fill_ratio = min(float(liquidity) / signal.required_capital, 1.0)
            score.fill_probability = fill_ratio
            score.liquidity_discount = 1.0 - fill_ratio
        opp.available_liquidity = float(liquidity) if liquidity is not None else None

        expectancy = self._strategy_performance.get(signal.strategy_id, 0.0)
        score.strategy_expectancy_bonus = max(expectancy, 0.0) * 0.1
        score.correlation_penalty = 0.0

        score.final_score = (
            (score.net_return * 0.6)
            + (score.fill_probability * 0.2)
            + (score.strategy_expectancy_bonus * 0.1)
            - (score.correlation_penalty * 0.1)
        )
        opp.score = score

        # R7: net_return (decimal fraction) vs min_net_return (also decimal fraction)
        if score.net_return < self.min_net_return:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.OTHER
            return opp

        if score.fill_probability < self.min_fill_probability:
            opp.status = OpportunityStatus.REJECTED
            opp.rejection_reason = RejectionReason.LIQUIDITY
            return opp

        opp.status = OpportunityStatus.RANKED
        return opp

    def evaluate_batch(self, signals: list[StrategySignal]) -> list[EvaluatedOpportunity]:
        evaluated: list[EvaluatedOpportunity] = []
        for signal in signals:
            opp = self.evaluate(signal)
            if opp.status == OpportunityStatus.RANKED:
                evaluated.append(opp)
        evaluated.sort(key=lambda o: o.score.final_score, reverse=True)
        return evaluated

    def _estimate_fees(self, signal: StrategySignal) -> float:
        taker_fee = float(signal.metadata.get("taker_fee", 0.001))
        return taker_fee * 2  # round trip

    def _estimate_spread(self, signal: StrategySignal) -> float:
        return float(signal.metadata.get("spread_pct", 0.0005))

    def _estimate_slippage(self, signal: StrategySignal) -> float:
        base = 0.0005
        if signal.required_capital and signal.required_capital > 5000:
            return base + 0.0005
        return base
