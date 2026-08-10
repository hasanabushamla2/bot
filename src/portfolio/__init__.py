"""Portfolio / Capital Allocation Subsystem.

This subsystem ensures the trading engine intelligently distributes
capital as account equity grows, rather than blindly increasing
individual position sizes.

Modules:
- liquidity.py:   Liquidity analysis, order-book depth, market impact estimation
- capacity.py:    Max-efficient-position-size computation
- correlation.py: Correlation matrix and diversification scoring
- universe.py:    Dynamic tradable-universe management
- allocator.py:   Capital allocation across eligible opportunities
"""

from src.portfolio.allocator import (
    AllocationDecision,
    AllocatorConfig,
    CapitalAllocator,
    PortfolioState,
)
from src.portfolio.capacity import CapacityEstimator, PositionCapacity
from src.portfolio.correlation import CorrelationMatrix, CorrelationTracker
from src.portfolio.liquidity import LiquidityAnalyzer, LiquidityMetrics
from src.portfolio.universe import UniverseAsset, UniverseManager

__all__ = [
    "AllocationDecision",
    "AllocatorConfig",
    "CapacityEstimator",
    "CapitalAllocator",
    "CorrelationMatrix",
    "CorrelationTracker",
    "LiquidityAnalyzer",
    "LiquidityMetrics",
    "PortfolioState",
    "PositionCapacity",
    "UniverseAsset",
    "UniverseManager",
]
