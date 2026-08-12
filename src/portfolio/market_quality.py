"""Market Quality Score — deterministic, explainable data-driven quality metric.

Calculated from:
- bid/ask spread
- order book depth
- 24h trading volume
- market data freshness
- estimated slippage
- order book stability
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class MarketQualityComponents:
    spread_score: float = 0.0
    depth_score: float = 0.0
    volume_score: float = 0.0
    freshness_score: float = 0.0
    slippage_score: float = 0.0
    total_score: float = 0.0


class MarketQualityCalculator:
    """Computes a composite Market Quality Score [0.0, 1.0].

    Weights:
    - Spread: 25%
    - Depth: 25%
    - Volume: 20%
    - Data Freshness: 15%
    - Slippage: 15%
    """

    @staticmethod
    def compute(
        spread_bps: float,
        depth_usd_10bps: float,
        volume_24h_usd: float,
        data_age_seconds: float,
        expected_slippage_bps: float = 0.0,
        max_acceptable_spread_bps: float = 50.0,
        target_depth_usd: float = 50_000.0,
        max_acceptable_age_sec: float = 60.0,
        max_acceptable_slippage_bps: float = 30.0,
    ) -> MarketQualityComponents:
        # Spread score: 0 bps -> 1.0, max_acceptable_spread_bps -> 0.0
        spread_score = max(0.0, min(1.0, 1.0 - (spread_bps / max(1.0, max_acceptable_spread_bps))))

        # Depth score: target_depth_usd -> 1.0
        depth_score = max(0.0, min(1.0, depth_usd_10bps / max(1.0, target_depth_usd)))

        # Volume score: log10 scale from $1k (0) to $100M (1.0)
        if volume_24h_usd > 1000.0:
            vol_score = max(0.0, min(1.0, (math.log10(volume_24h_usd) - 3.0) / 5.0))
        else:
            vol_score = 0.0

        # Freshness score: 0s -> 1.0, max_acceptable_age_sec -> 0.0
        freshness_score = max(
            0.0, min(1.0, 1.0 - (data_age_seconds / max(1.0, max_acceptable_age_sec)))
        )

        # Slippage score: 0 bps -> 1.0, max_acceptable_slippage_bps -> 0.0
        slippage_score = max(
            0.0,
            min(1.0, 1.0 - (expected_slippage_bps / max(1.0, max_acceptable_slippage_bps))),
        )

        # Weighted total
        total = (
            0.25 * spread_score
            + 0.25 * depth_score
            + 0.20 * vol_score
            + 0.15 * freshness_score
            + 0.15 * slippage_score
        )

        return MarketQualityComponents(
            spread_score=round(spread_score, 4),
            depth_score=round(depth_score, 4),
            volume_score=round(vol_score, 4),
            freshness_score=round(freshness_score, 4),
            slippage_score=round(slippage_score, 4),
            total_score=round(total, 4),
        )
