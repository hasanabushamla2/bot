"""Live, no-look-ahead market-regime classification for spot strategies.

The classifier uses only the current feature snapshot and rolling measurements
already available to the feature engine.  It deliberately does not infer a
future candle direction; its role is to route a candidate to a compatible
strategy family (trend continuation versus range mean reversion).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.features.engine import InstrumentFeatures


class MarketRegime(str, Enum):
    UPTREND = "uptrend"
    RANGE = "range"
    TRANSITION = "transition"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    noise_pct: float
    normalized_momentum: float
    trend_score: float
    reason: str


@dataclass(frozen=True)
class RegimeConfig:
    """Normalized routing thresholds, not symbol-specific price thresholds."""

    minimum_noise_pct: float = 0.01
    uptrend_trend_floor: float = 0.15
    uptrend_momentum_multiple: float = 0.50
    range_trend_ceiling: float = 0.15
    high_risk_spread_to_noise: float = 2.5


class MarketRegimeClassifier:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()

    def assess(self, features: InstrumentFeatures) -> RegimeAssessment:
        cfg = self.config
        spread_pct = max(0.0, features.spread_bps / 100.0)
        realized_noise_pct = max(
            cfg.minimum_noise_pct,
            abs(features.atr_pct),
            abs(features.volatility_5m_pct),
        )
        noise_pct = max(realized_noise_pct, spread_pct)
        directed_momentum = 0.60 * features.momentum_1m + 0.40 * features.momentum_5m
        momentum_multiple = directed_momentum / noise_pct
        trend_score = features.trend_strength

        if spread_pct > realized_noise_pct * cfg.high_risk_spread_to_noise:
            return RegimeAssessment(
                MarketRegime.HIGH_RISK,
                noise_pct,
                momentum_multiple,
                trend_score,
                "spread_dominates_realized_noise",
            )
        if (
            trend_score >= cfg.uptrend_trend_floor
            and features.momentum_5m > 0.0
            and momentum_multiple >= cfg.uptrend_momentum_multiple
        ):
            return RegimeAssessment(
                MarketRegime.UPTREND,
                noise_pct,
                momentum_multiple,
                trend_score,
                "trend_and_momentum_aligned",
            )
        if (
            abs(trend_score) <= cfg.range_trend_ceiling
            and abs(momentum_multiple) < cfg.uptrend_momentum_multiple
        ):
            return RegimeAssessment(
                MarketRegime.RANGE,
                noise_pct,
                momentum_multiple,
                trend_score,
                "low_directional_trend",
            )
        return RegimeAssessment(
            MarketRegime.TRANSITION,
            noise_pct,
            momentum_multiple,
            trend_score,
            "mixed_trend_momentum",
        )
