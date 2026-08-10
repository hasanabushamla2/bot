"""Trailing Stop tests — FINAL: trailing_delta = 0.002 (0.20%)."""
from __future__ import annotations

import pytest

from src.paper.account import PaperAccount
from src.paper.position_monitor import PositionMonitor
from src.strategies.trailing_stop import (
    TrailConfig,
    TrailDirection,
    TrailingStopManager,
)


class TestTrailingDelta:
    def test_config_defaults_020(self) -> None:
        cfg = TrailConfig()
        assert cfg.trail_pct == 0.20
        assert cfg.activation_pct == 0.20
        assert cfg.trailing_delta == 0.002
        assert cfg.enable_fixed_take_profit is False

    def test_A_small_retracement_holds(self) -> None:
        """Retracement < 0.2% → HOLD."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("A-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 100.25)
        assert ts.activated
        mgr.update(ts, 100.10)
        assert not mgr.should_exit(ts)

    def test_B_exit_at_threshold(self) -> None:
        """exact 0.2% retracement → EXIT."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("B-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 100.30)
        mgr.update(ts, 100.0994)
        assert mgr.should_exit(ts)

    def test_C_extended_rally_trail(self) -> None:
        """entry=100→110, trail=109.78."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("C-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 105.0)
        mgr.update(ts, 110.0)
        assert ts.peak_price == 110.0
        assert ts.trail_level == pytest.approx(109.78, rel=0.001)

    def test_D_highest_never_decreases(self) -> None:
        """peak must ratchet upward only."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("D-USD", TrailDirection.LONG, 100.0)
        peaks = []
        for price in [100.0, 102.0, 101.90, 103.0, 102.80, 104.0]:
            mgr.update(ts, price)
            peaks.append(ts.peak_price)
        assert peaks == [100.0, 102.0, 102.0, 103.0, 103.0, 104.0]

    def test_E_small_retracement_no_exit(self) -> None:
        """<0.2% → no exit."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("E-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 102.0)
        mgr.update(ts, 101.85)
        assert not mgr.should_exit(ts)

    def test_F_exact_retracement_exits(self) -> None:
        """exactly 0.2% → exit."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("F-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 102.0)
        trail = 102.0 * (1.0 - 0.002)
        mgr.update(ts, trail)
        assert mgr.should_exit(ts)

    def test_G_large_retracement_exits(self) -> None:
        """>0.2% → exit."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("G-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 105.0)
        trail = 105.0 * (1.0 - 0.002)
        mgr.update(ts, trail - 0.5)
        assert mgr.should_exit(ts)

    def test_H_restart_restores_highest(self) -> None:
        """restart → highest price restored."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("H-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 108.0)
        saved = ts.peak_price
        ts2 = mgr.initialize("H-USD", TrailDirection.LONG, 100.0)
        ts2.peak_price = saved
        mgr.update(ts2, 109.0)
        assert ts2.peak_price == 109.0

    def test_I_duplicate_event_idempotent(self) -> None:
        """Duplicate event → idempotent exit check."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("I-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 105.0)
        trail = 105.0 * (1.0 - 0.002)
        mgr.update(ts, trail - 0.1)
        r1 = mgr.should_exit(ts)
        r2 = mgr.should_exit(ts)
        assert r1
        assert r2

    def test_J_position_monitor_integration(self) -> None:
        """PositionMonitor → trail activation + exit at 0.20%."""
        acct = PaperAccount(10000)
        acct.open_position(
            "J-USD", "long", 100.0, 1.0, fees=0.1, stop_loss_price=99.70
        )
        pos = acct.state.open_positions["J-USD"]
        cfg = TrailConfig(
            trail_pct=0.20,
            activation_pct=0.20,
            trailing_delta=0.002,
        )
        monitor = PositionMonitor(acct, trail_config=cfg)
        monitor.register_position(pos)
        pos.current_price = 100.50
        assert len(monitor.check_all()) == 0
        pos.current_price = 100.25
        exits = monitor.check_all()
        assert len(exits) > 0
        assert exits[0]["reason"] == "trail_hit"

    def test_trail_level_uses_delta(self) -> None:
        """trail_level = peak * (1 - trailing_delta)."""
        mgr = TrailingStopManager(
            TrailConfig(trail_pct=0.20, activation_pct=0.20, trailing_delta=0.002)
        )
        ts = mgr.initialize("X-USD", TrailDirection.LONG, 100.0)
        mgr.update(ts, 105.0)
        assert ts.trail_level == pytest.approx(104.79, rel=0.001)

    def test_no_fixed_take_profit(self) -> None:
        assert TrailConfig().enable_fixed_take_profit is False
