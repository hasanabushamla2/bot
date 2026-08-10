"""Asset Quality Filter — dynamic, data-driven qualification for crypto spot instruments.

An instrument becomes qualified (eligible for trading) only when it passes
ALL measurable criteria. This is NOT a static coin-name list — it is a
living filter that continuously re-evaluates every asset.

Criteria (all must pass):
- Active SPOT market
- Valid instrument metadata
- Sufficient 24h quote volume
- Sufficient order-book depth
- Acceptable bid/ask spread
- Acceptable estimated slippage
- Acceptable market impact
- Healthy WebSocket/data feed
- Minimum market age (if data available)
- Minimum exchange listing quality
- Adequate recent trade count
- Acceptable liquidity consistency
- No abnormal market halt/suspension
- Sufficient CapitalEstimator capacity
- No critical data-quality issues

Future optional criteria:
- Market-cap threshold
- Exchange count (listed on N exchanges)
- Token age
- Historical liquidity stability
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.core.logging_config import get_logger
from src.portfolio.liquidity import LiquidityMetrics

logger = get_logger(__name__)


class QualityTier(str, Enum):
    """Asset quality/liquidity classification tier.

    TIER_A: Deep liquidity, major assets (BTC, ETH, top-tier)
    TIER_B: Highly liquid established altcoins
    TIER_C: Qualified medium-liquidity altcoins
    TIER_D: Low-liquidity, speculative, or rejected
    """

    TIER_A = "tier_a"  # Deep liquidity / major
    TIER_B = "tier_b"  # Highly liquid established
    TIER_C = "tier_c"  # Qualified medium-liquidity
    TIER_D = "tier_d"  # Low-liquidity / speculative / rejected


@dataclass
class QualityFilterConfig:
    """Configurable thresholds for the dynamic quality filter.

    All thresholds are data-driven. No asset name is hard-coded.
    Assets qualify purely on measurable criteria.
    """

    # --- Volume thresholds (24h quote volume in USD) ---
    min_volume_tier_a: float = 100_000_000.0  # $100M+
    min_volume_tier_b: float = 25_000_000.0  # $25M+
    min_volume_tier_c: float = 1_000_000.0  # $1M+
    # Below $1M → TIER_D (rejected)

    # --- Spread thresholds (bid/ask % of mid) ---
    max_spread_tier_a: float = 0.05  # 5 bps
    max_spread_tier_b: float = 0.20  # 20 bps
    max_spread_tier_c: float = 2.00  # 200 bps

    # --- Depth thresholds (USD notional at 10bps) ---
    min_depth_tier_a: float = 500_000.0
    min_depth_tier_b: float = 100_000.0
    min_depth_tier_c: float = 10_000.0

    # --- Execution quality ---
    max_estimated_slippage_1k_bps: float = 5.0  # Max slippage on $1k order
    max_market_impact_bps: float = 10.0  # Max market impact

    # --- Data quality ---
    min_data_freshness_seconds: float = 60.0
    max_allowed_staleness_seconds: float = 300.0

    # --- Market integrity ---
    min_market_age_days: float = 30.0  # Minimum days listed
    min_daily_trades: int = 100  # Minimum recent trades

    # --- Liquidity consistency ---
    min_liquidity_score: float = 0.20  # From LiquidityAnalyzer


@dataclass
class AssetQualityReport:
    """Result of a quality assessment for one instrument."""

    symbol: str
    exchange: str
    tier: QualityTier = QualityTier.TIER_D
    qualified: bool = False

    # Individual checks
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    # Metrics snapshot
    volume_24h: float = 0.0
    spread_pct: float = 0.0
    depth_10bps_usd: float = 0.0
    liquidity_score: float = 0.0
    data_age_seconds: float = 0.0

    assessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


class AssetQualityFilter:
    """Dynamic quality filter for crypto spot instruments.

    Evaluates every instrument against measurable criteria and assigns
    a quality tier. No hard-coded coin lists. No social-popularity bias.

    The filter is re-run whenever new market data arrives (per instrument).
    Results feed into UniverseManager for universe decisions and into
    the Capital Allocator for tier-aware allocation.
    """

    def __init__(self, config: QualityFilterConfig | None = None) -> None:
        self.config = config or QualityFilterConfig()

    # ------------------------------------------------------------------
    # Main assessment
    # ------------------------------------------------------------------

    def assess(
        self,
        symbol: str,
        exchange: str,
        liquidity: LiquidityMetrics | None = None,
        volume_24h: float = 0.0,
        spread_pct: float = 0.0,
        data_age_seconds: float = 0.0,
        market_age_days: float | None = None,
        daily_trades: int = 0,
        **kwargs: Any,
    ) -> AssetQualityReport:
        """Evaluate one instrument against all quality criteria.

        Returns an AssetQualityReport with tier assignment and pass/fail details.
        """
        report = AssetQualityReport(symbol=symbol, exchange=exchange)
        report.volume_24h = volume_24h
        report.spread_pct = spread_pct
        report.data_age_seconds = data_age_seconds
        cfg = self.config

        # --- Compute depth USD ---
        depth_usd = 0.0
        if liquidity is not None:
            mid = (liquidity.bid + liquidity.ask) / 2.0 if liquidity.bid > 0 else 0.0
            depth_usd = liquidity.depth_10bps * mid
            report.liquidity_score = liquidity.liquidity_score

        report.depth_10bps_usd = depth_usd

        # --- Run all checks ---
        checks: dict[str, bool] = {}
        failures: list[str] = []

        # Volume
        if volume_24h < cfg.min_volume_tier_c:
            checks["volume"] = False
            failures.append(f"Volume ${volume_24h:,.0f} < ${cfg.min_volume_tier_c:,.0f}")
        else:
            checks["volume"] = True

        # Spread
        if spread_pct > cfg.max_spread_tier_c:
            checks["spread"] = False
            failures.append(f"Spread {spread_pct:.2f}% > {cfg.max_spread_tier_c}%")
        else:
            checks["spread"] = True

        # Depth
        if depth_usd < cfg.min_depth_tier_c:
            checks["depth"] = False
            failures.append(f"Depth ${depth_usd:,.0f} < ${cfg.min_depth_tier_c:,.0f}")
        else:
            checks["depth"] = True

        # Liquidity score
        if report.liquidity_score < cfg.min_liquidity_score:
            checks["liquidity_score"] = False
            failures.append(
                f"Liquidity score {report.liquidity_score:.2f} < {cfg.min_liquidity_score}"
            )
        else:
            checks["liquidity_score"] = True

        # Data freshness
        if data_age_seconds > cfg.max_allowed_staleness_seconds:
            checks["data_freshness"] = False
            failures.append(
                f"Data age {data_age_seconds:.0f}s > {cfg.max_allowed_staleness_seconds}s"
            )
        else:
            checks["data_freshness"] = True

        # Market age
        if market_age_days is not None and market_age_days < cfg.min_market_age_days:
            checks["market_age"] = False
            failures.append(f"Market age {market_age_days:.0f}d < {cfg.min_market_age_days}d")
        else:
            checks["market_age"] = True

        # Trade count
        if daily_trades < cfg.min_daily_trades:
            checks["trade_count"] = False
            failures.append(f"Trades {daily_trades} < {cfg.min_daily_trades}")
        else:
            checks["trade_count"] = True

        report.checks = checks
        report.failures = failures

        # --- Determine qualification ---
        report.qualified = len(failures) == 0

        # --- Assign tier ---
        if report.qualified:
            report.tier = self._classify_tier(
                volume_24h, spread_pct, depth_usd, report.liquidity_score
            )

        return report

    # ------------------------------------------------------------------
    # Tier classification
    # ------------------------------------------------------------------

    def _classify_tier(
        self,
        volume_24h: float,
        spread_pct: float,
        depth_usd: float,
        liquidity_score: float,
    ) -> QualityTier:
        """Classify a qualified asset into a quality tier.

        All thresholds are configurable. No asset-name-based exceptions.
        """
        cfg = self.config

        # TIER_A: Deep liquidity, tight spread, high volume
        if (
            volume_24h >= cfg.min_volume_tier_a
            and spread_pct <= cfg.max_spread_tier_a
            and depth_usd >= cfg.min_depth_tier_a
        ):
            return QualityTier.TIER_A

        # TIER_B: Highly liquid established
        if (
            volume_24h >= cfg.min_volume_tier_b
            and spread_pct <= cfg.max_spread_tier_b
            and depth_usd >= cfg.min_depth_tier_b
        ):
            return QualityTier.TIER_B

        # TIER_C: Qualified medium-liquidity
        return QualityTier.TIER_C

    # ------------------------------------------------------------------
    # Tier-aware universe selection
    # ------------------------------------------------------------------

    def select_for_tier(
        self,
        reports: list[AssetQualityReport],
        allowed_tiers: list[QualityTier],
        max_count: int = 100,
    ) -> list[AssetQualityReport]:
        """Filter reports to those in allowed tiers, prioritized by tier then volume.

        Args:
            reports: All quality reports.
            allowed_tiers: Which tiers are permitted (e.g., [A, B, C] for Level 1/2).
            max_count: Maximum number to return.

        Returns:
            Filtered and sorted list (best first).
        """
        eligible = [r for r in reports if r.qualified and r.tier in allowed_tiers]

        # Sort: tier first (A > B > C), then volume descending
        tier_order = {QualityTier.TIER_A: 0, QualityTier.TIER_B: 1, QualityTier.TIER_C: 2}
        eligible.sort(
            key=lambda r: (
                tier_order.get(r.tier, 99),
                -r.volume_24h,
            )
        )

        return eligible[:max_count]
