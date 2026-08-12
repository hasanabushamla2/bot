"""Comprehensive tests for Liquidity Gate, Execution Estimator, Symbol Risk, Trailing Cleanup, and Reconciliation (Tests A-O + Edge Cases)."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta

import pytest

from src.data.normalization import BookLevel
from src.data.order_book import OrderBookState
from src.db.persist import PaperPersistence
from src.execution.estimator import ExecutionEstimator
from src.execution.liquidity_gate import (
    LiquidityGate,
    LiquidityGateConfig,
    LiquidityRejectionReason,
)
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.portfolio.market_quality import MarketQualityCalculator
from src.risk.engine import RiskEngine
from src.risk.strategy_risk import StrategyRiskConfig, StrategyRiskManager
from src.risk.symbol_risk import SymbolRiskConfig, SymbolRiskManager


def _create_test_book(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    symbol: str = "BTC-USDT",
    exchange: str = "binance",
) -> OrderBookState:
    book = OrderBookState(symbol=symbol, exchange=exchange)
    book.bids.apply_snapshot([BookLevel(p, q) for p, q in bids])
    book.asks.apply_snapshot([BookLevel(p, q) for p, q in asks])
    book.last_update_time = datetime.now(UTC)
    book.initialized = True
    return book


# ══════════════════════════════════════════════════════════════
# TEST A: Extremely shallow book -> rejected
# ══════════════════════════════════════════════════════════
class TestA_ExtremelyShallowBook:
    def test_extremely_shallow_book_rejected(self):
        gate = LiquidityGate(LiquidityGateConfig(min_top_book_notional=500.0))
        # Total notional $20 bid, $20 ask
        shallow_book = _create_test_book(
            bids=[(100.0, 0.2)],
            asks=[(100.1, 0.2)],
        )
        res = gate.assess_market("SHALLOW-USDT", shallow_book, volume_24h=500_000, data_age_seconds=5.0)
        assert not res.passed
        assert res.reason == LiquidityRejectionReason.BOOK_TOO_SHALLOW


# ══════════════════════════════════════════════════════════════
# TEST B: Huge spread -> rejected
# ══════════════════════════════════════════════════════════
class TestB_HugeSpread:
    def test_huge_spread_rejected(self):
        gate = LiquidityGate(LiquidityGateConfig(max_spread_bps=35.0))
        # Spread = (105 - 95)/100 = 10% = 1000 bps > 35 bps
        wide_book = _create_test_book(
            bids=[(95.0, 10.0), (94.0, 10.0)],
            asks=[(105.0, 10.0), (106.0, 10.0)],
        )
        res = gate.assess_market("WIDE-USDT", wide_book, volume_24h=500_000, data_age_seconds=5.0)
        assert not res.passed
        assert res.reason == LiquidityRejectionReason.SPREAD_TOO_WIDE


# ══════════════════════════════════════════════════════════════
# TEST C: Entry quantity would produce > max slippage -> rejected
# ══════════════════════════════════════════════════════════
class TestC_EntryQuantitySlippage:
    def test_entry_slippage_rejected(self):
        estimator = ExecutionEstimator(max_entry_slippage_bps=25.0)
        # Asks: 100.0 (qty 1), 101.0 (qty 1), 105.0 (qty 10)
        book = _create_test_book(
            bids=[(99.9, 10.0)],
            asks=[(100.0, 1.0), (101.0, 1.0), (105.0, 10.0)],
        )
        # Request qty 5: fills 1@100 + 1@101 + 3@105 = 516 / 5 = 103.2 -> slippage 3.2% = 320 bps > 25 bps
        res = estimator.simulate_buy_entry("THIN-USDT", book, requested_qty=5.0)
        assert not res.passed
        assert res.rejection_reason == LiquidityRejectionReason.ENTRY_SLIPPAGE_TOO_HIGH


# ══════════════════════════════════════════════════════════════
# TEST D: Exit liquidity insufficient -> entry rejected
# ══════════════════════════════════════════════════════════
class TestD_ExitLiquidityInsufficient:
    def test_exit_liquidity_insufficient_rejected(self):
        estimator = ExecutionEstimator(max_exit_slippage_bps=35.0, max_effective_stop_loss_pct=0.80)
        # Asks are deep, but Bids are thin (e.g. 99.9 qty 0.1, 90.0 qty 10)
        book = _create_test_book(
            bids=[(99.9, 0.1), (90.0, 10.0)],
            asks=[(100.0, 10.0), (100.1, 10.0)],
        )
        # Sized position of 2.0 would have to sell into 90.0 bids -> exit slippage > 9%
        res = estimator.simulate_sell_exit("TRAP-USDT", book, position_qty=2.0, stop_loss_pct=0.30)
        assert not res.passed
        assert res.rejection_reason in (
            LiquidityRejectionReason.EXIT_SLIPPAGE_TOO_HIGH,
            LiquidityRejectionReason.EFFECTIVE_STOP_LOSS_TOO_HIGH,
        )


# ══════════════════════════════════════════════════════════════
# TEST E: Large requested quantity -> liquidity sizing caps quantity
# ══════════════════════════════════════════════════════════
class TestE_LargeRequestedQuantitySizing:
    def test_liquidity_sizing_caps_quantity(self):
        estimator = ExecutionEstimator(
            max_entry_slippage_bps=25.0,
            max_depth_participation_pct=0.10,
            max_levels_consumed=5,
        )
        # Book has total 100 qty
        book = _create_test_book(
            bids=[(99.9 - i * 0.01, 20.0) for i in range(5)],
            asks=[(100.0 + i * 0.01, 20.0) for i in range(5)],
        )
        # Strategy wants 500 qty ($50k), Risk wants 500 qty
        safe_qty = estimator.compute_max_safe_quantity(
            "CAPPED-USDT", book, risk_qty=500.0, capital_qty=500.0, strategy_qty=500.0
        )
        # Should be capped by participation (10% of 100 = 10.0)
        assert safe_qty <= 10.0001
        assert safe_qty > 0.0


# ══════════════════════════════════════════════════════════════
# TEST F: Stop triggers in shallow market -> realistic VWAP and explicit slippage
# ══════════════════════════════════════════════════════════
class TestF_StopTriggersRealisticVWAP:
    @pytest.mark.asyncio
    async def test_stop_triggers_realistic_vwap(self):
        engine = PaperExecutionEngine(taker_fee=0.001, slippage_bps=0.0, simulated_latency_ms=0.0)
        # Bids have levels: 100 (qty 1), 98 (qty 1), 95 (qty 2)
        bids_depth = [(100.0, 1.0), (98.0, 1.0), (95.0, 2.0)]
        res = await engine.simulate_fill(
            "TEST-USDT", "sell", quantity=3.0, bid=100.0, ask=100.1, last=100.0, bids_depth=bids_depth
        )
        # VWAP = (1*100 + 1*98 + 1*95)/3 = 293 / 3 = 97.6667
        assert res.filled_qty == 3.0
        assert res.fill_price == pytest.approx(97.6667, rel=1e-3)
        # Slippage vs top of book (100.0): (100 - 97.6667)/100 = 2.333% = 233.3 bps
        assert res.slippage_bps == pytest.approx(233.33, rel=1e-2)
        assert res.levels_consumed == 3


# ══════════════════════════════════════════════════════════════
# TEST G: Repeated stop-outs on same symbol -> symbol cooldown
# ══════════════════════════════════════════════════════════
class TestG_RepeatedStopoutsSymbolCooldown:
    def test_repeated_stopouts_trigger_cooldown(self):
        mgr = SymbolRiskManager(SymbolRiskConfig(symbol_cooldown_seconds=300.0))
        now = datetime.now(UTC)

        # 1st stopout
        mgr.record_trade_exit("RARI-USDT", net_pnl=-30.0, exit_reason="hard_stop", slippage_bps=10.0, exit_time=now)
        eligible, _ = mgr.is_symbol_eligible("RARI-USDT", 10000.0, now=now + timedelta(seconds=10))
        assert not eligible  # On cooldown

        # Fast forward past 1st cooldown
        eligible, _ = mgr.is_symbol_eligible("RARI-USDT", 10000.0, now=now + timedelta(seconds=350))
        assert eligible

        # 2nd stopout -> exponential backoff cooldown (2 * 300 = 600s)
        mgr.record_trade_exit(
            "RARI-USDT", net_pnl=-30.0, exit_reason="hard_stop", slippage_bps=10.0, exit_time=now + timedelta(seconds=360)
        )
        eligible, reason = mgr.is_symbol_eligible("RARI-USDT", 10000.0, now=now + timedelta(seconds=400))
        assert not eligible
        assert "SYMBOL_COOLDOWN_ACTIVE" in reason


# ══════════════════════════════════════════════════════════════
# TEST H: Immediate re-entry after hard stop -> blocked
# ══════════════════════════════════════════════════════════
class TestH_ImmediateReentryBlocked:
    def test_immediate_reentry_blocked(self):
        mgr = SymbolRiskManager(SymbolRiskConfig(symbol_cooldown_seconds=300.0))
        now = datetime.now(UTC)

        mgr.record_trade_exit("FLK-USDT", net_pnl=-10.0, exit_reason="hard_stop", exit_time=now)
        # Attempt to re-enter 5 seconds later
        eligible, reason = mgr.is_symbol_eligible("FLK-USDT", 10000.0, now=now + timedelta(seconds=5))
        assert not eligible
        assert "COOLDOWN" in reason
        assert mgr.reentry_blocks_count >= 1


# ══════════════════════════════════════════════════════════════
# TEST I: Partial fill -> filled position preserved and remainder canceled
# ══════════════════════════════════════════════════════════
class TestI_PartialFillLifecycle:
    @pytest.mark.asyncio
    async def test_partial_fill_terminal_state(self):
        engine = PaperExecutionEngine(simulated_latency_ms=0.0)
        # Asks depth only has 0.4 qty
        asks_depth = [(100.0, 0.4)]
        res = await engine.simulate_fill(
            "PARTIAL-USDT", "buy", quantity=1.0, bid=99.9, ask=100.0, asks_depth=asks_depth
        )
        assert res.status == "PARTIALLY_FILLED_CANCELED"
        assert res.filled_qty == pytest.approx(0.4)
        assert res.remaining_qty == pytest.approx(0.6)
        assert res.requested_qty == pytest.approx(1.0)


# ══════════════════════════════════════════════════════════════
# TEST J: Restart after partial fill -> no duplicate execution
# ══════════════════════════════════════════════════════════
class TestJ_RestartAfterPartialFill:
    def test_restart_after_partial_fill_no_duplicate(self):
        db_path = tempfile.mktemp(suffix=".db")
        p = PaperPersistence(db_path)
        p.connect()

        order_id = "entry-test-123"
        p.save_order({
            "order_id": order_id,
            "client_order_id": order_id,
            "symbol": "BTC-USDT",
            "side": "buy",
            "requested_qty": 1.0,
            "filled_qty": 0.4,
            "remaining_qty": 0.6,
            "avg_fill_price": 50000,
            "status": "PARTIALLY_FILLED_CANCELED",
        })
        p.save_fill({
            "fill_id": f"{order_id}-fill",
            "order_id": order_id,
            "symbol": "BTC-USDT",
            "side": "buy",
            "quantity": 0.4,
            "price": 50000,
            "notional": 20000,
            "fees": 20,
            "slippage_bps": 2,
        })
        p.close()

        # Restart instance B
        p2 = PaperPersistence(db_path)
        p2.connect()

        # Check open orders: terminal orders (including PARTIALLY_FILLED_CANCELED) must not be returned
        open_orders = p2.load_open_orders()
        assert len(open_orders) == 0
        assert p2.order_id_exists(order_id)
        assert p2.fill_id_exists(f"{order_id}-fill")

        p2.close()
        os.unlink(db_path)


# ══════════════════════════════════════════════════════════════
# TEST K: Closed position with trailing state -> trail removed
# ══════════════════════════════════════════════════════════
class TestK_ClosedPositionTrailRemoved:
    def test_closed_position_trail_removed(self):
        db_path = tempfile.mktemp(suffix=".db")
        p = PaperPersistence(db_path)
        p.connect()

        pid = "pos-BTC-USDT"
        p.save_position({
            "position_id": pid, "symbol": "BTC-USDT", "direction": "long",
            "quantity": 0.1, "entry_price": 50000, "entry_notional": 5000,
            "cost_basis": 5005, "entry_fee": 5.0, "stop_loss_price": 49850,
        })
        p.save_trail(pid, {"trail_peak": 50500, "trail_level": 50399, "trail_activated": True})
        assert p.load_trail(pid) is not None

        # Delete position on close
        p.delete_position(pid)

        # Both position and trail must be removed
        assert len(p.load_open_positions()) == 0
        assert p.load_trail(pid) is None
        assert p.count_orphan_trails() == 0

        p.close()
        os.unlink(db_path)


# ══════════════════════════════════════════════════════════════
# TEST L: Restart with orphaned trail -> safely reconciled
# ══════════════════════════════════════════════════════════
class TestL_RestartOrphanTrailReconciled:
    def test_restart_orphan_trail_reconciled(self):
        db_path = tempfile.mktemp(suffix=".db")
        p = PaperPersistence(db_path)
        p.connect()

        # Directly insert an orphaned trail (e.g. from a past ungraceful crash)
        p.save_trail("pos-ORPHAN-USDT", {"trail_peak": 100, "trail_level": 99.8, "trail_activated": True})
        assert p.count_orphan_trails() == 1

        # Run cleanup
        cleaned = p.cleanup_orphan_trails()
        assert cleaned == 1
        assert p.count_orphan_trails() == 0

        p.close()
        os.unlink(db_path)


# ══════════════════════════════════════════════════════════════
# TEST M: Symbol daily loss limit -> new symbol entries blocked
# ══════════════════════════════════════════════════════════
class TestM_SymbolDailyLossLimit:
    def test_symbol_daily_loss_limit_blocks_new_entries(self):
        mgr = SymbolRiskManager(SymbolRiskConfig(max_symbol_daily_loss_pct=1.5, symbol_extended_cooldown_seconds=300.0))
        now = datetime.now(UTC)

        # Equity $10,000 -> 1.5% is $150
        mgr.record_trade_exit("LOSER-USDT", net_pnl=-160.0, exit_reason="trail_hit", exit_time=now)
        # Even after cooldown window expires (400s > 300s), daily loss limit must continuously block new entries
        eligible, reason = mgr.is_symbol_eligible("LOSER-USDT", 10000.0, now=now + timedelta(seconds=400))
        assert not eligible
        assert "DAILY_LOSS_LIMIT_EXCEEDED" in reason


# ══════════════════════════════════════════════════════════════
# TEST N: Portfolio breaker -> new entries blocked while engine remains healthy
# ══════════════════════════════════════════════════════════
class TestN_PortfolioCircuitBreaker:
    def test_circuit_breaker_blocks_entries(self):
        risk = RiskEngine()
        risk.trip_circuit_breaker("drawdown_exceeded")
        assert risk.state.circuit_breaker_tripped

        from src.opportunity.engine import EvaluatedOpportunity
        from src.strategies.base import SignalDirection, StrategySignal

        sig = StrategySignal(
            strategy_id="test", symbol="BTC-USDT", direction=SignalDirection.LONG,
            confidence=0.9, estimated_return=0.02,
        )
        opp = EvaluatedOpportunity(signal=sig)
        assessment = risk.assess(opp)
        assert assessment.decision.value == "rejected"
        assert assessment.reason.value == "circuit_breaker"


# ══════════════════════════════════════════════════════════════
# TEST O: Very liquid BTC/ETH-style book -> ordinary trades not incorrectly rejected
# ══════════════════════════════════════════════════════════
class TestO_LiquidBTCEquityBookPassed:
    def test_liquid_btc_book_passed(self):
        gate = LiquidityGate(LiquidityGateConfig())
        estimator = ExecutionEstimator()

        # BTC-like book: tight 1 bps spread, deep liquidity ($10M+ depth)
        mid = 60000.0
        bids = [(mid - i * 1.0, 5.0) for i in range(20)]  # 100 BTC depth = $6M
        asks = [(mid + 1.0 + i * 1.0, 5.0) for i in range(20)]
        book = _create_test_book(bids=bids, asks=asks, symbol="BTC-USDT")

        # 1. Liquidity gate evaluation
        res = gate.assess_market("BTC-USDT", book, volume_24h=500_000_000, data_age_seconds=1.0)
        assert res.passed
        assert res.market_quality_score > 0.70

        # 2. Sizing and simulation for normal $1,000 order (0.0166 BTC)
        safe_qty = estimator.compute_max_safe_quantity("BTC-USDT", book, risk_qty=0.0166, capital_qty=0.0166)
        assert safe_qty == pytest.approx(0.0166, rel=1e-3)

        entry_sim = estimator.simulate_buy_entry("BTC-USDT", book, safe_qty)
        assert entry_sim.passed
        assert entry_sim.expected_slippage_bps < 5.0

        exit_sim = estimator.simulate_sell_exit("BTC-USDT", book, safe_qty)
        assert exit_sim.passed
        assert exit_sim.effective_stop_loss_pct < 0.35


# ══════════════════════════════════════════════════════════════
# ADDITIONAL EDGE CASE TESTS
# ══════════════════════════════════════════════════════════
class TestAdditionalEdgeCases:
    def test_market_quality_score_calculation(self):
        comp = MarketQualityCalculator.compute(
            spread_bps=5.0,
            depth_usd_10bps=25000.0,
            volume_24h_usd=10_000_000.0,
            data_age_seconds=2.0,
            expected_slippage_bps=3.0,
        )
        assert 0.0 <= comp.total_score <= 1.0
        assert comp.spread_score > 0.8
        assert comp.freshness_score > 0.9

    def test_strategy_risk_cooldown(self):
        mgr = StrategyRiskManager(StrategyRiskConfig(max_strategy_consecutive_losses=3))
        now = datetime.now(UTC)

        for _i in range(3):
            mgr.record_trade_exit("momentum_v1", -10.0, -10.0, 0.1, 0.0, "hard_stop", exit_time=now)

        eligible, reason = mgr.is_strategy_eligible("momentum_v1", 10000.0, now=now + timedelta(seconds=10))
        assert not eligible
        assert "STRATEGY_COOLDOWN_ACTIVE" in reason

    def test_accounting_exact_conservation(self):
        acct = PaperAccount(initial_balance=10000.0)
        p1 = acct.open_position("BTC-USDT", "long", 50000.0, 0.05, fees=2.5, stop_loss_price=49850.0)
        assert p1 is not None

        trade = acct.close_position("BTC-USDT", 50200.0, fees=2.51, slippage=1.0, exit_reason="trail_hit")
        assert trade is not None

        # Cash == initial + net PnL exactly
        assert acct.state.cash == pytest.approx(10000.0 + trade.net_pnl, rel=1e-5)
        assert len(acct.state.open_positions) == 0
        assert acct.state.allocated == 0.0
