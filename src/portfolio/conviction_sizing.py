"""Bounded high-conviction sizing inside existing portfolio risk limits.

This module does not change leverage, maximum exposure, the configured maximum
single-position percentage, or protective stops.  It only lets a candidate
with independently strong confidence, entry quality, and net edge use more of
the *already approved* per-position cap than the default risk-budget slice.
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class ConvictionSizingConfig:
    enabled: bool = True
    min_confidence: float = 0.72
    full_confidence: float = 0.90
    min_quality_score: float = 0.70
    full_quality_score: float = 0.85
    min_net_edge_fraction: float = 0.0015
    full_net_edge_fraction: float = 0.0040
    max_multiplier: float = 4.0


@dataclass(frozen=True)
class ConvictionSizingDecision:
    multiplier: float
    is_high_conviction: bool
    reason: str
    confidence_component: float
    quality_component: float
    edge_component: float


class ConvictionSizer:
    """Calculate a bounded allocation multiplier from independent evidence."""

    def __init__(self, config: ConvictionSizingConfig | None = None) -> None:
        self.config = config or ConvictionSizingConfig()

    def assess(
        self,
        *,
        confidence: float,
        entry_quality_score: float,
        expected_net_edge_fraction: float,
    ) -> ConvictionSizingDecision:
        cfg = self.config
        if not cfg.enabled:
            return ConvictionSizingDecision(1.0, False, "disabled", 0.0, 0.0, 0.0)

        if (
            confidence < cfg.min_confidence
            or entry_quality_score < cfg.min_quality_score
            or expected_net_edge_fraction < cfg.min_net_edge_fraction
        ):
            return ConvictionSizingDecision(
                1.0,
                False,
                "base_size_insufficient_independent_conviction",
                0.0,
                0.0,
                0.0,
            )

        confidence_component = _scaled(
            confidence, cfg.min_confidence, cfg.full_confidence
        )
        quality_component = _scaled(
            entry_quality_score, cfg.min_quality_score, cfg.full_quality_score
        )
        edge_component = _scaled(
            expected_net_edge_fraction,
            cfg.min_net_edge_fraction,
            cfg.full_net_edge_fraction,
        )
        # The weakest independent component controls the scale.  A high
        # confidence score alone cannot produce a larger position if expected
        # net edge or execution quality is marginal.
        conviction = min(confidence_component, quality_component, edge_component)
        multiplier = 1.0 + (max(1.0, cfg.max_multiplier) - 1.0) * conviction
        return ConvictionSizingDecision(
            multiplier=multiplier,
            is_high_conviction=multiplier > 1.0,
            reason="bounded_high_conviction",
            confidence_component=confidence_component,
            quality_component=quality_component,
            edge_component=edge_component,
        )


def _scaled(value: float, minimum: float, full: float) -> float:
    if full <= minimum:
        return 1.0 if value >= full else 0.0
    return _clamp((value - minimum) / (full - minimum))
