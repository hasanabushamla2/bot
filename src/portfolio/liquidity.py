"""Liquidity Analyzer — order-book depth, volume, spread, and market-impact estimation.

Provides the foundation for capacity-aware position sizing. Every opportunity
must pass a liquidity assessment before capital is allocated.

Key outputs:
- Effective spread at different order sizes
- Order-book depth available before N bps of slippage
- Estimated market impact for a given notional
- Maximum efficient position size before edge deteriorates
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from src.adapters.base import NormalizedOrderBook, NormalizedOrderBookLevel
from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class LiquidityMetrics:
    """Quantitative liquidity assessment for one instrument at one point in time."""

    symbol: str
    exchange: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Spread
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0

    # Depth — cumulative quantity available within N bps of mid
    depth_1bps: float = 0.0
    depth_5bps: float = 0.0
    depth_10bps: float = 0.0
    depth_25bps: float = 0.0
    depth_50bps: float = 0.0

    # Volume
    volume_24h: float = 0.0
    volume_1h: float = 0.0

    # Market-impact model: estimated slippage for given notional sizes
    impact_1k: float = 0.0
    impact_5k: float = 0.0
    impact_10k: float = 0.0
    impact_25k: float = 0.0
    impact_50k: float = 0.0
    impact_100k: float = 0.0

    # Derived
    max_efficient_notional: float = 0.0
    liquidity_score: float = 0.0  # 0.0 (illiquid) to 1.0 (extremely liquid)

    metadata: dict[str, Any] = field(default_factory=dict)


class LiquidityAnalyzer:
    """Analyzes market liquidity from order books and ticker data.

    Computes:
    - Spread analysis
    - Depth at multiple levels
    - Market-impact estimation using a square-root model
    - Maximum efficient notional (point where marginal edge ≤ threshold)
    - Composite liquidity score

    The market-impact model is a simplified square-root model:
        impact_bps = spread_bps/2 + k * sqrt(notional / avg_daily_volume)
    where k is a configurable scaling factor.

    This model is NOT a guarantee — it's a conservative estimate.
    Real impact varies by market conditions.
    """

    def __init__(
        self,
        impact_k: float = 0.1,
        max_impact_threshold_bps: float = 10.0,
        min_liquidity_score: float = 0.3,
    ) -> None:
        self.impact_k = impact_k
        self.max_impact_threshold_bps = max_impact_threshold_bps
        self.min_liquidity_score = min_liquidity_score

    def analyze(
        self,
        order_book: NormalizedOrderBook | None = None,
        volume_24h: float = 0.0,
        volume_1h: float = 0.0,
    ) -> LiquidityMetrics:
        """Analyze liquidity for one instrument snapshot.

        Args:
            order_book: Current order book snapshot (may be None).
            volume_24h: 24-hour trading volume in quote currency.
            volume_1h: 1-hour trading volume in quote currency.

        Returns:
            LiquidityMetrics with all computed values.
        """
        metrics = LiquidityMetrics(
            symbol=order_book.symbol if order_book else "unknown",
            exchange=order_book.exchange if order_book else "unknown",
        )

        if order_book is None or not order_book.bids or not order_book.asks:
            metrics.liquidity_score = 0.0
            return metrics

        best_bid = order_book.bids[0].price
        best_ask = order_book.asks[0].price

        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            metrics.liquidity_score = 0.0
            return metrics

        mid = (best_bid + best_ask) / 2.0
        metrics.bid = best_bid
        metrics.ask = best_ask
        metrics.spread_pct = (best_ask - best_bid) / mid * 100.0

        # --- Depth at multiple levels ---
        metrics.depth_1bps = self._depth_at_bps(order_book.bids, mid, 1)
        metrics.depth_5bps = self._depth_at_bps(order_book.bids, mid, 5)
        metrics.depth_10bps = self._depth_at_bps(order_book.bids, mid, 10)
        metrics.depth_25bps = self._depth_at_bps(order_book.bids, mid, 25)
        metrics.depth_50bps = self._depth_at_bps(order_book.bids, mid, 50)

        # --- Volume ---
        metrics.volume_24h = volume_24h
        metrics.volume_1h = volume_1h

        # --- Market Impact estimation ---
        avg_daily_vol = max(volume_24h, volume_1h * 24.0, 1.0)
        metrics.impact_1k = self._estimate_impact(1_000, metrics.spread_pct, avg_daily_vol)
        metrics.impact_5k = self._estimate_impact(5_000, metrics.spread_pct, avg_daily_vol)
        metrics.impact_10k = self._estimate_impact(10_000, metrics.spread_pct, avg_daily_vol)
        metrics.impact_25k = self._estimate_impact(25_000, metrics.spread_pct, avg_daily_vol)
        metrics.impact_50k = self._estimate_impact(50_000, metrics.spread_pct, avg_daily_vol)
        metrics.impact_100k = self._estimate_impact(100_000, metrics.spread_pct, avg_daily_vol)

        # --- Max efficient notional ---
        metrics.max_efficient_notional = self._max_efficient_notional(
            metrics.spread_pct, avg_daily_vol
        )

        # --- Composite liquidity score ---
        metrics.liquidity_score = self._compute_liquidity_score(metrics)

        return metrics

    def _depth_at_bps(self, levels: list[NormalizedOrderBookLevel], mid: float, bps: int) -> float:
        """Cumulative quantity available within `bps` basis points of mid price."""
        threshold = mid * (1 - bps / 10000.0)
        total_qty = 0.0
        for level in levels:
            if level.price >= threshold:
                total_qty += level.quantity
            else:
                break
        return total_qty

    def _estimate_impact(
        self, notional: float, spread_bps: float, avg_daily_volume: float
    ) -> float:
        """Estimate total market impact in basis points.

        Uses square-root model: half-spread + k * sqrt(notional / ADV).
        """
        if avg_daily_volume <= 0:
            return spread_bps + 50.0  # Very conservative fallback
        participation = notional / avg_daily_volume
        impact = (spread_bps / 2.0) + self.impact_k * math.sqrt(participation) * 10000.0
        return round(impact, 2)

    def _max_efficient_notional(self, spread_bps: float, avg_daily_volume: float) -> float:
        """Compute the maximum notional where estimated impact ≤ threshold.

        Solves: spread_bps/2 + k * sqrt(N / ADV) * 10000 ≤ threshold
        for N.
        """
        if avg_daily_volume <= 0 or self.impact_k <= 0:
            return 0.0

        allowed_impact = self.max_impact_threshold_bps - spread_bps / 2.0
        if allowed_impact <= 0:
            return 0.0

        # N = ADV * (allowed_impact / (k * 10000))^2
        ratio = allowed_impact / (self.impact_k * 10000.0)
        max_notional = avg_daily_volume * ratio * ratio
        return round(max_notional, 2)

    def _compute_liquidity_score(self, m: LiquidityMetrics) -> float:
        """Composite liquidity score 0-1 from multiple factors."""
        scores: list[float] = []

        # Spread: narrower is better
        # 0 bps → 1.0, 50 bps → 0.0
        spread_score = max(0.0, 1.0 - m.spread_pct / 50.0)
        scores.append(spread_score)

        # Depth at 10bps relative to a reference (e.g., $10k worth at mid)
        mid_price = (m.bid + m.ask) / 2.0 if m.bid > 0 else 1.0
        depth_notional = m.depth_10bps * mid_price
        depth_score = min(1.0, depth_notional / 50_000.0)
        scores.append(depth_score)

        # Volume: log-scale score
        if m.volume_24h > 0:
            vol_score = min(1.0, math.log10(m.volume_24h) / math.log10(1_000_000_000))
        else:
            vol_score = 0.0
        scores.append(vol_score)

        # Max efficient notional
        cap_score = min(1.0, m.max_efficient_notional / 250_000.0)
        scores.append(cap_score)

        return float(np.mean(scores))
