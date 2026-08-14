"""Tests for the explicit simulation-only high-utilization profile."""

from src.paper.orchestrator import PaperTradingOrchestrator


def test_aggressive_paper_profile_uses_full_balance_without_leverage(tmp_path) -> None:
    balance = 20_000.0
    orchestrator = PaperTradingOrchestrator(
        symbols=["BTCUSDT"],
        initial_balance=balance,
        db_path=str(tmp_path / "aggressive.db"),
        aggressive_paper=True,
    )

    assert orchestrator.risk_engine.max_total_exposure_usd == balance
    assert orchestrator.risk_engine.max_position_size_usd == balance / 10.0
    assert orchestrator.risk_engine.max_leverage == 1.0
    assert orchestrator.allocator.config.reserve_pct == 0.0
    assert orchestrator.allocator.config.max_single_position_pct == 10.0
    assert orchestrator.allocator.config.max_single_exchange_pct == 100.0
    assert orchestrator.tier_manager.config.active_capital_pct == 100.0
    assert orchestrator.tier_manager.config.slots_level_2 == 20
    assert orchestrator._scan_interval == 2.0


def test_default_orchestrator_keeps_conservative_profile(tmp_path) -> None:
    orchestrator = PaperTradingOrchestrator(symbols=["BTCUSDT"], db_path=str(tmp_path / "safe.db"))

    assert orchestrator.allocator.config.reserve_pct == 5.0
    assert orchestrator.tier_manager.config.slots_level_2 == 5
    assert orchestrator._scan_interval == 5.0
