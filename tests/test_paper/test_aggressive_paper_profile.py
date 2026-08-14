"""Tests for the explicit simulation-only high-utilization profile."""

from src.paper.orchestrator import (
    PaperTradingOrchestrator,
    _aggressive_paper_allocation,
    _reward_risk_is_acceptable,
)


def test_aggressive_paper_profile_uses_full_balance_without_leverage(tmp_path) -> None:
    balance = 20_000.0
    orchestrator = PaperTradingOrchestrator(
        symbols=["BTCUSDT"],
        initial_balance=balance,
        db_path=str(tmp_path / "aggressive.db"),
        aggressive_paper=True,
    )

    assert orchestrator.risk_engine.max_total_exposure_usd == balance
    assert orchestrator.risk_engine.max_position_size_usd == balance / 2.0
    assert orchestrator.risk_engine.max_leverage == 1.0
    assert orchestrator.allocator.config.reserve_pct == 0.0
    assert orchestrator.allocator.config.max_single_position_pct == 50.0
    assert orchestrator.allocator.config.max_single_exchange_pct == 100.0
    assert orchestrator.tier_manager.config.active_capital_pct == 100.0
    assert orchestrator.tier_manager.config.slots_level_2 == 20
    assert orchestrator.opportunity_engine.min_confidence == 0.65
    assert orchestrator.entry_quality.config.min_quality_score == 0.72
    assert orchestrator.liquidity_gate.config.min_top_book_notional == 100.0
    assert orchestrator.liquidity_gate.config.max_spread_bps == 25.0
    assert orchestrator.strategy_risk.config.max_strategy_consecutive_losses == 2
    assert orchestrator._scan_interval == 2.0


def test_quality_profile_rejects_bad_reward_to_effective_risk() -> None:
    # Reproduces the failed run: 0.50% gross versus 0.30% costs + 0.30% stop.
    assert not _reward_risk_is_acceptable(0.005, 0.003, 0.30)
    assert _reward_risk_is_acceptable(0.009, 0.003, 0.30)


def test_aggressive_allocation_divides_cash_by_actual_candidates() -> None:
    assert _aggressive_paper_allocation(10_000.0, 5_000.0, 20, 2) == (5_000.0, 2)
    assert _aggressive_paper_allocation(10_000.0, 5_000.0, 20, 5) == (2_000.0, 5)
    assert _aggressive_paper_allocation(10_000.0, 5_000.0, 20, 20) == (500.0, 20)
    # A lone candidate is capped at 50%, preserving multi-symbol deployment.
    assert _aggressive_paper_allocation(10_000.0, 5_000.0, 20, 1) == (5_000.0, 1)


def test_default_orchestrator_keeps_conservative_profile(tmp_path) -> None:
    orchestrator = PaperTradingOrchestrator(symbols=["BTCUSDT"], db_path=str(tmp_path / "safe.db"))

    assert orchestrator.allocator.config.reserve_pct == 5.0
    assert orchestrator.tier_manager.config.slots_level_2 == 5
    assert orchestrator._scan_interval == 5.0
