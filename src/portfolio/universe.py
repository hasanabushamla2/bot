"""Dynamic Tradable Universe Manager.

Continuously classifies instruments by liquidity, spread, depth, volume,
volatility, execution quality, and data health.

Automatically adds/removes assets from the active trading universe
according to configurable eligibility requirements.

Illiquid or unhealthy markets are rejected before any strategy
even sees them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.core.logging_config import get_logger
from src.portfolio.liquidity import LiquidityAnalyzer, LiquidityMetrics

logger = get_logger(__name__)


class UniverseStatus(str, Enum):
    ACTIVE = "active"  # Trading allowed
    WATCH = "watch"  # Being evaluated (new/returning)
    SUSPENDED = "suspended"  # Temporarily disabled
    REJECTED = "rejected"  # Does not meet requirements
    DEGRADED = "degraded"  # Trading allowed but with restrictions


@dataclass
class UniverseAsset:
    """Tracked instrument in the tradable universe."""

    symbol: str
    exchange: str
    base_asset: str = ""
    quote_asset: str = ""

    status: UniverseStatus = UniverseStatus.WATCH
    status_reason: str = ""

    # Latest metrics
    liquidity: LiquidityMetrics | None = None
    liquidity_score: float = 0.0
    spread_pct: float = 0.0
    volume_24h: float = 0.0
    volatility_pct: float = 0.0  # Annualized

    # Data health
    data_healthy: bool = True
    last_data_at: datetime | None = None
    stale_seconds: float = 0.0

    # Execution
    execution_quality: float = 0.0  # 0-1

    # Metadata
    added_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status_changed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UniverseConfig:
    """Eligibility thresholds for universe membership."""

    min_liquidity_score: float = 0.3
    max_spread_pct: float = 5.0  # Maximum acceptable spread
    min_volume_24h: float = 1_000_000.0  # Minimum daily volume in quote
    max_volatility_pct: float = 200.0  # Max annualized volatility
    max_stale_seconds: float = 300.0  # 5 minutes
    min_data_freshness: float = 60.0  # Data must be < 60s old

    # Watch period for new assets (seconds)
    watch_period_seconds: float = 3600.0  # 1 hour


class UniverseManager:
    """Manages the dynamic tradable universe.

    Responsibilities:
    1. Track all known instruments across exchanges.
    2. Continuously evaluate eligibility.
    3. Promote/demote instruments based on current conditions.
    4. Provide the active universe to the scanner and strategies.
    5. Reject illiquid/unhealthy markets before strategy evaluation.
    """

    def __init__(self, config: UniverseConfig | None = None) -> None:
        self.config = config or UniverseConfig()
        self._assets: dict[str, UniverseAsset] = {}  # key = "exchange:symbol"
        self._liquidity = LiquidityAnalyzer()

    # --- Asset Management ---

    def register(self, symbol: str, exchange: str, **kwargs: Any) -> UniverseAsset:
        """Register or retrieve an instrument in the universe."""
        key = f"{exchange}:{symbol}"
        if key in self._assets:
            return self._assets[key]

        asset = UniverseAsset(
            symbol=symbol,
            exchange=exchange,
            status=UniverseStatus.WATCH,
            status_reason="Newly registered — under watch period",
            **kwargs,
        )
        self._assets[key] = asset
        logger.info("universe_asset_registered", key=key, symbol=symbol, exchange=exchange)
        return asset

    def get(self, symbol: str, exchange: str) -> UniverseAsset | None:
        """Get an asset by exchange:symbol."""
        return self._assets.get(f"{exchange}:{symbol}")

    def get_active(self) -> list[UniverseAsset]:
        """Return all ACTIVE assets."""
        return [a for a in self._assets.values() if a.status == UniverseStatus.ACTIVE]

    def get_active_symbols(self) -> list[str]:
        """Return symbols for all ACTIVE assets."""
        return [a.symbol for a in self.get_active()]

    def get_tradable(self) -> list[UniverseAsset]:
        """Return all tradable assets (ACTIVE or DEGRADED)."""
        return [
            a
            for a in self._assets.values()
            if a.status in (UniverseStatus.ACTIVE, UniverseStatus.DEGRADED)
        ]

    def get_all(self) -> list[UniverseAsset]:
        """Return all tracked assets."""
        return list(self._assets.values())

    # --- Evaluation ---

    def evaluate_all(self, now: datetime | None = None) -> dict[str, int]:
        """Re-evaluate all tracked instruments.

        Returns counts of status changes.
        """
        now = now or datetime.now(UTC)
        counts: dict[str, int] = {
            "promoted": 0,
            "demoted": 0,
            "suspended": 0,
            "rejected": 0,
        }

        for asset in self._assets.values():
            old_status = asset.status
            new_status = self._evaluate_one(asset, now)
            if new_status != old_status:
                asset.status = new_status
                asset.status_changed_at = now
                counts[self._status_change_key(old_status, new_status)] += 1

                logger.info(
                    "universe_status_change",
                    symbol=asset.symbol,
                    exchange=asset.exchange,
                    old=old_status.value,
                    new=new_status.value,
                    reason=asset.status_reason,
                )

        return counts

    def _evaluate_one(self, asset: UniverseAsset, now: datetime) -> UniverseStatus:
        """Evaluate eligibility for a single asset."""
        cfg = self.config

        # --- Data health ---
        if not asset.data_healthy:
            asset.status_reason = "Data feed unhealthy"
            return UniverseStatus.SUSPENDED

        if asset.last_data_at is None:
            asset.stale_seconds = 999999.0
        else:
            asset.stale_seconds = (now - asset.last_data_at).total_seconds()

        if asset.stale_seconds > cfg.max_stale_seconds:
            asset.status_reason = (
                f"Data stale ({asset.stale_seconds:.0f}s > {cfg.max_stale_seconds}s)"
            )
            return UniverseStatus.SUSPENDED

        # --- Watch period ---
        time_in_universe = (now - asset.added_at).total_seconds()
        if time_in_universe < cfg.watch_period_seconds:
            asset.status_reason = (
                f"Under watch ({time_in_universe:.0f}s / {cfg.watch_period_seconds}s)"
            )
            return UniverseStatus.WATCH

        # --- Spread ---
        if asset.spread_pct > cfg.max_spread_pct:
            asset.status_reason = (
                f"Spread too wide ({asset.spread_pct:.2f}% > {cfg.max_spread_pct}%)"
            )
            return UniverseStatus.SUSPENDED

        # --- Volume ---
        if asset.volume_24h < cfg.min_volume_24h:
            asset.status_reason = (
                f"Volume too low (${asset.volume_24h:,.0f} < ${cfg.min_volume_24h:,.0f})"
            )
            return UniverseStatus.SUSPENDED

        # --- Liquidity ---
        if asset.liquidity_score < cfg.min_liquidity_score:
            asset.status_reason = (
                f"Liquidity too low ({asset.liquidity_score:.2f} < {cfg.min_liquidity_score})"
            )
            return UniverseStatus.SUSPENDED

        # --- Volatility ---
        if asset.volatility_pct > cfg.max_volatility_pct:
            asset.status_reason = (
                f"Volatility too high ({asset.volatility_pct:.1f}% > {cfg.max_volatility_pct}%)"
            )
            return UniverseStatus.DEGRADED

        # --- Passed all checks ---
        asset.status_reason = "All eligibility checks passed"
        return UniverseStatus.ACTIVE

    def update_liquidity(self, symbol: str, exchange: str, metrics: LiquidityMetrics) -> None:
        """Update liquidity metrics for an asset."""
        key = f"{exchange}:{symbol}"
        asset = self._assets.get(key)
        if asset is None:
            asset = self.register(symbol, exchange)
        asset.liquidity = metrics
        asset.liquidity_score = metrics.liquidity_score
        asset.spread_pct = metrics.spread_pct
        asset.volume_24h = metrics.volume_24h
        asset.last_data_at = metrics.timestamp
        asset.stale_seconds = 0.0

    def suspend(self, symbol: str, exchange: str, reason: str = "manual") -> None:
        """Manually suspend an asset."""
        key = f"{exchange}:{symbol}"
        asset = self._assets.get(key)
        if asset:
            asset.status = UniverseStatus.SUSPENDED
            asset.status_reason = reason
            asset.status_changed_at = datetime.now(UTC)

    def unsuspend(self, symbol: str, exchange: str) -> None:
        """Return a suspended asset to WATCH for re-evaluation."""
        key = f"{exchange}:{symbol}"
        asset = self._assets.get(key)
        if asset:
            asset.status = UniverseStatus.WATCH
            asset.status_reason = "Manually unsuspended — re-evaluating"
            asset.added_at = datetime.now(UTC)  # Reset watch period
            asset.status_changed_at = datetime.now(UTC)

    # --- Status Helpers ---

    @staticmethod
    def _status_change_key(old: UniverseStatus, new: UniverseStatus) -> str:
        """Map status transition to a counter key."""
        if new == UniverseStatus.ACTIVE:
            return "promoted"
        if new == UniverseStatus.DEGRADED:
            return "demoted"
        if new == UniverseStatus.SUSPENDED:
            return "suspended"
        if new == UniverseStatus.REJECTED:
            return "rejected"
        return "demoted"

    # --- Universe Summary ---

    def summary(self) -> dict[str, Any]:
        """Generate a summary of the current universe state."""
        assets = list(self._assets.values())
        if not assets:
            return {"total": 0, "active": 0, "watch": 0, "suspended": 0, "degraded": 0}

        status_counts: dict[str, int] = {}
        for a in assets:
            status_counts[a.status.value] = status_counts.get(a.status.value, 0) + 1

        return {
            "total": len(assets),
            **status_counts,
            "active_symbols": [a.symbol for a in assets if a.status == UniverseStatus.ACTIVE],
            "avg_liquidity_score": float(sum(a.liquidity_score for a in assets) / len(assets)),
        }
