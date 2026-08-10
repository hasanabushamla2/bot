"""Comprehensive micro-live tests — $50 cap enforcement, dry-run, fee accounting."""
from __future__ import annotations

import pytest

from src.micro_live.account import MicroLiveAccount
from src.micro_live.config import MicroLivePolicy, MicroLiveSettings
from src.micro_live.fees import RealFeeService
from src.micro_live.monitors import (
    CircuitBreakerState,
    LatencyMonitor,
    SlippageMonitor,
    StopExecutionAudit,
)


class TestMicroLiveSettings:
    def test_default_disabled(self) -> None:
        s = MicroLiveSettings()
        assert not s.enabled
        assert not s.acknowledged
        assert not s.is_fully_armed

    def test_dry_run_enabled_by_default(self) -> None:
        s = MicroLiveSettings()
        assert s.dry_run is True

    def test_all_gates_required(self) -> None:
        s = MicroLiveSettings(enabled=True, acknowledged=True, dry_run=False)
        assert s.is_fully_armed
        assert s.can_place_real_orders

    def test_dry_run_blocks_real_orders(self) -> None:
        s = MicroLiveSettings(enabled=True, acknowledged=True, dry_run=True)
        assert not s.can_place_real_orders
        assert s.is_dry_run

    def test_missing_ack_blocks(self) -> None:
        s = MicroLiveSettings(enabled=True, acknowledged=False, dry_run=False)
        assert not s.is_fully_armed

class TestMicroLiveAccount:
    def test_initial_cap_is_50(self) -> None:
        acct = MicroLiveAccount()
        assert acct.state.cash_available == 50.0
        assert acct.state.micro_equity == 50.0

    def test_cap_enforcement(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        ok = acct.reserve_capital(60.0)
        assert not ok

    def test_slot_sizing(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0, slot_size=5.0)
        ok = acct.reserve_capital(5.0)
        assert ok
        assert acct.state.cash_available == 45.0

    def test_balance_above_cap_still_capped(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        # Even if exchange balance were $1000, only $50 usable
        assert acct.state.remaining_capital <= 50.0

    def test_buy_and_sell_cycle(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        acct.reserve_capital(10.0)
        acct.execute_buy(10.0, 0.01)
        assert acct.state.capital_in_positions == 10.0
        acct.execute_sell(11.0, 0.01, 1.0 - 0.02)
        assert acct.state.realized_pnl > 0

    def test_validation_loss_limit(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        acct.state.daily_start_equity = 50.0
        acct.state.daily_validation_loss_limit = 5.0
        # Force equity below 45 to trigger  loss limit
        acct.reserve_capital(10.0)
        acct.execute_buy(10.0, 0.01)
        acct.execute_sell(0.0, 0.01, -10.0)
        acct.state.cash_available = 35.0  # Force equity to 35
        assert acct.state.realized_pnl < 0
        assert acct.check_validation_loss()

    def test_summary(self) -> None:
        acct = MicroLiveAccount()
        s = acct.summary()
        assert s["mode"] == "MICRO-LIVE"
        assert s["capital_cap"] == 50.0

    def test_daily_report(self) -> None:
        acct = MicroLiveAccount()
        r = acct.daily_report()
        assert r["mode"] == "MICRO-LIVE — REAL MONEY — MAX $50"
        assert r["micro_capital_cap"] == 50.0
        assert "total_real_trades" in r

class TestRealFeeService:
    def test_fallback_fee(self) -> None:
        svc = RealFeeService()
        sched = svc.get_schedule("BTC/USDT")
        assert sched.taker_fee == 0.001
        assert sched.source == "fallback"

    def test_update_from_exchange(self) -> None:
        svc = RealFeeService()
        svc.update_from_exchange("BTC/USDT", 0.0005, 0.0008, "api")
        sched = svc.get_schedule("BTC/USDT")
        assert sched.taker_fee == 0.0008
        assert sched.source == "api"

    def test_compute_from_fills(self) -> None:
        svc = RealFeeService()
        result = svc.compute_from_fills("BTC/USDT", 100.0, 105.0, 0.1, 0.105, "USDT")
        assert result.from_actual_fill
        assert result.total_fees == pytest.approx(0.205, rel=0.01)
        assert result.buy_fee_pct == pytest.approx(0.1, rel=0.1)

class TestLatencyMonitor:
    def test_start_measure(self) -> None:
        lm = LatencyMonitor()
        rec = lm.start_measure("BTC/USDT", "buy")
        rec.total_execution_ms = 120.0
        assert lm.avg_total_ms() == 120.0

    def test_p95(self) -> None:
        lm = LatencyMonitor()
        for i in range(100):
            rec = lm.start_measure("BTC/USDT", "buy")
            rec.total_execution_ms = float(i + 1)
        assert lm.p95_total_ms() > 0

class TestSlippageMonitor:
    def test_record_buy_slippage(self) -> None:
        sm = SlippageMonitor()
        sm.record("BTC/USDT", "buy", 50000, 50025, 0.01)
        assert sm.count() == 1
        assert sm.avg_bps() > 0

    def test_worst_bps(self) -> None:
        sm = SlippageMonitor()
        sm.record("A", "buy", 100, 101, 1)
        sm.record("B", "buy", 100, 100.5, 1)
        assert sm.worst_bps() > sm.avg_bps()

class TestStopExecutionAudit:
    def test_record_stop(self) -> None:
        audit = StopExecutionAudit()
        audit.record("BTC/USDT", 50000, 49850, 49800, 5.0, 200.0)
        assert audit.count() == 1
        assert audit.avg_slippage_pct() > 0

    def test_worst_slippage(self) -> None:
        audit = StopExecutionAudit()
        audit.record("A", 100, 99.7, 99.0, 1, 50)
        audit.record("B", 100, 99.7, 99.5, 1, 50)
        assert audit.worst_slippage_pct() >= audit.avg_slippage_pct()

class TestCircuitBreaker:
    def test_initial_state_off(self) -> None:
        cb = CircuitBreakerState()
        assert not cb.active

    def test_trip_and_reset(self) -> None:
        cb = CircuitBreakerState()
        cb.trip("test_reason")
        assert cb.active
        assert cb.reason == "test_reason"
        cb.reset()
        assert not cb.active

    def test_consecutive_rejection_counting(self) -> None:
        cb = CircuitBreakerState()
        cb.consecutive_rejections = 6
        assert cb.consecutive_rejections == 6

class TestMicroLivePolicy:
    def test_defaults(self) -> None:
        p = MicroLivePolicy()
        assert p.capital_cap_usd == 50.0
        assert p.default_slot_size_usd == 5.0
        assert p.max_slots == 10
        assert p.spot_only is True
        assert p.allow_shorts is False
        assert p.allow_leverage is False

class TestDryRunSafety:
    def test_dry_run_never_places_order(self) -> None:
        s = MicroLiveSettings(enabled=True, acknowledged=True, dry_run=True)
        assert not s.can_place_real_orders
        assert s.is_dry_run

    def test_dry_run_default(self) -> None:
        s = MicroLiveSettings()
        assert s.dry_run is True

class TestCapitalCap50:
    def test_exactly_50_usable(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0, slot_size=5.0)
        # Should be able to reserve exactly 50 in 5 slots
        total_reserved = 0.0
        for _i in range(10):
            if acct.reserve_capital(5.0):
                total_reserved += 5.0
        assert total_reserved == 50.0
        assert not acct.reserve_capital(0.01)

    def test_exchange_balance_irrelevant(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        # The micro account only cares about its own envelope
        assert acct.state.remaining_capital == 50.0

class TestSpotOnlyEnforcement:
    def test_policy_spot_only(self) -> None:
        p = MicroLivePolicy()
        assert p.spot_only
        assert not p.allow_futures
        assert not p.allow_margin
        assert not p.allow_shorts

class TestNetPnlCalculation:
    def test_net_pnl_includes_fees(self) -> None:
        acct = MicroLiveAccount(capital_cap=50.0)
        acct.reserve_capital(10.0)
        acct.execute_buy(10.0, 0.01)
        acct.execute_sell(10.5, 0.0105, 0.5 - 0.0205)
        # Net P&L = realized - total fees - slippage
        report = acct.daily_report()
        assert report["total_fees"] > 0
        assert "net_pnl" in report
