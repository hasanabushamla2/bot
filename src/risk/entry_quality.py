"""Adaptive pre-entry quality assessment.

This module is deliberately a *market-normalized* gate, not a collection of
price-specific rules.  It compares momentum, spread, breakout extension,
volume, and order-book pressure with the instrument's recent realized
volatility and local observation history.  It never reads future candles.

The gate is intended to reduce the common hard-stop pattern where a signal has
little favourable excursion (MFE) before reversing.  It does not create take
profits, change hard stops, or increase position size.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import median
from typing import Any

from src.features.engine import InstrumentFeatures
from src.strategies.base import StrategySignal


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _quantile(values: list[float], q: float, default: float) -> float:
    """Small dependency-free percentile helper for an already live history."""
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return default
    if len(finite) == 1:
        return finite[0]
    bounded_q = _clamp(q)
    idx = (len(finite) - 1) * bounded_q
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return finite[lo]
    return finite[lo] + (finite[hi] - finite[lo]) * (idx - lo)


@dataclass
class EntryQualityConfig:
    """Controls for normalized entry quality; none are price-level constants."""

    rolling_window: int = 120
    min_history_observations: int = 12
    min_quality_score: float = 0.55
    dynamic_score_quantile: float = 0.55
    min_normalized_momentum: float = 0.35
    momentum_quantile: float = 0.55
    minimum_noise_pct: float = 0.01
    min_signal_persistence_observations: int = 2
    strong_first_observation_confidence: float = 0.85
    max_reversal_risk: float = 0.60
    max_dynamic_score_uplift: float = 0.12


@dataclass
class _Baseline:
    momentum_multiples: deque[float]
    quality_scores: deque[float]
    volatility_pct: deque[float]
    relative_volume: deque[float]
    spread_bps: deque[float]
    trend_abs: deque[float]
    breakout_position: deque[float]
    imbalance: deque[float]

    @classmethod
    def create(cls, maxlen: int) -> _Baseline:
        return cls(
            momentum_multiples=deque(maxlen=maxlen),
            quality_scores=deque(maxlen=maxlen),
            volatility_pct=deque(maxlen=maxlen),
            relative_volume=deque(maxlen=maxlen),
            spread_bps=deque(maxlen=maxlen),
            trend_abs=deque(maxlen=maxlen),
            breakout_position=deque(maxlen=maxlen),
            imbalance=deque(maxlen=maxlen),
        )

    @property
    def count(self) -> int:
        return len(self.volatility_pct)


@dataclass
class EntryQualityAssessment:
    passed: bool
    quality_score: float
    required_score: float
    momentum_multiple: float
    required_momentum_multiple: float
    reversal_risk: float
    market_structure_score: float
    volatility_pct: float
    signal_persistence: int
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float | int] = field(default_factory=dict)


class EntryQualityGate:
    """Score one candidate against live, rolling market behavior.

    ``observe_market`` must be called with each contemporaneously available
    market snapshot.  The gate only stores values observed at or before entry;
    it contains no candle close-ahead or future excursion data.
    """

    def __init__(self, config: EntryQualityConfig | None = None) -> None:
        self.config = config or EntryQualityConfig()
        self._baselines: dict[str, _Baseline] = {}

    def _baseline(self, symbol: str) -> _Baseline:
        if symbol not in self._baselines:
            self._baselines[symbol] = _Baseline.create(self.config.rolling_window)
        return self._baselines[symbol]

    def observe_market(self, features: InstrumentFeatures) -> None:
        """Add a live market observation to the symbol's rolling baseline."""
        if not features.symbol or features.last_price <= 0:
            return
        baseline = self._baseline(features.symbol)
        volatility = self._noise_pct(features, baseline)
        directional_momentum = 0.60 * features.momentum_1m + 0.40 * features.momentum_5m
        momentum_multiple = abs(directional_momentum) / max(volatility, self.config.minimum_noise_pct)
        imbalance = self._book_imbalance(features)
        # A neutral-direction preliminary score lets the rolling score history
        # reflect current market quality without presuming an entry direction.
        preliminary = self._preliminary_score(features, volatility, momentum_multiple, imbalance)

        baseline.momentum_multiples.append(momentum_multiple)
        baseline.quality_scores.append(preliminary)
        baseline.volatility_pct.append(volatility)
        baseline.relative_volume.append(max(0.0, features.relative_volume))
        baseline.spread_bps.append(max(0.0, features.spread_bps))
        baseline.trend_abs.append(abs(features.trend_strength))
        baseline.breakout_position.append(_clamp(features.breakout_position_pct, 0.0, 100.0))
        baseline.imbalance.append(imbalance)

    def assess(self, signal: StrategySignal, features: InstrumentFeatures) -> EntryQualityAssessment:
        """Assess a signal using only its current/live feature snapshot."""
        symbol = signal.symbol or features.symbol
        baseline = self._baseline(symbol)
        direction = signal.direction.value
        side = 1.0 if direction == "long" else -1.0
        volatility = self._noise_pct(features, baseline)
        noise = max(volatility, self.config.minimum_noise_pct)
        directed_momentum_pct = side * (
            0.60 * features.momentum_1m + 0.40 * features.momentum_5m
        )
        momentum_multiple = directed_momentum_pct / noise
        historic_momentum = _quantile(
            list(baseline.momentum_multiples), self.config.momentum_quantile, 0.0
        )
        required_momentum = max(self.config.min_normalized_momentum, historic_momentum)

        directed_trend = side * features.trend_strength
        breakout_score = self._breakout_score(signal, features, side, baseline)
        volume_score = self._volume_score(features, baseline)
        liquidity_score = self._liquidity_score(features, volatility, baseline)
        flow_score = self._flow_score(features, side, baseline)
        momentum_score = _clamp(momentum_multiple / max(required_momentum, 1e-9))
        trend_score = self._trend_score(directed_trend, baseline)
        persistence = self._signal_persistence(signal)
        persistence_score = self._persistence_score(signal.confidence, persistence)
        reversal_risk = self._reversal_risk(features, side, noise, flow_score)

        # The weights intentionally reward confirmation from independent
        # sources.  A single fast move cannot compensate for adverse trend,
        # liquidity, or reversal conditions.
        quality_score = _clamp(
            momentum_score * 0.24
            + trend_score * 0.20
            + breakout_score * 0.15
            + volume_score * 0.12
            + liquidity_score * 0.12
            + flow_score * 0.08
            + persistence_score * 0.09
            - reversal_risk * 0.20
        )

        historical_quality = _quantile(
            list(baseline.quality_scores), self.config.dynamic_score_quantile, 0.0
        )
        required_score = self.config.min_quality_score
        if baseline.count >= self.config.min_history_observations:
            # The dynamic component can raise the bar in unusually noisy/poor
            # local conditions, but is bounded so one regime cannot starve a
            # symbol indefinitely solely because of a transient outlier.
            required_score = max(
                required_score,
                min(
                    self.config.min_quality_score + self.config.max_dynamic_score_uplift,
                    historical_quality,
                ),
            )

        reasons: list[str] = []
        if directed_momentum_pct <= 0.0:
            reasons.append("momentum_not_directional")
        elif momentum_multiple < required_momentum:
            reasons.append("momentum_below_volatility_normalized_threshold")
        if directed_trend <= 0.0:
            reasons.append("trend_misaligned")
        if reversal_risk > self.config.max_reversal_risk:
            reasons.append("short_term_reversal_risk")
        if (
            persistence < self.config.min_signal_persistence_observations
            and signal.confidence < self.config.strong_first_observation_confidence
        ):
            reasons.append("signal_not_persistent")
        if quality_score < required_score:
            reasons.append("quality_score_below_dynamic_threshold")

        market_structure_score = _clamp(
            trend_score * 0.45 + breakout_score * 0.25 + flow_score * 0.20 + momentum_score * 0.10
        )
        return EntryQualityAssessment(
            passed=not reasons,
            quality_score=quality_score,
            required_score=required_score,
            momentum_multiple=momentum_multiple,
            required_momentum_multiple=required_momentum,
            reversal_risk=reversal_risk,
            market_structure_score=market_structure_score,
            volatility_pct=volatility,
            signal_persistence=persistence,
            reasons=reasons,
            metrics={
                "baseline_observations": baseline.count,
                "directed_momentum_pct": directed_momentum_pct,
                "directed_trend": directed_trend,
                "breakout_score": breakout_score,
                "volume_score": volume_score,
                "liquidity_score": liquidity_score,
                "flow_score": flow_score,
                "persistence_score": persistence_score,
            },
        )

    def _noise_pct(self, features: InstrumentFeatures, baseline: _Baseline) -> float:
        observed_vol = max(abs(features.atr_pct), abs(features.volatility_5m_pct))
        historical_vol = median(baseline.volatility_pct) if baseline.volatility_pct else 0.0
        # Spread is an immediately observable source of microstructure noise.
        spread_pct = max(0.0, features.spread_bps / 100.0)
        return max(observed_vol, historical_vol, spread_pct, self.config.minimum_noise_pct)

    @staticmethod
    def _book_imbalance(features: InstrumentFeatures) -> float:
        ratio = features.bid_ask_ratio
        if ratio <= 0 or not math.isfinite(ratio):
            return 0.0
        return _clamp((ratio - 1.0) / (ratio + 1.0), -1.0, 1.0)

    def _preliminary_score(
        self,
        features: InstrumentFeatures,
        volatility: float,
        momentum_multiple: float,
        imbalance: float,
    ) -> float:
        momentum = _clamp(momentum_multiple / max(self.config.min_normalized_momentum, 1e-9))
        trend = _clamp(abs(features.trend_strength))
        volume = _clamp(features.relative_volume / max(1.0, features.relative_volume))
        spread_quality = _clamp(1.0 - (features.spread_bps / 100.0) / max(volatility * 3.0, 1e-9))
        flow = _clamp(0.5 + abs(imbalance))
        return _clamp(momentum * 0.35 + trend * 0.25 + volume * 0.15 + spread_quality * 0.15 + flow * 0.10)

    def _breakout_score(
        self,
        signal: StrategySignal,
        features: InstrumentFeatures,
        side: float,
        baseline: _Baseline,
    ) -> float:
        position = _clamp(features.breakout_position_pct, 0.0, 100.0)
        directional_position = position if side > 0 else 100.0 - position
        # Momentum strategies need not be at a range extreme, while a breakout
        # strategy receives a confirmation score only for a directional range
        # location.  The threshold comes from the symbol's recent range
        # placement where enough history exists.
        if signal.entry_logic.get("type") != "breakout":
            return _clamp(0.45 + (directional_position - 50.0) / 100.0)
        historical = _quantile(list(baseline.breakout_position), 0.65, 65.0)
        threshold = max(55.0, historical if side > 0 else 100.0 - historical)
        return _clamp((directional_position - threshold) / max(100.0 - threshold, 1.0))

    def _volume_score(self, features: InstrumentFeatures, baseline: _Baseline) -> float:
        historical = _quantile(list(baseline.relative_volume), 0.50, 1.0)
        target = max(1.0, historical)
        return _clamp(max(0.0, features.relative_volume) / target / 1.5)

    def _liquidity_score(
        self, features: InstrumentFeatures, volatility: float, baseline: _Baseline
    ) -> float:
        historical_spread = _quantile(list(baseline.spread_bps), 0.80, features.spread_bps)
        spread_pct = max(0.0, features.spread_bps / 100.0)
        local_budget = max(volatility, historical_spread / 100.0, self.config.minimum_noise_pct)
        # A spread around one local volatility unit is acceptable; a spread
        # much larger than local noise has already consumed too much edge.
        return _clamp(1.0 - spread_pct / max(local_budget * 2.0, 1e-9))

    def _flow_score(self, features: InstrumentFeatures, side: float, baseline: _Baseline) -> float:
        directed = side * self._book_imbalance(features)
        flow_ratio = features.trade_flow_ratio
        if flow_ratio > 0 and math.isfinite(flow_ratio):
            flow_component = side * math.tanh(math.log(flow_ratio))
        else:
            flow_component = 0.0
        historical = _quantile([abs(v) for v in baseline.imbalance], 0.50, 0.0)
        # Neutral books are not rejected: OBI is an optional confirmation.
        return _clamp(0.50 + 0.30 * directed + 0.20 * flow_component - 0.10 * historical)

    def _trend_score(self, directed_trend: float, baseline: _Baseline) -> float:
        historical = _quantile(list(baseline.trend_abs), 0.50, 0.20)
        denominator = max(historical, 0.20)
        return _clamp(max(0.0, directed_trend) / denominator)

    def _signal_persistence(self, signal: StrategySignal) -> int:
        raw = signal.metadata.get("signal_observations", 1)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _persistence_score(self, confidence: float, observations: int) -> float:
        target = max(1, self.config.min_signal_persistence_observations)
        if observations >= target:
            return 1.0
        # A genuinely high-confidence first observation is allowed to score
        # well, but still receives less than a confirmed persistence sequence.
        high_conf = _clamp(confidence / max(self.config.strong_first_observation_confidence, 1e-9))
        return _clamp(0.45 + high_conf * 0.35)

    def _reversal_risk(
        self,
        features: InstrumentFeatures,
        side: float,
        noise_pct: float,
        flow_score: float,
    ) -> float:
        directed_acceleration = side * features.acceleration
        directed_fast = side * features.momentum_1m
        directed_slow = side * features.momentum_5m
        momentum_conflict = 1.0 if directed_fast <= 0 or directed_slow <= 0 else 0.0
        acceleration_risk = _clamp(max(0.0, -directed_acceleration) / max(noise_pct, 1e-9))
        # Being far from VWAP relative to realized noise is a chase/reversion
        # risk, regardless of how attractive the raw momentum appears.
        extension_risk = _clamp(
            max(0.0, abs(features.vwap_deviation_pct) / max(noise_pct * 2.0, 1e-9) - 1.0)
        )
        weak_flow = _clamp((0.50 - flow_score) * 2.0)
        return _clamp(
            momentum_conflict * 0.35
            + acceleration_risk * 0.25
            + extension_risk * 0.25
            + weak_flow * 0.15
        )

    def get_state(self) -> dict[str, Any]:
        """Return a compact inspectable summary (not used as a price source)."""
        return {
            symbol: {
                "observations": baseline.count,
                "median_volatility_pct": median(baseline.volatility_pct)
                if baseline.volatility_pct
                else 0.0,
                "median_momentum_multiple": median(baseline.momentum_multiples)
                if baseline.momentum_multiples
                else 0.0,
            }
            for symbol, baseline in self._baselines.items()
        }
