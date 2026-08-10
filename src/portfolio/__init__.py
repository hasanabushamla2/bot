"""Portfolio / Capital Allocation Subsystem.

This subsystem ensures the trading engine intelligently distributes
capital as account equity grows, rather than blindly increasing
individual position sizes.

Modules:
- liquidity.py:       Liquidity analysis, order-book depth, market impact
- capacity.py:        Max-efficient-position-size computation
- correlation.py:     Correlation matrix and diversification scoring
- universe.py:        Dynamic tradable-universe management
- allocator.py:       Capital allocation across eligible opportunities
- capital_tiers.py:   Capital-tier detection, slot management, daily reports
- sweep.py:           Auto-sweep recommendations (simulation only)
- markets/:           Asset quality filter and tier classification
"""

from src.portfolio.allocator import (
    AllocationDecision,
    AllocatorConfig,
    CapitalAllocator,
    PortfolioState,
)
from src.portfolio.capacity import CapacityEstimator, PositionCapacity
from src.portfolio.capital_tiers import (
    CapitalDailyReport,
    CapitalLevel,
    CapitalTierConfig,
    CapitalTierManager,
    TierState,
    generate_daily_report,
)
from src.portfolio.correlation import CorrelationMatrix, CorrelationTracker
from src.portfolio.liquidity import LiquidityAnalyzer, LiquidityMetrics
from src.portfolio.markets import (
    AssetQualityFilter,
    AssetQualityReport,
    QualityFilterConfig,
    QualityTier,
)
from src.portfolio.sweep import (
    DestinationType,
    SweepEngine,
    SweepPolicyConfig,
    SweepRecommendation,
    SweepStatus,
)
from src.portfolio.universe import UniverseAsset, UniverseManager

__all__ = [
    "AllocationDecision",
    "AllocatorConfig",
    "AssetQualityFilter",
    "AssetQualityReport",
    "CapacityEstimator",
    "CapitalAllocator",
    "CapitalDailyReport",
    "CapitalLevel",
    "CapitalTierConfig",
    "CapitalTierManager",
    "CorrelationMatrix",
    "CorrelationTracker",
    "DestinationType",
    "LiquidityAnalyzer",
    "LiquidityMetrics",
    "PortfolioState",
    "PositionCapacity",
    "QualityFilterConfig",
    "QualityTier",
    "SweepEngine",
    "SweepPolicyConfig",
    "SweepRecommendation",
    "SweepStatus",
    "TierState",
    "UniverseAsset",
    "UniverseManager",
    "generate_daily_report",
]
