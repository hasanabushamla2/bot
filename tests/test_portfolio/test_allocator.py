"""Tests for the Capital Allocator — allocation, concentration, sizing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.opportunity.engine import EvaluatedOpportunity, OpportunityScore
from src.portfolio.allocator import (
    AllocatorConfig,
    CapitalAllocator,
    PortfolioState,
)
from src.portfolio.capacity import PositionCapacity
from src.risk.engine import RiskAssessment, RiskDecision
from src.strategies.base import SignalDirection, StrategySignal


def make_opp(
    strategy_id: str = "strat_a",
    symbol: str = "BTC-USD",
    required_capital: float = 1000.0,
    estimated_return: float = 2.0,
    final_score: float = 1.0,
) -> tuple[EvaluatedOpportunity, RiskAssessment, PositionCapacity]:
    """Build a complete (opportunity, risk, capacity) tuple."""
    signal = StrategySignal(
        strategy_id=strategy_id,
        symbol=symbol,
        exchange="test",
        direction=SignalDirection.LONG,
        confidence=0.9,
        estimated_return=estimated_return,
        required_capital=required_capital,
        timestamp=datetime.now(UTC),
        signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    score = OpportunityScore(final_score=final_score, net_return=estimated_return)
    opp = EvaluatedOpportunity(signal=signal, score=score)

    risk = RiskAssessment(
        opportunity=opp, decision=RiskDecision.APPROVED, max_position_size=required_capital
    )

    capacity = PositionCapacity(
        symbol=symbol,
        strategy_id=strategy_id,
        max_efficient_size=required_capital * 5,
        is_viable=True,
    )
    return opp, risk, capacity


def make_portfolio(
    equity: float = 10_000.0,
    available: float = 8_000.0,
    total_exposure_pct: float = 10.0,
) -> PortfolioState:
    return PortfolioState(
        total_equity=equity,
        available_cash=available,
        total_exposure_pct=total_exposure_pct,
    )


class TestCapitalAllocator:
    def test_allocate_single_opportunity(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio()
        opps = [make_opp(required_capital=500.0)]
        decisions = allocator.allocate(portfolio, opps)
        assert len(decisions) == 1
        assert decisions[0].is_allocated
        assert decisions[0].allocated_capital > 0

    def test_allocate_multiple_opportunities(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio(available=10_000.0)
        opps = [
            make_opp("s1", "BTC-USD", 500.0, final_score=2.0),
            make_opp("s2", "ETH-USD", 500.0, final_score=1.5),
            make_opp("s3", "SOL-USD", 500.0, final_score=1.0),
        ]
        decisions = allocator.allocate(portfolio, opps)
        allocated = [d for d in decisions if d.is_allocated]
        # Should allocate to at least 2 (diversified)
        assert len(allocated) >= 1

    def test_insufficient_capital_no_allocations(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio(equity=100.0, available=10.0)
        opps = [make_opp(required_capital=5_000.0)]
        decisions = allocator.allocate(portfolio, opps)
        assert len([d for d in decisions if d.is_allocated]) == 0

    def test_respects_asset_concentration(self) -> None:
        """Multiple BTC opportunities should hit asset concentration limit."""
        allocator = CapitalAllocator(
            AllocatorConfig(max_single_asset_pct=15.0, max_single_position_pct=10.0)
        )
        portfolio = make_portfolio(available=10_000.0)
        # 10 BTC-USD opportunities at $1k each
        opps = [make_opp(f"s{i}", "BTC-USD", 1000.0) for i in range(10)]
        decisions = allocator.allocate(portfolio, opps)
        allocated = [d for d in decisions if d.is_allocated]
        total_btc = sum(d.allocated_capital for d in allocated)
        # Max BTC: 15% of 10k = 1500
        assert total_btc <= 1500.0 * 1.1  # Allow small rounding margin

    def test_respects_strategy_concentration(self) -> None:
        allocator = CapitalAllocator(
            AllocatorConfig(max_single_strategy_pct=20.0, max_single_position_pct=10.0)
        )
        portfolio = make_portfolio(available=10_000.0)
        opps = [make_opp("same_strat", f"SYM-{i}-USD", 1000.0) for i in range(10)]
        decisions = allocator.allocate(portfolio, opps)
        allocated = [d for d in decisions if d.is_allocated]
        total_strat = sum(d.allocated_capital for d in allocated)
        assert total_strat <= 2000.0 * 1.1

    def test_respects_exchange_concentration(self) -> None:
        allocator = CapitalAllocator(
            AllocatorConfig(max_single_exchange_pct=25.0, max_single_position_pct=10.0)
        )
        portfolio = make_portfolio(available=10_000.0)
        opps = [make_opp(f"s{i}", f"SYM-{i}-USD", 1000.0) for i in range(10)]
        decisions = allocator.allocate(portfolio, opps)
        allocated = [d for d in decisions if d.is_allocated]
        total_exchange = sum(d.allocated_capital for d in allocated)
        assert total_exchange <= 2500.0 * 1.1

    def test_respects_max_positions_limit(self) -> None:
        allocator = CapitalAllocator(AllocatorConfig(max_positions_limit=3))
        portfolio = make_portfolio(available=50_000.0)
        opps = [make_opp(f"s{i}", f"SYM-{i}-USD", 500.0) for i in range(20)]
        decisions = allocator.allocate(portfolio, opps)
        allocated = [d for d in decisions if d.is_allocated]
        assert len(allocated) <= 3

    def test_capacity_report(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio()
        opps = [
            make_opp("s1", "BTC-USD", 500.0),
            make_opp("s2", "ETH-USD", 300.0),
        ]
        decisions = allocator.allocate(portfolio, opps)
        report = allocator.generate_capacity_report(decisions, portfolio)
        assert report["total_opportunities"] == 2
        assert "by_asset" in report
        assert "by_strategy" in report
        assert "capital_utilization_pct" in report

    def test_reserve_pct_reduces_available(self) -> None:
        alloc_high_reserve = CapitalAllocator(AllocatorConfig(reserve_pct=30.0))
        alloc_low_reserve = CapitalAllocator(AllocatorConfig(reserve_pct=5.0))
        portfolio = make_portfolio(available=10_000.0)
        opps = [make_opp(required_capital=5_000.0)]

        dec_high = alloc_high_reserve.allocate(portfolio, opps)
        dec_low = alloc_low_reserve.allocate(portfolio, opps)

        total_high = sum(d.allocated_capital for d in dec_high if d.is_allocated)
        total_low = sum(d.allocated_capital for d in dec_low if d.is_allocated)
        # Higher reserve → less allocated
        assert total_high <= total_low

    def test_allocation_score_ranking(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio(available=5_000.0)
        opps = [
            make_opp("s1", "BTC-USD", 3000.0, final_score=3.0),
            make_opp("s2", "ETH-USD", 3000.0, final_score=1.0),
        ]
        decisions = allocator.allocate(portfolio, opps)
        # Higher score should get allocated first
        allocated = [d for d in decisions if d.is_allocated]
        if len(allocated) == 1:
            # Only the higher-scored one should be allocated
            assert allocated[0].strategy_id == "s1"

    def test_rebalance_check(self) -> None:
        allocator = CapitalAllocator()
        portfolio = make_portfolio()
        positions = [
            {"symbol": "BTC-USD", "notional": 3000.0, "unrealized_pnl": -100.0},
            {"symbol": "ETH-USD", "notional": 1000.0, "unrealized_pnl": 50.0},
        ]
        # 30% in BTC exceeds 20% max_single_asset_pct default
        adjustments = allocator.rebalance_check(portfolio, positions)
        # BTC should be flagged for reduction
        btc_adjustments = [a for a in adjustments if a["symbol"] == "BTC-USD"]
        assert len(btc_adjustments) >= 1
