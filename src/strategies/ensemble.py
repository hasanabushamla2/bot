"""Regime-aware strategy ensemble selector.

More strategy modules should create more *distinct candidate setups*, not
multiple simultaneous entries for the same symbol.  This selector routes each
candidate to the compatible market regime and keeps the strongest long-only
candidate per symbol before the opportunity/risk pipeline.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from src.features.engine import InstrumentFeatures
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.regime import MarketRegime, MarketRegimeClassifier


@dataclass(frozen=True)
class EnsembleRejection:
    signal: StrategySignal
    reason: str


@dataclass
class EnsembleSelection:
    selected: list[StrategySignal] = field(default_factory=list)
    rejected: list[EnsembleRejection] = field(default_factory=list)


class StrategyEnsembleSelector:
    """Choose one compatible candidate per symbol without changing risk limits."""

    _TREND_STRATEGIES = {
        "liquid_alt_trend_v1",
        "breakout_v1",
        "momentum_v1",
        "pullback_continuation_v1",
    }

    def __init__(self, classifier: MarketRegimeClassifier | None = None) -> None:
        self.classifier = classifier or MarketRegimeClassifier()

    def select(
        self,
        signals: list[StrategySignal],
        feature_for_symbol: Callable[[str], InstrumentFeatures],
    ) -> EnsembleSelection:
        grouped: dict[str, list[tuple[float, StrategySignal]]] = defaultdict(list)
        rejected: list[EnsembleRejection] = []

        for signal in signals:
            symbol = signal.symbol or ""
            if not symbol:
                rejected.append(EnsembleRejection(signal, "ensemble_missing_symbol"))
                continue
            if signal.direction != SignalDirection.LONG:
                rejected.append(EnsembleRejection(signal, "ensemble_spot_long_only"))
                continue

            regime = self.classifier.assess(feature_for_symbol(symbol))
            compatibility = self._compatibility(signal.strategy_id, regime.regime)
            if compatibility <= 0.0:
                rejected.append(
                    EnsembleRejection(signal, f"ensemble_regime_{regime.regime.value}")
                )
                continue
            score = signal.confidence * compatibility
            signal.metadata["ensemble_score"] = score
            signal.metadata["ensemble_regime"] = regime.regime.value
            signal.metadata["ensemble_regime_reason"] = regime.reason
            grouped[symbol].append((score, signal))

        selected: list[StrategySignal] = []
        for symbol, candidates in grouped.items():
            candidates.sort(key=lambda item: item[0], reverse=True)
            selected.append(candidates[0][1])
            for _, discarded in candidates[1:]:
                rejected.append(EnsembleRejection(discarded, "ensemble_symbol_competition"))

        selected.sort(
            key=lambda signal: float(signal.metadata.get("ensemble_score", signal.confidence)),
            reverse=True,
        )
        return EnsembleSelection(selected=selected, rejected=rejected)

    def _compatibility(self, strategy_id: str, regime: MarketRegime) -> float:
        if regime == MarketRegime.HIGH_RISK:
            return 0.0
        if regime == MarketRegime.UPTREND:
            if strategy_id == "range_mean_reversion_v1":
                return 0.0
            if strategy_id in self._TREND_STRATEGIES:
                return 1.0
            if strategy_id == "order_flow_v1":
                return 0.82
            if strategy_id == "global_scanner":
                return 0.70
            return 0.55
        if regime == MarketRegime.RANGE:
            if strategy_id == "range_mean_reversion_v1":
                return 1.0
            if strategy_id == "order_flow_v1":
                return 0.65
            if strategy_id == "global_scanner":
                return 0.45
            return 0.0
        # Transition can provide trend breakouts, but deliberately discounts
        # their score versus a mature uptrend.
        if strategy_id in {"liquid_alt_trend_v1", "breakout_v1", "momentum_v1"}:
            return 0.72
        if strategy_id == "pullback_continuation_v1":
            return 0.60
        if strategy_id == "order_flow_v1":
            return 0.55
        if strategy_id == "global_scanner":
            return 0.50
        return 0.35
