"""Position Capacity Estimator — liquidity-aware maximum efficient position size.

For every opportunity, estimates:
- Order-book depth available
- Slippage at multiple order sizes
- Expected market impact
- Fill probability at different sizes
- Maximum efficient position size (where net edge remains > threshold)

CRITICAL: These are ESTIMATES from data, never fabricated.
Actual capacity must be validated in paper trading before any live use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.adapters.base import NormalizedOrderBook
from src.core.logging_config import get_logger
from src.portfolio.liquidity import LiquidityAnalyzer
from src.strategies.base import StrategySignal

logger = get_logger(__name__)


@dataclass
class PositionCapacity:
    """Liquidity-aware capacity assessment for one opportunity."""

    symbol: str
    strategy_id: str
    assessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Input
    signal_required_capital: float = 0.0

    # Capacity estimates at different sizes
    capacity_1k: float = 0.0  # Net edge (bps) at $1k
    capacity_5k: float = 0.0
    capacity_10k: float = 0.0
    capacity_25k: float = 0.0
    capacity_50k: float = 0.0
    capacity_100k: float = 0.0

    # Maximum efficient position
    max_efficient_size: float = 0.0
    reason_capped: str = ""  # Why it's capped: "liquidity", "spread", "volume", "none"

    # Fill estimates
    estimated_fill_pct: float = 100.0  # Expected fill percentage
    expected_execution_time_seconds: float = 5.0

    # Degradation curve
    size_vs_edge: list[tuple[float, float]] = field(default_factory=list)
    # [(size, net_edge_bps), ...]

    # Derived
    is_viable: bool = False
    viability_reason: str = ""

    metadata: dict[str, Any] = field(default_factory=dict)


class CapacityEstimator:
    """Estimates maximum efficient position size for each opportunity.

    The estimator:
    1. Takes an opportunity and current market liquidity data.
    2. Estimates net edge at increasing position sizes.
    3. Finds the point where expected net edge falls below the threshold.
    4. Returns the maximum efficient position size.

    Key principle: as position size grows, slippage and market impact
    increase, reducing net edge. The capacity estimator finds the
    optimal trade-off.
    """

    def __init__(
        self,
        min_net_edge_bps: float = 1.0,
        max_participation_pct: float = 1.0,
    ) -> None:
        self.min_net_edge_bps = min_net_edge_bps
        self.max_participation_pct = max_participation_pct
        self._liquidity = LiquidityAnalyzer()

    def estimate(
        self,
        signal: StrategySignal,
        order_book: NormalizedOrderBook | None = None,
        volume_24h: float = 0.0,
    ) -> PositionCapacity:
        """Estimate capacity for a signal given current market conditions.

        Args:
            signal: Strategy signal with estimated return and required capital.
            order_book: Current order book for liquidity analysis.
            volume_24h: 24h volume in quote currency.

        Returns:
            PositionCapacity with size-dependent edge estimates.
        """
        cap = PositionCapacity(
            symbol=signal.symbol or "unknown",
            strategy_id=signal.strategy_id,
            signal_required_capital=signal.required_capital or 0.0,
        )

        # --- Analyze liquidity ---
        liq = self._liquidity.analyze(order_book, volume_24h)

        # --- Base edge from signal (gross return - fees - spread) ---
        gross_return_pct = signal.estimated_return or 0.0
        base_edge_bps = gross_return_pct * 100.0  # Convert % to bps

        # Estimate fees (rough: 0.2% round-trip = 20 bps)
        taker_fee = float(signal.metadata.get("taker_fee", 0.001))
        fee_bps = taker_fee * 2.0 * 100.0

        # --- Compute edge at different sizes ---
        test_sizes = [1_000, 5_000, 10_000, 25_000, 50_000, 100_000]
        avg_daily_vol = max(volume_24h, 1.0)

        cap.size_vs_edge = []
        _size_fields: dict[int, str] = {
            1: "capacity_1k",
            5: "capacity_5k",
            10: "capacity_10k",
            25: "capacity_25k",
            50: "capacity_50k",
            100: "capacity_100k",
        }
        for size in test_sizes:
            impact = self._liquidity._estimate_impact(size, liq.spread_pct, avg_daily_vol)
            net_edge = base_edge_bps - fee_bps - impact
            cap.size_vs_edge.append((float(size), round(net_edge, 2)))

            # Store in named fields
            key = size // 1000
            field_name = _size_fields.get(key)
            if field_name is not None:
                object.__setattr__(cap, field_name, round(net_edge, 2))

        # --- Find max efficient size ---
        max_size: float = 0.0
        reason = "liquidity"
        for test_size, test_edge in reversed(cap.size_vs_edge):
            if test_edge >= self.min_net_edge_bps:
                max_size = test_size
                reason = "none" if test_size >= float(test_sizes[-1]) else "liquidity"
                break

        # Additional constraints: participation limit
        participation_limit = avg_daily_vol * self.max_participation_pct / 100.0
        if max_size > participation_limit:
            max_size = participation_limit
            reason = "volume"

        cap.max_efficient_size = round(max_size, 2)
        cap.reason_capped = reason

        # --- Fill estimate ---
        if signal.required_capital and signal.required_capital > 0:
            if signal.required_capital <= max_size:
                cap.estimated_fill_pct = min(100.0, max(50.0, 100.0 - liq.spread_pct * 2.0))
            else:
                # Linear degradation beyond max size
                ratio = max_size / signal.required_capital
                cap.estimated_fill_pct = max(0.0, ratio * 100.0)
        else:
            cap.estimated_fill_pct = 100.0

        # --- Execution time estimate ---
        # Simple heuristic: larger relative to ADV → more time
        if signal.required_capital and avg_daily_vol > 0:
            participation = signal.required_capital / avg_daily_vol * 100
            cap.expected_execution_time_seconds = min(300.0, max(1.0, participation * 60.0))
        else:
            cap.expected_execution_time_seconds = 5.0

        # --- Viability ---
        signal_size = signal.required_capital or 0.0
        cap.is_viable = max_size > 0 and signal_size <= max_size
        if not cap.is_viable and max_size > 0:
            cap.viability_reason = (
                f"Required capital ${signal_size:,.0f} exceeds max efficient "
                f"size ${max_size:,.0f}. Consider resizing to ${max_size:,.0f}."
            )
        elif max_size <= 0:
            cap.viability_reason = "Market too illiquid for any efficient position."
            cap.is_viable = False

        return cap

    def capacity_report(self, capacities: list[PositionCapacity]) -> dict[str, Any]:
        """Generate a summary capacity report across multiple opportunities.

        This is the STRATEGY_CAPACITY report — shows how strategies
        perform at different capital levels.
        """
        if not capacities:
            return {"total_opportunities": 0}

        by_strategy: dict[str, list[float]] = {}
        for cap in capacities:
            by_strategy.setdefault(cap.strategy_id, []).append(cap.max_efficient_size)

        strategy_reports: dict[str, dict[str, Any]] = {}
        for sid, sizes in by_strategy.items():
            strategy_reports[sid] = {
                "min_efficient": min(sizes) if sizes else 0,
                "max_efficient": max(sizes) if sizes else 0,
                "mean_efficient": float(sum(sizes) / len(sizes)) if sizes else 0,
                "num_opportunities": len(sizes),
                "viable_count": len([s for s in sizes if s > 0]),
            }

        return {
            "total_opportunities": len(capacities),
            "viable_opportunities": len([c for c in capacities if c.is_viable]),
            "total_efficient_capacity": sum(c.max_efficient_size for c in capacities),
            "by_strategy": strategy_reports,
        }
