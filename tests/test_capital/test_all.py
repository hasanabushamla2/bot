"""Tests for capital tiers, auto-sweep, asset quality filter, tier classification."""

from __future__ import annotations

import pytest

from src.portfolio.capital_tiers import (
    CapitalLevel,
    CapitalTierConfig,
    CapitalTierManager,
    generate_daily_report,
)
from src.portfolio.liquidity import LiquidityMetrics
from src.portfolio.markets import (
    AssetQualityFilter,
    AssetQualityReport,
    QualityTier,
)
from src.portfolio.sweep import (
    DestinationType,
    SweepEngine,
    SweepPolicyConfig,
    SweepStatus,
)

# ===========================================================================
# Capital Tiers
# ===========================================================================


class TestCapitalTiers:
    """Verify all four capital levels with exact boundary values."""

    def test_level_1_exactly_5000(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(5000.00)
        assert state.level == CapitalLevel.LEVEL_1
        assert state.target_slots == 2
        assert state.sweep_eligible == 0.0

    def test_level_1_small(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(1000.0)
        assert state.level == CapitalLevel.LEVEL_1
        assert state.target_slots == 2

    def test_level_2_start(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(5000.01)
        assert state.level == CapitalLevel.LEVEL_2
        assert state.target_slots == 5

    def test_level_2_mid(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(50000.0)
        assert state.level == CapitalLevel.LEVEL_2
        assert state.target_slots == 5

    def test_level_2_boundary_exactly(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(100000.0)
        assert state.level == CapitalLevel.LEVEL_2

    def test_level_3_start(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(100000.01)
        assert state.level == CapitalLevel.LEVEL_3
        assert state.target_slots == 10

    def test_level_3_large(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(1_000_000.0)
        assert state.level == CapitalLevel.LEVEL_3

    def test_level_3_boundary(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(5_000_000.0)
        assert state.level == CapitalLevel.LEVEL_3
        assert state.sweep_eligible == 0.0

    def test_level_4_start(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(5_000_000.01)
        assert state.level == CapitalLevel.LEVEL_4
        assert state.target_slots == 20
        assert state.sweep_eligible > 0

    def test_level_4_large(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(10_000_000.0)
        assert state.level == CapitalLevel.LEVEL_4

    def test_active_capital_capped_at_level_4(self) -> None:
        mgr = CapitalTierManager(CapitalTierConfig(active_capital_cap=5_000_000.0))
        state = mgr.determine_tier(7_500_000.0)
        assert state.active_capital == 5_000_000.0
        assert state.sweep_eligible == 2_500_000.0

    def test_provisional_slot_size(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(50_000.0)
        # Level 2: 5 slots → provisional = 50000/5 = 10000
        assert state.provisional_slot_size == pytest.approx(10_000.0)

    def test_level_3_restricts_to_tier_ab(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(500_000.0)
        assert QualityTier.TIER_C not in state.allowed_tiers

    def test_level_1_allows_tier_c(self) -> None:
        mgr = CapitalTierManager()
        state = mgr.determine_tier(3000.0)
        assert QualityTier.TIER_C in state.allowed_tiers

    def test_custom_config(self) -> None:
        cfg = CapitalTierConfig(
            level_1_cap=10_000.0,
            slots_level_1=3,
            level_2_cap=200_000.0,
            slots_level_2=8,
        )
        mgr = CapitalTierManager(cfg)
        s1 = mgr.determine_tier(8_000.0)
        assert s1.level == CapitalLevel.LEVEL_1
        assert s1.target_slots == 3

        s2 = mgr.determine_tier(150_000.0)
        assert s2.level == CapitalLevel.LEVEL_2
        assert s2.target_slots == 8


class TestSlotAllocation:
    """Verify slot distribution across tiers."""

    def _make_report(self, tier: QualityTier, symbol: str, volume: float) -> AssetQualityReport:
        return AssetQualityReport(
            symbol=symbol,
            exchange="test",
            tier=tier,
            qualified=True,
            volume_24h=volume,
            spread_pct=0.1,
            depth_10bps_usd=100_000,
            liquidity_score=0.8,
        )

    def test_distribution_across_abc(self) -> None:
        mgr = CapitalTierManager()
        mgr.determine_tier(50_000.0)  # Level 2, 5 slots

        assets = (
            [self._make_report(QualityTier.TIER_A, "A-USD", 500_000_000)]
            + [self._make_report(QualityTier.TIER_B, f"B-{i}-USD", 50_000_000) for i in range(10)]
            + [self._make_report(QualityTier.TIER_C, f"C-{i}-USD", 5_000_000) for i in range(20)]
        )

        result = mgr.compute_slot_allocation(assets)
        assert result["target_slots"] == 5
        assert result["slots_a"] >= 1  # At least one A slot
        assert result["slots_b"] >= 1
        total_slots = result["slots_a"] + result["slots_b"] + result["slots_c"]
        assert total_slots <= 5

    def test_tier_c_limited(self) -> None:
        """Tier C should be capped at max_tier_c_slot_pct (40% default)."""
        mgr = CapitalTierManager()
        mgr.determine_tier(50_000.0)  # 5 slots, max 2 C slots (40% floor=1, 5*0.4=2)

        assets = [self._make_report(QualityTier.TIER_C, f"C-{i}-USD", 5_000_000) for i in range(50)]
        result = mgr.compute_slot_allocation(assets)
        assert result["slots_c"] <= 2, f"Tier C slots ({result['slots_c']}) exceeded limit (2)"

    def test_daily_report(self) -> None:
        mgr = CapitalTierManager()
        mgr.determine_tier(64_500.0)
        report = generate_daily_report(
            mgr,
            total_balance=64_500.0,
            daily_pnl=320.0,
            daily_fees=12.0,
            daily_slippage=5.0,
            allocation_by_asset={"BTC-USD": 30000, "ETH-USD": 20000, "SOL-USD": 14500},
            allocation_by_strategy={"global_scanner": 64500},
            avg_order_allocation=16_125.0,
        )
        assert report.portfolio_level == "LEVEL_2"
        assert report.total_balance == 64_500.0
        assert report.target_slots == 5
        assert report.daily_realized_pnl == 320.0
        assert report.daily_fees == 12.0
        assert report.daily_slippage == 5.0
        assert report.sweep_eligible == 0.0
        assert "BTC-USD" in report.allocation_by_asset


# ===========================================================================
# Asset Quality Filter
# ===========================================================================


class TestAssetQualityFilter:
    """Verify the dynamic quality filter classifies assets correctly."""

    def test_deep_liquid_is_tier_a(self) -> None:
        qf = AssetQualityFilter()
        liq = _make_liquidity(bid=49999, ask=50001, depth_10bps=500.0)
        report = qf.assess(
            "BTC-USDT",
            "binance",
            liquidity=liq,
            volume_24h=500_000_000,
            spread_pct=0.004,
            data_age_seconds=1,
            market_age_days=3000,
            daily_trades=500_000,
        )
        assert report.qualified
        assert report.tier == QualityTier.TIER_A

    def test_established_altcoin_tier_b(self) -> None:
        qf = AssetQualityFilter()
        # Need depth at 10bps that, multiplied by mid price, exceeds $100K for TIER_B
        # mid ≈ $100 → need depth_10bps ≥ 1000 to get $100K USD
        liq = _make_liquidity(bid=99.9, ask=100.1, depth_10bps=1500.0)
        report = qf.assess(
            "SOL-USDT",
            "binance",
            liquidity=liq,
            volume_24h=80_000_000,
            spread_pct=0.05,
            data_age_seconds=2,
            market_age_days=500,
            daily_trades=100_000,
        )
        assert report.qualified
        assert report.tier in (QualityTier.TIER_A, QualityTier.TIER_B)

    def test_medium_liquidity_tier_c(self) -> None:
        qf = AssetQualityFilter()
        # mid ≈ $2, need depth_10bps * mid ≥ $10K → depth ≥ 5000
        liq = _make_liquidity(bid=1.99, ask=2.01, depth_10bps=6000.0)
        report = qf.assess(
            "MED-USD",
            "binance",
            liquidity=liq,
            volume_24h=5_000_000,
            spread_pct=0.50,
            data_age_seconds=5,
            market_age_days=180,
            daily_trades=5000,
        )
        assert report.qualified
        assert report.tier == QualityTier.TIER_C

    def test_low_volume_rejected(self) -> None:
        qf = AssetQualityFilter()
        report = qf.assess(
            "ILL-USD",
            "binance",
            volume_24h=50_000,
            spread_pct=5.0,
            data_age_seconds=10,
            daily_trades=10,
        )
        assert not report.qualified
        assert report.tier == QualityTier.TIER_D

    def test_wide_spread_rejected(self) -> None:
        qf = AssetQualityFilter()
        report = qf.assess(
            "WIDE-USD",
            "binance",
            volume_24h=10_000_000,
            spread_pct=20.0,
            data_age_seconds=1,
            daily_trades=1000,
        )
        assert not report.qualified
        assert "Spread" in "\n".join(report.failures)

    def test_stale_data_rejected(self) -> None:
        qf = AssetQualityFilter()
        report = qf.assess(
            "STALE-USD",
            "binance",
            volume_24h=10_000_000,
            spread_pct=0.5,
            data_age_seconds=600,
            daily_trades=1000,
        )
        assert not report.qualified
        assert any("data" in f.lower() for f in report.failures)

    def test_too_few_trades_rejected(self) -> None:
        qf = AssetQualityFilter()
        report = qf.assess(
            "DEAD-USD",
            "binance",
            volume_24h=10_000_000,
            spread_pct=0.5,
            data_age_seconds=10,
            daily_trades=5,
        )
        assert not report.qualified

    def test_select_for_tier(self) -> None:
        qf = AssetQualityFilter()
        reports = [
            AssetQualityReport("A-USD", "ex", QualityTier.TIER_A, True, volume_24h=500_000_000),
            AssetQualityReport("B-USD", "ex", QualityTier.TIER_B, True, volume_24h=80_000_000),
            AssetQualityReport("C-USD", "ex", QualityTier.TIER_C, True, volume_24h=5_000_000),
            AssetQualityReport("D-USD", "ex", QualityTier.TIER_D, False, volume_24h=500),
        ]
        selected = qf.select_for_tier(reports, [QualityTier.TIER_A, QualityTier.TIER_B])
        assert len(selected) == 2


# ===========================================================================
# Auto-Sweep
# ===========================================================================


class TestSweepEngine:
    """Verify sweep recommendations — execution is DISABLED."""

    def test_below_cap_no_sweep(self) -> None:
        engine = SweepEngine()
        rec = engine.evaluate(total_balance=500_000, daily_realized_profit=10_000)
        assert rec is None

    def test_above_cap_with_profit(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate(total_balance=5_500_000, daily_realized_profit=300_000)
        assert rec is not None
        # excess = 500k, profit = 300k → sweep 300k
        assert rec.eligible_amount == 300_000.0

    def test_above_cap_no_profit_no_sweep(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate(total_balance=5_500_000, daily_realized_profit=-50_000)
        assert rec is None

    def test_zero_profit_no_sweep(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate(total_balance=6_000_000, daily_realized_profit=0)
        assert rec is None

    def test_large_profit_capped_by_excess(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate(total_balance=5_200_000, daily_realized_profit=500_000)
        assert rec is not None
        # excess = 200k, profit = 500k → sweep 200k only (capped by excess)
        assert rec.eligible_amount == 200_000.0

    def test_approval_required_always_true(self) -> None:
        engine = SweepEngine()
        engine.config.active_capital_cap = 5_000_000
        rec = engine.evaluate(total_balance=6_000_000, daily_realized_profit=500_000)
        assert rec is not None
        assert rec.approval_required is True
        assert rec.status == SweepStatus.PENDING

    def test_reserved_capital_not_swept(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        # $6M balance but $800k in open positions → only $200k excess available
        rec = engine.evaluate(
            total_balance=6_000_000,
            daily_realized_profit=400_000,
            active_positions_value=800_000,
        )
        assert rec is not None
        # excess = 1M, reserved = 800k, available = 200k, profit = 400k → 200k
        assert rec.eligible_amount == 200_000.0

    def test_sweep_never_auto_executes(self) -> None:
        """SweepPolicyConfig must default to auto_execute=False."""
        cfg = SweepPolicyConfig()
        assert cfg.auto_execute is False
        assert cfg.approval_required is True

    def test_below_minimum_no_sweep(self) -> None:
        engine = SweepEngine(
            SweepPolicyConfig(active_capital_cap=5_000_000, min_sweep_amount=10_000)
        )
        rec = engine.evaluate(total_balance=5_005_000, daily_realized_profit=1_000)
        assert rec is None  # 1k < 10k minimum

    def test_unrealized_profit_never_swept(self) -> None:
        """Only realized (daily_realized_profit) counts. If there's
        unrealized gains but realized is negative, no sweep."""
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate(total_balance=7_000_000, daily_realized_profit=-10_000)
        assert rec is None

    def test_mark_approved_and_rejected(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=1_000))
        rec = engine.evaluate(total_balance=2_000, daily_realized_profit=1_000)
        assert rec is not None
        assert engine.mark_approved(rec.sweep_id, "admin")
        assert rec.status == SweepStatus.APPROVED
        assert rec.approved_by == "admin"

    def test_evaluate_for_tier_with_override(self) -> None:
        engine = SweepEngine(SweepPolicyConfig(active_capital_cap=5_000_000))
        rec = engine.evaluate_for_tier(
            total_balance=200_000,
            daily_realized_profit=50_000,
            capital_cap_override=150_000,
        )
        assert rec is not None
        assert rec.eligible_amount == 50_000.0

    def test_destination_defaults_to_manual_review(self) -> None:
        engine = SweepEngine()
        assert engine.config.default_destination == DestinationType.MANUAL_REVIEW


# ===========================================================================
# Helpers
# ===========================================================================


def _make_liquidity(
    bid: float = 100.0, ask: float = 100.1, depth_10bps: float = 100.0
) -> LiquidityMetrics:
    m = LiquidityMetrics(symbol="TEST", exchange="test", bid=bid, ask=ask)
    m.spread_pct = (ask - bid) / ((bid + ask) / 2) * 100 if bid > 0 else 0
    m.depth_10bps = depth_10bps
    m.volume_24h = 50_000_000.0
    m.liquidity_score = 0.8
    return m
