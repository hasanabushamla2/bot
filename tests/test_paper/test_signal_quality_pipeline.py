"""Regression coverage for the adaptive qualified-trade pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.features.engine import InstrumentFeatures
from src.paper.account import ClosedTrade
from src.paper.engine import PaperExecutionEngine
from src.paper.reporting import exit_analysis
from src.paper.telemetry import SignalFunnelTelemetry
from src.risk.entry_quality import EntryQualityGate
from src.risk.strategy_risk import StrategyRiskConfig, StrategyRiskManager
from src.risk.symbol_risk import ReentryContext, SymbolRiskConfig, SymbolRiskManager
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.trailing_stop import (
    TrailConfig,
    TrailDirection,
    TrailingStopManager,
    compute_volatility_aware_trail,
)


def _high_quality_features() -> InstrumentFeatures:
    return InstrumentFeatures(
        symbol="AAA-USDT",
        last_price=100.0,
        momentum_1m=0.80,
        momentum_5m=1.10,
        acceleration=0.15,
        atr_pct=0.18,
        volatility_5m_pct=0.20,
        relative_volume=2.2,
        breakout_position_pct=85.0,
        bid=99.99,
        ask=100.01,
        spread_bps=2.0,
        bid_ask_ratio=1.35,
        trade_flow_ratio=1.40,
        trend_strength=0.75,
        sample_count=50,
    )


def _signal(confidence: float = 0.9, observations: int = 2) -> StrategySignal:
    return StrategySignal(
        strategy_id="momentum_v1",
        symbol="AAA-USDT",
        direction=SignalDirection.LONG,
        confidence=confidence,
        estimated_return=0.02,
        metadata={"entry_price": 100.0, "signal_observations": observations},
    )


def test_adaptive_hard_stop_cooldown_scales_with_realized_severity_and_volatility() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(
        SymbolRiskConfig(
            loss_cooldown_seconds=100.0,
            reference_volatility_pct=0.30,
            hard_stop_volatility_scale=0.50,
            hard_stop_loss_return_reference_pct=0.30,
        )
    )
    manager.record_trade_exit(
        "AAA-USDT",
        -10.0,
        "hard_stop",
        exit_time=now,
        return_pct=-0.60,
        direction="long",
        strategy_id="momentum_v1",
        market_volatility_pct=0.60,
    )
    # base 100s × severity 2 × volatility multiplier 2 = 400s.
    assert manager.get_symbol_state("AAA-USDT").cooldown_until == now + timedelta(seconds=400)


def test_profitable_trail_allows_fresh_materially_stronger_reentry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(
        SymbolRiskConfig(
            win_cooldown_seconds=60.0,
            material_confidence_improvement=0.10,
            base_market_structure_score=0.55,
        )
    )
    manager.record_trade_exit(
        "AAA-USDT",
        2.0,
        "trail_hit",
        exit_time=now,
        direction="long",
        strategy_id="breakout_v1",
        entry_confidence=0.70,
        signal_id="old-sequence",
        signal_sequence=1,
        market_volatility_pct=0.20,
    )
    decision = manager.evaluate_entry(
        ReentryContext(
            symbol="AAA-USDT",
            direction="long",
            strategy_id="breakout_v1",
            confidence=0.90,
            signal_id="fresh-sequence",
            signal_sequence=2,
            fresh_signal=True,
            market_structure_score=0.80,
            market_volatility_pct=0.20,
        ),
        current_equity=10_000.0,
        now=now + timedelta(seconds=2),
    )
    assert decision.allowed
    assert decision.early_reentry
    assert decision.reason == "EARLY_REENTRY_NEW_HIGH_QUALITY_SEQUENCE"


def test_immediate_reentry_after_hard_stop_remains_rejected_even_when_strong() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=120.0))
    manager.record_trade_exit(
        "AAA-USDT",
        -1.0,
        "hard_stop",
        exit_time=now,
        direction="long",
        strategy_id="momentum_v1",
        entry_confidence=0.70,
        signal_id="old",
        signal_sequence=1,
    )
    decision = manager.evaluate_entry(
        ReentryContext(
            symbol="AAA-USDT",
            direction="long",
            strategy_id="momentum_v1",
            confidence=0.99,
            signal_id="fresh",
            signal_sequence=2,
            fresh_signal=True,
            market_structure_score=0.99,
            market_volatility_pct=0.10,
        ),
        current_equity=10_000.0,
        now=now + timedelta(seconds=1),
    )
    assert not decision.allowed
    assert decision.reason.startswith("HARD_STOP_COOLDOWN_ACTIVE")


def test_fee_aware_expected_net_edge_rejects_cost_uncovered_trade() -> None:
    engine = PaperExecutionEngine(taker_fee=0.001, slippage_bps=5.0, simulated_latency_ms=0.0)
    insufficient = engine.estimate_expected_net_edge(
        quantity=10.0,
        entry_reference_price=100.0,
        exit_reference_price=99.9,
        expected_gross_edge_fraction=0.003,
        safety_buffer_fraction=0.001,
    )
    sufficient = engine.estimate_expected_net_edge(
        quantity=10.0,
        entry_reference_price=100.0,
        exit_reference_price=99.9,
        expected_gross_edge_fraction=0.010,
        safety_buffer_fraction=0.001,
    )
    assert insufficient.expected_net_edge_fraction < 0.0
    assert not insufficient.is_positive_after_costs
    assert sufficient.expected_net_edge_fraction > 0.0
    assert sufficient.is_positive_after_costs
    assert sufficient.estimated_entry_fee > 0.0
    assert sufficient.estimated_exit_fee > 0.0
    assert sufficient.expected_slippage > 0.0


def test_dynamic_entry_quality_uses_volatility_normalized_momentum() -> None:
    gate = EntryQualityGate()
    high = _high_quality_features()
    # Build a live-history baseline before assessing the current signal.  This
    # is historic/current data only; no future price move is supplied.
    for _ in range(15):
        gate.observe_market(high)
    accepted = gate.assess(_signal(), high)
    assert accepted.passed
    assert accepted.momentum_multiple >= accepted.required_momentum_multiple
    assert accepted.market_structure_score >= 0.55

    weak = _high_quality_features()
    weak.momentum_1m = -0.20
    weak.momentum_5m = -0.10
    weak.acceleration = -0.05
    rejected = gate.assess(_signal(), weak)
    assert not rejected.passed
    assert "momentum_not_directional" in rejected.reasons
    assert "trend_misaligned" not in rejected.reasons


def test_hard_stop_mfe_mae_and_hold_time_are_classified_for_diagnostics() -> None:
    trades = [
        ClosedTrade(
            symbol="AAA-USDT",
            net_pnl=-1.0,
            exit_reason="hard_stop",
            holding_seconds=2.0,
            max_favorable_excursion_pct=0.0,
            max_adverse_excursion_pct=-0.4,
        ),
        ClosedTrade(
            symbol="BBB-USDT",
            net_pnl=-1.0,
            exit_reason="hard_stop",
            holding_seconds=20.0,
            max_favorable_excursion_pct=0.1,
            max_adverse_excursion_pct=-0.5,
        ),
    ]
    diagnostics = exit_analysis(trades)["hard_stop"]["entry_classification"]
    assert diagnostics["short_lived_no_favorable_excursion"] == 1
    assert diagnostics["limited_follow_through"] == 1


def test_volatility_aware_trail_widens_without_creating_a_take_profit_ceiling() -> None:
    params = compute_volatility_aware_trail(
        base_trail_distance_pct=0.20,
        base_activation_pct=0.20,
        volatility_pct=0.60,
        spread_bps=4.0,
        round_trip_cost_fraction=0.003,
        volatility_multiplier=1.5,
        spread_multiplier=2.0,
        max_trail_distance_pct=1.25,
    )
    assert params.trail_distance_pct == pytest.approx(0.90)
    assert params.activation_pct >= 0.75

    manager = TrailingStopManager(TrailConfig(trail_pct=0.20, activation_pct=0.20))
    state = manager.initialize(
        "AAA-USDT",
        TrailDirection.LONG,
        100.0,
        activation_pct=params.activation_pct,
        trail_distance_pct=params.trail_distance_pct,
    )
    manager.update(state, 101.0)
    assert state.activated
    # The 0.90% volatility-aware retracement is materially wider than the
    # legacy 0.20% trail, so a normal small pullback does not clip the winner.
    manager.update(state, 100.50)
    assert not manager.should_exit(state)
    manager.update(state, 100.05)
    assert manager.should_exit(state)


def test_rejection_telemetry_has_required_structured_counters_and_percentages() -> None:
    telemetry = SignalFunnelTelemetry()
    telemetry.increment("raw_signals", 4)
    telemetry.increment("valid_signals", 3)
    telemetry.increment("opportunities_created", 3)
    telemetry.reject("confidence", strategy_id="breakout_v1", symbol="AAA-USDT")
    telemetry.reject("spread", strategy_id="momentum_v1", symbol="BBB-USDT")
    telemetry.reject("cooldown", strategy_id="momentum_v1", symbol="AAA-USDT", entry_attempt=True)

    funnel = telemetry.funnel()
    assert funnel["raw_signals"] == 4
    assert funnel["confidence_rejections"] == 1
    assert funnel["spread_rejections"] == 1
    assert funnel["liquidity_rejections"] == 1
    assert funnel["cooldown_rejections"] == 1
    assert telemetry.entry_rejections == 1
    breakdown = telemetry.rejection_breakdown()
    assert breakdown["confidence"]["pct_of_all_rejections"] == pytest.approx(100 / 3, abs=1e-4)
    assert telemetry.per_strategy_rejections()["momentum_v1"]["cooldown"] == 1


@pytest.mark.asyncio
async def test_orchestrator_report_exposes_end_to_end_signal_funnel() -> None:
    """Integration: live features → signal → funnel report has no absent stage."""
    from src.data.normalization import BookLevel
    from src.paper.orchestrator import PaperTradingOrchestrator
    from src.strategies.momentum_strategy import MomentumStrategy

    orchestrator = PaperTradingOrchestrator(symbols=["AAAUSDT"], initial_balance=10_000.0)
    orchestrator._accepting_new = True
    orchestrator.registry.register(MomentumStrategy())
    await orchestrator.registry.initialize_all()
    symbol = "AAA-USDT"
    book = orchestrator.order_book_engine.get_or_create("binance", symbol)
    book.bids.apply_snapshot([BookLevel(99.99 - step * 0.01, 100.0) for step in range(10)])
    book.asks.apply_snapshot([BookLevel(100.01 + step * 0.01, 100.0) for step in range(10)])
    for _ in range(3):
        features = orchestrator.features.get(symbol)
        features.last_price = 100.0
        features.bid = 99.99
        features.ask = 100.01
        features.sample_count = 50
        features.volume_24h = 2_000_000.0
        features.momentum_1m = 0.80
        features.momentum_5m = 1.10
        features.acceleration = 0.10
        features.volatility_5m_pct = 0.20
        features.atr_pct = 0.18
        features.relative_volume = 2.0
        features.trend_strength = 0.70
        features.spread_bps = 2.0
        features.bid_ask_ratio = 1.25
        features.trade_flow_ratio = 1.2
        await orchestrator._scan_tick()

    report = orchestrator._final_report()
    funnel = report["signal_funnel"]
    assert funnel["raw_signals"] > 0
    assert funnel["qualified_signals"] > 0
    assert funnel["opportunities"] > 0
    assert "execution_attempts" in report["funnel_counters"]
    assert "trade_performance" in report
    assert "exit_analysis" in report
    assert "strategy_analysis" in report
    assert "symbol_analysis" in report
    await orchestrator.registry.shutdown_all()


def test_strategy_allocation_is_unchanged_until_evidence_is_statistically_meaningful() -> None:
    manager = StrategyRiskManager(
        StrategyRiskConfig(
            min_trades_for_allocation_adjustment=30,
            negative_expectancy_allocation_multiplier=0.75,
        )
    )
    for _ in range(5):
        manager.record_trade_exit("momentum_v1", -1.0, -1.0, 0.0, 0.0, "hard_stop")
    assert manager.allocation_multiplier("momentum_v1") == 1.0

    for _ in range(25):
        manager.record_trade_exit("momentum_v1", -1.0, -1.0, 0.0, 0.0, "hard_stop")
    evidence = manager.allocation_evidence("momentum_v1")
    assert evidence["statistically_actionable"]
    assert manager.allocation_multiplier("momentum_v1") == pytest.approx(0.75)
    assert manager.get_summary()["momentum_v1"]["average_mfe_pct"] == 0.0


def test_signal_funnel_state_round_trips_without_silent_stage_loss() -> None:
    telemetry = SignalFunnelTelemetry()
    for stage, amount in (
        ("raw_signals", 10),
        ("valid_signals", 8),
        ("inactive_signals", 2),
        ("opportunities_created", 8),
        ("qualified_opportunities", 4),
        ("approved_opportunities", 2),
        ("execution_attempts", 2),
        ("successful_entries", 1),
        ("closed_trades", 1),
    ):
        telemetry.increment(stage, amount)
    telemetry.reject("expected_edge", strategy_id="momentum_v1", symbol="AAA-USDT")
    restored = SignalFunnelTelemetry.from_dict(telemetry.to_dict())

    assert restored.funnel() == telemetry.funnel()
    assert restored.rejection_breakdown() == telemetry.rejection_breakdown()
    # Every externally required counter exists even if no event reached it.
    for required in (
        "correlation_rejections",
        "risk_rejections",
        "capacity_rejections",
        "execution_attempts",
        "successful_entries",
    ):
        assert required in restored.funnel()


def test_funnel_and_strategy_telemetry_persist_across_restart(tmp_path) -> None:
    from src.db.persist import PaperPersistence

    telemetry = SignalFunnelTelemetry()
    telemetry.increment("raw_signals", 7)
    telemetry.reject("capacity", strategy_id="breakout_v1", symbol="AAA-USDT", entry_attempt=True)
    strategies = StrategyRiskManager()
    strategies.record_trade_exit(
        "breakout_v1", 2.0, 1.0, 0.2, 0.1, "trail_hit", mfe_pct=1.5, mae_pct=-0.2, holding_seconds=12
    )

    path = tmp_path / "telemetry.db"
    writer = PaperPersistence(str(path))
    writer.connect()
    writer.save_telemetry_state("signal_funnel", telemetry.to_dict())
    writer.save_strategy_risk_state(strategies.get_state())
    writer.close()

    reader = PaperPersistence(str(path))
    reader.connect()
    restored_funnel = SignalFunnelTelemetry.from_dict(reader.load_telemetry_state("signal_funnel"))
    restored_strategies = StrategyRiskManager()
    restored_strategies.restore_state(reader.load_strategy_risk_state())
    reader.close()

    assert restored_funnel.funnel()["raw_signals"] == 7
    assert restored_funnel.entry_rejections == 1
    summary = restored_strategies.get_summary()["breakout_v1"]
    assert summary["trade_count"] == 1
    assert summary["average_mfe_pct"] == pytest.approx(1.5)
