"""Deterministic execution/risk lifecycle regressions for soak-test fixes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.db.persist import PaperPersistence
from src.paper.account import PaperAccount
from src.paper.position_monitor import PositionMonitor
from src.risk.entry_guard import EntrySignalGuard
from src.risk.symbol_risk import SymbolRiskConfig, SymbolRiskManager
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.trailing_stop import TrailConfig, TrailDirection, TrailingStopManager


def _signal(symbol: str = "AAA-USDT", strategy: str = "test_v1") -> StrategySignal:
    return StrategySignal(
        strategy_id=strategy,
        symbol=symbol,
        direction=SignalDirection.LONG,
        confidence=0.9,
        estimated_return=0.01,
        metadata={"entry_price": 100.0},
    )


def test_same_consumed_signal_cannot_repeatedly_open_trades() -> None:
    guard = EntrySignalGuard()
    sig = _signal()
    key = guard.key(sig.strategy_id, sig.symbol)
    guard.observe_cycle([sig], [key])
    assert guard.can_enter(sig)[0]
    guard.record_consumed(sig)

    repeated = _signal()
    guard.observe_cycle([repeated], [key])
    assert repeated.signal_id == sig.signal_id
    assert guard.can_enter(repeated) == (False, "DUPLICATE_OR_STALE_SIGNAL")


def test_losing_trade_activates_symbol_cooldown() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(
        SymbolRiskConfig(loss_cooldown_seconds=120, win_cooldown_seconds=10)
    )
    manager.record_trade_exit("AAA-USDT", -1.0, "signal_exit", exit_time=now)
    state = manager.get_symbol_state("AAA-USDT")
    assert state.last_exit_time == now
    assert state.last_exit_reason == "signal_exit"
    assert state.last_trade_profitable is False
    assert state.cooldown_until == now + timedelta(seconds=120)


def test_new_entry_rejected_during_loss_cooldown() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=120))
    manager.record_trade_exit("AAA-USDT", -1.0, "hard_stop", exit_time=now)
    allowed, reason = manager.is_symbol_eligible(
        "AAA-USDT", 10_000.0, now=now + timedelta(seconds=119)
    )
    assert not allowed
    assert "SYMBOL_COOLDOWN_ACTIVE" in reason


def test_new_valid_signal_can_trade_after_cooldown() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=60))
    guard = EntrySignalGuard()
    key = guard.key("test_v1", "AAA-USDT")

    first = _signal()
    guard.observe_cycle([first], [key], now=now)
    guard.record_consumed(first, now)
    manager.record_trade_exit("AAA-USDT", -1.0, "signal_exit", exit_time=now)

    # The continuous old regime remains consumed even after time passes.
    stale = _signal()
    guard.observe_cycle([stale], [key], now=now + timedelta(seconds=61))
    assert not guard.can_enter(stale)[0]

    # A false observation rearms the predicate.  Its next true transition gets
    # a new id and is eligible once the independent cooldown has expired.
    guard.observe_cycle([], [key], now=now + timedelta(seconds=62))
    fresh = _signal()
    guard.observe_cycle([fresh], [key], now=now + timedelta(seconds=63))
    assert fresh.signal_id != first.signal_id
    assert guard.can_enter(fresh)[0]
    assert manager.is_symbol_eligible(
        "AAA-USDT", 10_000.0, now=now + timedelta(seconds=63)
    )[0]


def test_trailing_stop_does_not_activate_before_effective_condition() -> None:
    manager = TrailingStopManager(
        TrailConfig(activation_pct=0.20, trail_pct=0.20, trailing_delta=0.002)
    )
    state = manager.initialize(
        "AAA-USDT", TrailDirection.LONG, 100.0, activation_pct=0.60
    )
    manager.update(state, 100.59)
    assert not state.activated
    assert not manager.should_exit(state)


def test_activated_trailing_reference_only_moves_favorably() -> None:
    manager = TrailingStopManager(
        TrailConfig(activation_pct=0.20, trail_pct=0.20, trailing_delta=0.002)
    )
    state = manager.initialize("AAA-USDT", TrailDirection.LONG, 100.0)
    levels = []
    for price in (101.0, 102.0, 101.8, 103.0, 102.9):
        manager.update(state, price)
        levels.append(state.trail_level)
    assert levels == sorted(levels)


def test_hard_stop_still_works_before_trailing_activation() -> None:
    account = PaperAccount(10_000)
    pos = account.open_position(
        "AAA-USDT",
        "long",
        100.0,
        1.0,
        stop_loss_price=99.7,
        trail_activation_pct=1.0,
    )
    assert pos is not None
    monitor = PositionMonitor(account)
    monitor.register_position(pos)
    pos.current_price = 99.69
    result = monitor.check_position(pos)
    assert result is not None
    assert result["reason"] == "hard_stop"
    assert not pos.trail_activated


def test_fees_charged_exactly_once_and_realized_pnl_is_net() -> None:
    account = PaperAccount(10_000)
    assert account.open_position("AAA-USDT", "long", 100.0, 10.0, fees=1.0)
    trade = account.close_position("AAA-USDT", 101.0, fees=1.01)
    assert trade is not None
    assert trade.gross_pnl == pytest.approx(10.0)
    assert trade.fees == pytest.approx(2.01)
    assert trade.net_pnl == pytest.approx(7.99)
    assert account.state.total_fees == pytest.approx(2.01)
    assert account.state.realized_pnl == pytest.approx(7.99)


def test_cash_equity_and_slippage_reconcile_after_close() -> None:
    account = PaperAccount(10_000)
    quantity = 10.0
    entry_reference = 100.0
    entry_actual = 100.05
    exit_reference = 101.0
    exit_actual = 100.9495
    entry_slippage = (entry_actual - entry_reference) * quantity
    exit_slippage = (exit_reference - exit_actual) * quantity
    entry_fee = entry_actual * quantity * 0.001
    exit_fee = exit_actual * quantity * 0.001

    assert account.open_position(
        "AAA-USDT",
        "long",
        entry_actual,
        quantity,
        fees=entry_fee,
        entry_reference_price=entry_reference,
        entry_slippage_cost=entry_slippage,
    )
    trade = account.close_position(
        "AAA-USDT",
        exit_actual,
        fees=exit_fee,
        exit_reference_price=exit_reference,
        embedded_slippage_cost=exit_slippage,
    )
    assert trade is not None
    expected_net = 10.0 - entry_fee - exit_fee - entry_slippage - exit_slippage
    assert trade.net_pnl == pytest.approx(expected_net)
    assert account.state.cash == pytest.approx(10_000 + expected_net)
    assert account.state.equity == pytest.approx(account.state.cash)
    assert account.state.total_slippage == pytest.approx(entry_slippage + exit_slippage)
    account.assert_invariants()


def test_consecutive_loss_lockout_is_temporary() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(
        SymbolRiskConfig(
            loss_cooldown_seconds=10,
            max_consecutive_losses_per_symbol=2,
            symbol_lockout_seconds=100,
        )
    )
    manager.record_trade_exit("AAA-USDT", -1.0, "signal_exit", exit_time=now)
    manager.record_trade_exit(
        "AAA-USDT", -1.0, "signal_exit", exit_time=now + timedelta(seconds=11)
    )
    assert not manager.is_symbol_eligible(
        "AAA-USDT", 10_000, now=now + timedelta(seconds=50)
    )[0]
    assert manager.is_symbol_eligible(
        "AAA-USDT", 10_000, now=now + timedelta(seconds=112)
    )[0]
    assert manager.consecutive_loss_events_count == 1


def test_different_symbol_not_blocked_by_cooldown() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=300))
    manager.record_trade_exit("AAA-USDT", -1.0, "hard_stop", exit_time=now)
    assert not manager.is_symbol_eligible("AAA-USDT", 10_000, now=now)[0]
    assert manager.is_symbol_eligible("BBB-USDT", 10_000, now=now)[0]


def test_restart_preserves_cooldown_and_consumed_signal(tmp_path) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    db_path = tmp_path / "restart.db"
    risk_a = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=300))
    guard_a = EntrySignalGuard()
    sig = _signal()
    key = guard_a.key(sig.strategy_id, sig.symbol)
    guard_a.observe_cycle([sig], [key], now=now)
    guard_a.record_consumed(sig, now)
    risk_a.record_trade_exit("AAA-USDT", -1.0, "hard_stop", exit_time=now)

    persistence = PaperPersistence(str(db_path))
    persistence.connect()
    persistence.save_symbol_risk_state(risk_a.get_state())
    persistence.save_signal_state(guard_a.get_state())
    persistence.close()

    persistence_b = PaperPersistence(str(db_path))
    persistence_b.connect()
    risk_b = SymbolRiskManager(SymbolRiskConfig(loss_cooldown_seconds=300))
    guard_b = EntrySignalGuard()
    risk_b.restore_state(persistence_b.load_symbol_risk_state())
    guard_b.restore_state(persistence_b.load_signal_state())
    persistence_b.close()

    repeated = _signal()
    guard_b.observe_cycle([repeated], [key], now=now + timedelta(seconds=10))
    assert not risk_b.is_symbol_eligible(
        "AAA-USDT", 10_000, now=now + timedelta(seconds=10)
    )[0]
    assert not guard_b.can_enter(repeated)[0]
    assert repeated.signal_id == sig.signal_id


def test_win_cooldown_is_shorter_than_loss_cooldown() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    manager = SymbolRiskManager(
        SymbolRiskConfig(loss_cooldown_seconds=300, win_cooldown_seconds=30)
    )
    manager.record_trade_exit("AAA-USDT", 1.0, "trailing_stop", exit_time=now)
    assert manager.get_symbol_state("AAA-USDT").last_trade_profitable is True
    assert manager.is_symbol_eligible(
        "AAA-USDT", 10_000, now=now + timedelta(seconds=31)
    )[0]
