"""End-to-end paper trading integration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.features.engine import FeatureEngine, InstrumentFeatures
from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.position_monitor import PositionMonitor
from src.portfolio.capital_tiers import CapitalTierManager
from src.portfolio.markets import (
    AssetQualityFilter,
    QualityTier,
)
from src.risk.engine import RiskDecision, RiskEngine
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.trailing_stop import TrailConfig


# ===========================================================================
# Paper Account tests
# ===========================================================================
class TestPaperAccount:
    def test_open_and_close_long_win(self) -> None:
        acct = PaperAccount(10000)
        pos = acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5.0, stop_loss_price=49850)
        assert pos is not None
        assert acct.state.cash < 10000
        assert len(acct.state.open_positions) == 1
        # Price rises
        trade = acct.close_position("BTC-USDT", 51000, fees=5.0, exit_reason="trail_hit")
        assert trade is not None
        assert trade.net_pnl > 0
        assert acct.state.trade_count == 1
        assert acct.state.win_count == 1

    def test_open_and_close_long_loss_hard_stop(self) -> None:
        acct = PaperAccount(10000)
        _pos = acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5.0, stop_loss_price=49850)
        trade = acct.close_position(
            "BTC-USDT", 49840, fees=5.0, exit_reason="hard_stop", slippage=1.0
        )
        assert trade is not None
        assert trade.net_pnl < 0
        assert trade.exit_reason == "hard_stop"
        assert acct.state.loss_count == 1

    def test_insufficient_cash(self) -> None:
        acct = PaperAccount(100)
        pos = acct.open_position("BTC-USDT", "long", 50000, 0.1)
        assert pos is None

    def test_update_market_price(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("ETH-USDT", "long", 3000, 1.0, fees=3.0)
        acct.update_market_price("ETH-USDT", 3100)
        pos = acct.state.open_positions["ETH-USDT"]
        assert pos.unrealized_pnl == pytest.approx(100, rel=0.1)
        assert acct.state.unrealized_pnl == pytest.approx(100, rel=0.1)

    def test_equity_calculation(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("SOL-USDT", "long", 100, 10, fees=1.0)
        acct.update_market_price("SOL-USDT", 110)
        assert acct.state.equity > 10000

    def test_daily_update(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5)
        acct.close_position("BTC-USDT", 51000, fees=5)
        acct.update_daily()
        assert acct.state.daily_pnl != 0


# ===========================================================================
# Position Monitor tests
# ===========================================================================
class TestPositionMonitor:
    def test_hard_stop_trigger(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1, stop_loss_price=49850)
        pos = acct.state.open_positions["BTC-USDT"]
        pos.current_price = 49840
        monitor = PositionMonitor(acct)
        monitor.register_position(pos)
        exits = monitor.check_all()
        assert len(exits) > 0
        assert exits[0]["reason"] == "hard_stop"

    def test_trailing_activation_and_exit(self) -> None:
        acct = PaperAccount(10000)
        cfg = TrailConfig(
            trail_pct=0.20,
            activation_pct=0.20,
            trailing_delta=0.002,
            enable_fixed_take_profit=False,
        )
        acct.open_position("BTC-USDT", "long", 50000, 0.1)
        pos = acct.state.open_positions["BTC-USDT"]
        monitor = PositionMonitor(acct, trail_config=cfg)
        monitor.register_position(pos)
        # Rise above activation (50000 * 1.002 = 50100)
        pos.current_price = 50100
        exits = monitor.check_all()
        assert len(exits) == 0  # Not yet triggered
        # Peak at 50200, trail = 50200 * 0.998 = 50099.6
        pos.current_price = 50200
        monitor.check_all()
        # Drop below trail
        pos.current_price = 50095
        exits = monitor.check_all()
        assert len(exits) > 0
        assert exits[0]["reason"] == "trail_hit"

    def test_no_exit_above_stop(self) -> None:
        acct = PaperAccount(10000)
        acct.open_position("ETH-USDT", "long", 3000, 1.0, stop_loss_price=2991)
        pos = acct.state.open_positions["ETH-USDT"]
        pos.current_price = 3050
        monitor = PositionMonitor(acct)
        monitor.register_position(pos)
        exits = monitor.check_all()
        assert len(exits) == 0


# ===========================================================================
# Strategy tests
# ===========================================================================
class TestStrategies:
    def test_momentum_strategy_signal(self) -> None:
        strat = MomentumStrategy()
        feat = InstrumentFeatures(symbol="BTC-USDT", last_price=50000)
        feat.momentum_1m = 1.5
        feat.momentum_5m = 3.0
        feat.acceleration = 0.8
        feat.trend_strength = 0.6
        feat.sample_count = 50
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        assert sig.strategy_id == "momentum_v1"
        assert sig.direction == SignalDirection.LONG

    def test_momentum_no_signal_when_flat(self) -> None:
        strat = MomentumStrategy()
        feat = InstrumentFeatures(symbol="BTC-USDT", last_price=50000)
        feat.sample_count = 50
        feat.momentum_1m = 0.0
        feat.momentum_5m = 0.0
        feat.trend_strength = 0.0
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is None

    def test_breakout_strategy_signal(self) -> None:
        strat = BreakoutStrategy()
        feat = InstrumentFeatures(symbol="ETH-USDT", last_price=3000)
        feat.breakout_position_pct = 90
        feat.relative_volume = 3.5
        feat.momentum_1m = 2.0
        feat.trend_strength = 0.5
        feat.sample_count = 30
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        assert sig.strategy_id == "breakout_v1"

    def test_order_flow_strategy_signal(self) -> None:
        strat = OrderFlowStrategy()
        feat = InstrumentFeatures(symbol="SOL-USDT", last_price=100)
        feat.bid_ask_ratio = 1.8
        feat.trade_flow_ratio = 2.0
        feat.momentum_1m = 0.5
        feat.spread_bps = 5.0
        feat.sample_count = 20
        sig = asyncio.run(strat.analyze(features=feat))
        assert sig is not None
        assert sig.strategy_id == "order_flow_v1"


# ===========================================================================
# Capital Tier tests
# ===========================================================================
class TestCapitalTiers:
    def test_level_1_default(self) -> None:
        mgr = CapitalTierManager()
        s = mgr.determine_tier(3000)
        assert s.target_slots == 2

    def test_level_2_slots(self) -> None:
        mgr = CapitalTierManager()
        s = mgr.determine_tier(50000)
        assert s.target_slots == 5

    def test_level_3_restricts_tiers(self) -> None:
        mgr = CapitalTierManager()
        s = mgr.determine_tier(500000)
        assert QualityTier.TIER_C not in s.allowed_tiers


# ===========================================================================
# Quality Filter tests
# ===========================================================================
class TestQualityFilter:
    def test_tier_a_btc(self) -> None:
        qf = AssetQualityFilter()
        from src.portfolio.liquidity import LiquidityMetrics

        liq = LiquidityMetrics(symbol="BTC-USDT", exchange="binance", bid=49999, ask=50001)
        liq.depth_10bps = 5000.0
        liq.liquidity_score = 0.95
        report = qf.assess(
            "BTC-USDT",
            "binance",
            liquidity=liq,
            volume_24h=500_000_000,
            spread_pct=0.004,
            data_age_seconds=1,
            market_age_days=3000,
            daily_trades=500000,
        )
        assert report.qualified
        assert report.tier == QualityTier.TIER_A

    def test_tier_d_illiquid(self) -> None:
        qf = AssetQualityFilter()
        report = qf.assess(
            "ILL-USD",
            "binance",
            volume_24h=500,
            spread_pct=15.0,
            data_age_seconds=600,
            daily_trades=5,
        )
        assert not report.qualified


# ===========================================================================
# End-to-end pipeline tests
# ===========================================================================
class TestEndToEndPipeline:
    def test_signal_to_approved_flow(self) -> None:
        """Signal → Opportunity → Risk → Approved."""
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.9,
            estimated_return=2.0,
            required_capital=500,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            metadata={"entry_price": 50000.0},
        )
        engine = OpportunityEngine()
        opp = engine.evaluate(signal)
        assert opp.status.value == "ranked"
        risk = RiskEngine()
        assessment = risk.assess(opp)
        assert assessment.decision == RiskDecision.APPROVED

    def test_signal_rejected_by_risk(self) -> None:
        """Short signal rejected by spot-only gate."""
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USDT",
            direction=SignalDirection.SHORT,
            confidence=0.9,
            estimated_return=2.0,
            required_capital=500,
            metadata={"entry_price": 50000.0},
        )
        engine = OpportunityEngine()
        opp = engine.evaluate(signal)
        risk = RiskEngine()
        assessment = risk.assess(opp)
        assert assessment.decision == RiskDecision.REJECTED

    def test_opportunity_rejected_low_confidence(self) -> None:
        signal = StrategySignal(
            strategy_id="test",
            symbol="BTC-USDT",
            direction=SignalDirection.LONG,
            confidence=0.1,
            estimated_return=2.0,
            metadata={"entry_price": 50000.0},
        )
        engine = OpportunityEngine(min_confidence=0.5)
        opp = engine.evaluate(signal)
        assert opp.status.value == "rejected"

    def test_capital_release_after_close(self) -> None:
        """Capital is released and available after position closes."""
        acct = PaperAccount(10000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5.0)
        cash_before = acct.state.cash
        acct.close_position("BTC-USDT", 51000, fees=5.0)
        assert acct.state.cash > cash_before
        assert len(acct.state.open_positions) == 0

    def test_two_simultaneous_positions(self) -> None:
        acct = PaperAccount(20000)
        p1 = acct.open_position("BTC-USDT", "long", 50000, 0.1, fees=5)
        p2 = acct.open_position("ETH-USDT", "long", 3000, 1.0, fees=3)
        assert p1 is not None
        assert p2 is not None
        assert len(acct.state.open_positions) == 2

    def test_duplicate_order_prevention(self) -> None:
        """Second position for same symbol should replace or be rejected."""
        acct = PaperAccount(20000)
        acct.open_position("BTC-USDT", "long", 50000, 0.1)
        # Opening same symbol again should fail (already in dict)
        # Actually our implementation replaces — that's fine for paper
        p2 = acct.open_position("BTC-USDT", "long", 51000, 0.1)
        # Duplicate same-symbol is now rejected
        assert p2 is None

    def test_daily_report_generation(self) -> None:
        from src.portfolio.capital_tiers import CapitalTierManager, generate_daily_report

        mgr = CapitalTierManager()
        mgr.determine_tier(50000)
        report = generate_daily_report(
            mgr,
            50000,
            daily_pnl=200,
            daily_fees=5,
            daily_slippage=2,
            allocation_by_asset={"BTC-USD": 30000},
            allocation_by_strategy={"momentum_v1": 30000},
            avg_order_allocation=15000,
        )
        assert report.portfolio_level == "LEVEL_2"
        assert report.total_balance == 50000
        assert report.daily_realized_pnl == 200

    def test_capital_tier_transition(self) -> None:
        mgr = CapitalTierManager()
        s1 = mgr.determine_tier(4000)
        assert s1.level.value == "level_1"
        s2 = mgr.determine_tier(6000)
        assert s2.level.value == "level_2"
        s3 = mgr.determine_tier(200000)
        assert s3.level.value == "level_3"
        s4 = mgr.determine_tier(6000000)
        assert s4.level.value == "level_4"

    def test_feature_engine_basic(self) -> None:
        engine = FeatureEngine()
        f = engine.update_price("BTC-USDT", 50000.0)
        assert f.last_price == 50000.0
        assert f.sample_count == 1
        f = engine.update_price("BTC-USDT", 50100.0)
        assert f.sample_count == 2
        f = engine.update_order_book("BTC-USDT", 49990, 50010)
        assert f.spread_bps > 0
