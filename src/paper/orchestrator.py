"""Paper Trading Orchestrator — Complete Multi-Stage Execution & Risk Pipeline.

Implements:
SIGNAL
→ MARKET QUALITY
→ LIQUIDITY GATE
→ EXECUTION FEASIBILITY
→ RISK (Hierarchical: Portfolio, Strategy, Symbol)
→ POSITION SIZE (Execution-aware)
→ ORDER
→ FILL
→ POSITION
→ EXIT
→ RECONCILIATION
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast


def _get_memory_mb() -> float:
    """Get process RSS memory in MB. Cross-platform: psutil, resource, or 0."""
    try:
        import psutil
        return float(psutil.Process().memory_info().rss / (1024 * 1024))
    except Exception:
        try:
            import resource
            return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0)
        except Exception:
            return 0.0


from src.analytics.tracker import AnalyticsTracker
from src.core.config import get_settings
from src.core.logging_config import get_logger
from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import BookLevel, CanonicalSymbol, TickerEvent
from src.data.order_book import OrderBookEngine
from src.db.persist import PaperPersistence
from src.execution.estimator import ExecutionEstimator
from src.execution.liquidity_gate import (
    LiquidityGate,
    LiquidityGateConfig,
    LiquidityRejectionReason,
)
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import CLOSED_TRADE_RAM_LIMIT, ClosedTrade, PaperAccount, PaperPosition
from src.paper.engine import PaperExecutionEngine
from src.paper.position_monitor import PositionMonitor
from src.paper.reporting import exit_analysis, finite_report_value, grouped_trade_metrics, trade_metrics
from src.paper.telemetry import SignalFunnelTelemetry
from src.portfolio.allocator import AllocatorConfig, CapitalAllocator, PortfolioState
from src.portfolio.capacity import PositionCapacity
from src.portfolio.capital_tiers import CapitalTierConfig, CapitalTierManager
from src.portfolio.liquidity import LiquidityAnalyzer
from src.portfolio.markets import AssetQualityFilter, QualityFilterConfig
from src.portfolio.universe import UniverseConfig, UniverseManager
from src.risk.engine import RiskEngine
from src.risk.entry_guard import EntrySignalGuard
from src.risk.entry_quality import EntryQualityGate
from src.risk.strategy_risk import StrategyRiskConfig, StrategyRiskManager
from src.risk.symbol_risk import ReentryContext, SymbolRiskConfig, SymbolRiskManager
from src.scanner.global_scanner import AssetClass, AssetSnapshot, GlobalScanner
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.registry import StrategyRegistry
from src.strategies.trailing_stop import TrailConfig, compute_volatility_aware_trail

logger = get_logger(__name__)

LEASE_HEARTBEAT_SEC = 15.0
LEASE_EXPIRY_SEC = 45.0


def _parse_iso_or_now(s: str) -> datetime:
    """Parse ISO timestamp string, fall back to now if unparseable."""
    if not s:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return datetime.now(UTC)


class _RuntimeLease:
    def __init__(self, persist: PaperPersistence, account_id: str, owner_id: str) -> None:
        self._persist = persist
        self._account_id = account_id
        self._owner_id = owner_id
        self._acquired = False

    def try_acquire(self) -> bool:
        if self._persist._conn is None:
            return False
        with self._persist._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat()
            row = c.execute(
                "SELECT owner_id, expires_at FROM runtime_lease WHERE account_id=?",
                (self._account_id,),
            ).fetchone()
            if row is not None and row["owner_id"] != self._owner_id and row["expires_at"] > now:
                c.execute("ROLLBACK")
                return False
            new_expiry = datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + LEASE_EXPIRY_SEC, tz=UTC
            ).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO runtime_lease(account_id,owner_id,acquired_at,heartbeat_at,expires_at) VALUES(?,?,?,?,?)",
                (self._account_id, self._owner_id, now, now, new_expiry),
            )
            c.execute("COMMIT")
        self._acquired = True
        return True

    def heartbeat(self) -> bool:
        with self._persist._tx() as c:
            now = datetime.now(UTC).isoformat()
            row = c.execute(
                "SELECT owner_id FROM runtime_lease WHERE account_id=?", (self._account_id,)
            ).fetchone()
            if row is None or row["owner_id"] != self._owner_id:
                return False
            new_expiry = datetime.fromtimestamp(
                datetime.now(UTC).timestamp() + LEASE_EXPIRY_SEC, tz=UTC
            ).isoformat()
            c.execute(
                "UPDATE runtime_lease SET heartbeat_at=?, expires_at=? WHERE account_id=?",
                (now, new_expiry, self._account_id),
            )
        return True

    def release(self) -> None:
        with self._persist._tx() as c:
            c.execute(
                "DELETE FROM runtime_lease WHERE account_id=? AND owner_id=?",
                (self._account_id, self._owner_id),
            )
        self._acquired = False


class PaperTradingOrchestrator:
    """Multi-market, execution-aware paper trading orchestrator."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
        db_path: str = "data/paper_trading.db",
        activity_test: bool = False,
    ) -> None:
        raw_symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        self._raw_to_canonical: dict[str, str] = {}
        self._canonical_symbols: list[str] = []
        for raw in raw_symbols:
            canonical = CanonicalSymbol.from_exchange_symbol("binance", raw).symbol
            self._canonical_symbols.append(canonical)
            self._raw_to_canonical[raw.upper()] = canonical
        self.initial_balance = initial_balance
        self.max_symbols = max_symbols
        self._db_path = db_path
        self._activity_test = activity_test
        self._last_test_signal_time: float = 0.0

        self.event_bus = EventBus(default_max_queue=500)
        book_cap = max(200, len(raw_symbols) + 100)
        feat_cap = max(500, len(raw_symbols) + 200)
        self.order_book_engine = OrderBookEngine(max_books=book_cap)
        self.feed_health = FeedHealthMonitor()
        self.features = FeatureEngine(max_instruments=feat_cap)
        self.universe = UniverseManager(UniverseConfig())
        self.quality_filter = AssetQualityFilter(QualityFilterConfig())
        self.scanner = GlobalScanner()
        self.registry = StrategyRegistry()
        self._paper_config = get_settings().paper
        self.opportunity_engine = OpportunityEngine(
            min_net_return=self._paper_config.min_expected_edge_over_cost
        )
        self.risk_engine = RiskEngine()
        self.tier_manager = CapitalTierManager(CapitalTierConfig())
        self.allocator = CapitalAllocator(AllocatorConfig())
        self.liquidity = LiquidityAnalyzer()
        self.account = PaperAccount(initial_balance=initial_balance)
        self.paper_exec = PaperExecutionEngine(
            maker_fee=self._paper_config.maker_fee,
            taker_fee=self._paper_config.taker_fee,
            slippage_bps=self._paper_config.slippage_bps,
            simulated_latency_ms=self._paper_config.simulated_latency_ms,
        )
        trail_config = TrailConfig(
            trail_pct=self._paper_config.trail_distance_pct,
            activation_pct=self._paper_config.trail_activation_pct,
            trailing_delta=self._paper_config.trail_distance_pct / 100.0,
            enable_fixed_take_profit=False,
        )
        self.monitor = PositionMonitor(self.account, trail_config=trail_config)
        self.analytics = AnalyticsTracker()
        self.signal_guard = EntrySignalGuard()
        self.entry_quality = EntryQualityGate()
        self.funnel = SignalFunnelTelemetry()

        # Engineering Defect Fix Components
        self.liquidity_gate = LiquidityGate(LiquidityGateConfig())
        self.execution_estimator = ExecutionEstimator(
            max_entry_slippage_bps=25.0,
            max_exit_slippage_bps=35.0,
            max_levels_consumed=8,
            max_depth_participation_pct=0.10,
            max_effective_stop_loss_pct=0.80,
        )
        self.symbol_risk = SymbolRiskManager(
            SymbolRiskConfig(
                loss_cooldown_seconds=self._paper_config.loss_cooldown_seconds,
                win_cooldown_seconds=self._paper_config.win_cooldown_seconds,
                max_consecutive_losses_per_symbol=(
                    self._paper_config.max_consecutive_losses_per_symbol
                ),
                symbol_lockout_seconds=self._paper_config.symbol_lockout_seconds,
                loss_streak_reset_seconds=self._paper_config.symbol_loss_streak_reset_seconds,
                material_confidence_improvement=(
                    self._paper_config.material_reentry_confidence_improvement
                ),
                base_market_structure_score=(
                    self._paper_config.min_reentry_market_structure_score
                ),
                reference_volatility_pct=(
                    self._paper_config.reentry_reference_volatility_pct
                ),
            )
        )
        self.strategy_risk = StrategyRiskManager(StrategyRiskConfig())

        self._persist: PaperPersistence | None = None
        self._lease: _RuntimeLease | None = None

        self.publish_count = 0
        self.consume_count = 0
        self.subscriber_count = 0
        self._accepting_new = False
        self._health_recovery_counter: dict[str, int] = {}

        self._running = False
        self._scan_interval = 5.0
        self._report_interval = 60.0
        self._start_time = 0.0
        self._wall_start: datetime | None = None
        self._total_scans = 0
        self._total_signals = 0
        self._total_trades = 0
        self._total_opportunities = 0
        self._risk_assessments = 0
        self._risk_approved = 0
        self._risk_rejected = 0
        self._orders_created = 0
        self._fills_created = 0
        self._partial_fills = 0
        self._partial_fills_canceled = 0
        self._positions_opened_total = 0
        self._positions_closed = 0
        self._trailing_exits = 0
        self._hard_stop_exits = 0
        self._lease_heartbeat_success = 0
        self._lease_heartbeat_errors = 0
        self._persistence_writes = 0
        self._persistence_reads = 0
        self._persistence_errors = 0
        self._exceptions = 0

        # Observability metrics
        self._liquidity_checks = 0
        self._liquidity_rejections = 0
        self._spread_rejections = 0
        self._entry_slippage_rejections = 0
        self._exit_slippage_rejections = 0
        self._participation_rejections = 0
        self._stale_market_rejections = 0
        self._rejected_entries = 0
        self._reentry_rejections = 0
        self._symbol_cooldown_rejections = 0
        self._duplicate_signal_rejections = 0
        self._expected_edge_rejections = 0
        self._slippage_bps_list: list[float] = []

        self._rss_start_mb: float = 0.0
        self._rss_peak_mb: float = 0.0
        self._task_count_start: int = 0
        self._task_count_peak: int = 0
        self._queue_depth_peak: int = 0
        self._db_bytes_start: int = 0
        self._trade_log: deque[dict[str, Any]] = deque(maxlen=50000)
        self._error_log: deque[dict[str, Any]] = deque(maxlen=1000)

        # Closed-trade dedup set
        self._persisted_trade_ids: set[str] = set()

        # Runtime safety flags
        self._fatal_error: str | None = None
        self._stale_feed_violation: bool = False

        # Strategy observability counters
        self._strategy_evaluations: int = 0
        self._strategy_evaluations_by_strategy: dict[str, int] = {}
        self._strategy_evaluations_by_symbol: dict[str, int] = {}
        self._no_signal_decisions: int = 0
        self._no_signal_reasons: dict[str, int] = {}
        self._market_events_received: int = 0
        self._ticker_events_received: int = 0
        self._book_events_received: int = 0

        try:
            signal.signal(signal.SIGTERM, lambda s, f: self._handle_signal())
            signal.signal(signal.SIGINT, lambda s, f: self._handle_signal())
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info("paper_start", balance=self.initial_balance)
        self._wall_start = datetime.now(UTC)
        self._sample_resources_start()

        self._persist = PaperPersistence(self._db_path)
        self._persist.connect()
        self._persist._ensure_lease_table()
        self._db_bytes_start = os.path.getsize(self._db_path) if os.path.exists(self._db_path) else 0

        session_id = self._get_experiment_id()
        owner_id = f"{session_id}-{uuid.uuid4().hex[:8]}"
        self._lease = _RuntimeLease(self._persist, "paper-account-1", owner_id)
        if not self._lease.try_acquire():
            logger.error("lease_acquire_failed")
            self._persist.close()
            return {"status": "LEASE_CONFLICT"}

        self._persist.start_session(session_id, self._get_commit_sha())
        self._restore_state()

        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()

        await self.event_bus.start()
        self.event_bus.subscribe("ticker_events", self._sub_ticker)
        self.subscriber_count = 1

        for canonical in self._canonical_symbols:
            a = self.universe.register(canonical, "binance")
            a.data_healthy = True
            a.last_data_at = datetime.now(UTC)

        self._accepting_new = True
        self._running = True
        self._start_time = time.monotonic()

        tasks = [
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._report_loop()),
            asyncio.create_task(self._health_supervisor()),
            asyncio.create_task(self._lease_heartbeat_loop()),
        ]
        end = time.monotonic() + duration_seconds if duration_seconds > 0 else float("inf")
        try:
            while self._running and time.monotonic() < end:
                self._sample_resource_peak()
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self._accepting_new = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.event_bus.shutdown()
            await self.registry.shutdown_all()
            self._persist_final_state()
            if self._lease:
                self._lease.release()
            self._persist.close()
        return self._final_report()

    # ══════════════════════════════════════════════════════════════════
    def _sample_resources_start(self) -> None:
        self._rss_start_mb = _get_memory_mb()
        self._task_count_start = len(asyncio.all_tasks())

    def _sample_resource_peak(self) -> None:
        try:
            mb = _get_memory_mb()
            if mb > self._rss_peak_mb:
                self._rss_peak_mb = mb
            tc = len(asyncio.all_tasks())
            if tc > self._task_count_peak:
                self._task_count_peak = tc
            for name in self.event_bus._consumers:
                qsize = self.event_bus._consumers[name].qsize() if name in self.event_bus._consumers else 0
                if qsize > self._queue_depth_peak:
                    self._queue_depth_peak = qsize
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    def _restore_state(self) -> None:
        if self._persist is None:
            return

        # Clean up any orphaned trail records on startup
        self._persist.cleanup_orphan_trails()

        saved = self._persist.load_account()
        if saved is not None:
            s = self.account.state
            s.initial_balance = saved.get("initial_balance", self.initial_balance)
            self.initial_balance = s.initial_balance
            s.cash = saved.get("cash", self.initial_balance)
            s.allocated = saved.get("allocated", 0)
            if abs(s.allocated) < 1e-9:
                s.allocated = 0.0
            s.unrealized_pnl = saved.get("unrealized_pnl", 0)
            s.realized_pnl = saved.get("realized_pnl", 0)
            s.total_fees = saved.get("total_fees", 0)
            s.total_slippage = saved.get("total_slippage", 0)
            s.trade_count = saved.get("trade_count", 0)
            s.win_count = saved.get("win_count", 0)
            s.loss_count = saved.get("loss_count", 0)
            s.peak_equity = saved.get("peak_equity", self.initial_balance)
            s.max_drawdown_pct = saved.get("max_drawdown_pct", 0)

        # Restore closed trades
        db_trades = self._persist.load_recent_closed_trades(200)
        for t_dict in db_trades:
            entry_time = _parse_iso_or_now(t_dict.get("entry_time", ""))
            exit_time = _parse_iso_or_now(t_dict.get("exit_time", ""))
            ct = ClosedTrade(
                symbol=t_dict["symbol"], direction=t_dict.get("direction", "long"),
                entry_price=t_dict["entry_price"], exit_price=t_dict["exit_price"],
                quantity=t_dict["quantity"], gross_pnl=t_dict.get("gross_pnl", 0),
                fees=t_dict.get("fees", 0),
                entry_fee=t_dict.get("entry_fee", 0),
                exit_fee=t_dict.get("exit_fee", 0),
                slippage_cost=t_dict.get("slippage_cost", 0),
                net_pnl=t_dict.get("net_pnl", 0), return_pct=t_dict.get("return_pct", 0),
                exit_reason=t_dict.get("exit_reason", ""),
                strategy_id=t_dict.get("strategy_id", ""),
                entry_time=entry_time,
                exit_time=exit_time,
                holding_seconds=t_dict.get(
                    "holding_seconds", max(0.0, (exit_time - entry_time).total_seconds())
                ),
                signal_id=t_dict.get("signal_id", ""),
                signal_timestamp=(
                    _parse_iso_or_now(t_dict["signal_timestamp"])
                    if t_dict.get("signal_timestamp") else None
                ),
                entry_confidence=t_dict.get("entry_confidence"),
                max_favorable_excursion_pct=t_dict.get("mfe_pct", 0),
                max_adverse_excursion_pct=t_dict.get("mae_pct", 0),
                trade_id=t_dict.get("trade_id", ""),
            )
            self.account.state.closed_trades.append(ct)
            if ct.trade_id:
                self._persisted_trade_ids.add(ct.trade_id)

        positions = self._persist.load_open_positions()
        for p_dict in positions:
            try:
                position_metadata = json.loads(p_dict.get("metadata_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                position_metadata = {}
            pos = PaperPosition(
                symbol=p_dict["symbol"], direction=p_dict.get("direction", "long"),
                entry_price=p_dict["entry_price"],
                entry_reference_price=p_dict.get("entry_reference_price", p_dict["entry_price"]),
                quantity=p_dict["quantity"],
                notional=p_dict.get("entry_notional", p_dict["entry_price"] * p_dict["quantity"]),
                stop_loss_price=p_dict.get("stop_loss_price", 0),
                fees_paid=p_dict.get("entry_fee", 0),
                entry_slippage_cost=p_dict.get("entry_slippage_cost", 0),
                trail_activation_pct=p_dict.get("trail_activation_pct", 0),
                entry_time=_parse_iso_or_now(p_dict.get("opened_at", "")),
                current_price=p_dict.get("current_price", 0),
                unrealized_pnl=p_dict.get("unrealized_pnl", 0),
                strategy_id=p_dict.get("strategy_id", ""),
                signal_id=p_dict.get("signal_id", ""),
                signal_timestamp=(
                    _parse_iso_or_now(p_dict["signal_timestamp"])
                    if p_dict.get("signal_timestamp") else None
                ),
                entry_confidence=p_dict.get("entry_confidence"),
                max_favorable_excursion_pct=p_dict.get("mfe_pct", 0),
                max_adverse_excursion_pct=p_dict.get("mae_pct", 0),
                metadata=position_metadata,
            )
            self.account.state.open_positions[p_dict["symbol"]] = pos
            self.monitor.register_position(pos)
            trail = self._persist.load_trail(p_dict["position_id"])
            if trail:
                self.monitor.restore_trail_state({
                    "symbol": p_dict["symbol"], "direction": p_dict.get("direction", "long"),
                    "entry_price": p_dict["entry_price"],
                    "peak_price": trail.get("trail_peak", p_dict["entry_price"]),
                    "trail_level": trail.get("trail_level", 0),
                    "activation_price": trail.get("activation_price", 0),
                    "activation_pct": trail.get(
                        "activation_pct", pos.trail_activation_pct
                    ),
                    "trail_distance_pct": trail.get(
                        "trail_distance_pct",
                        pos.metadata.get("effective_trail_distance_pct", 0.0),
                    ),
                    "activated": bool(trail.get("trail_activated")),
                    "exit_intent": bool(trail.get("exit_intent_active")),
                })
                pos.trail_peak = trail.get("trail_peak", p_dict["entry_price"])
                pos.trail_activated = bool(trail.get("trail_activated"))
                pos.trail_level = trail.get("trail_level", 0)

        risk = self._persist.load_risk()
        if risk:
            self.risk_engine.restore_state(
                total_exposure=risk.get("total_exposure", 0),
                per_market=risk.get("per_market", {}),
                per_strategy=risk.get("per_strategy", {}),
                strat_counts=risk.get("strat_counts", {}),
                peak_equity=risk.get("peak_equity", 0),
                consecutive_losses=risk.get("consecutive_losses", 0),
                breaker_active=bool(risk.get("breaker_active")),
            )

        symbol_state = self._persist.load_symbol_risk_state()
        if symbol_state:
            self.symbol_risk.restore_state(symbol_state)
        signal_state = self._persist.load_signal_state()
        if signal_state:
            self.signal_guard.restore_state(signal_state)
        strategy_state = self._persist.load_strategy_risk_state()
        if strategy_state:
            self.strategy_risk.restore_state(strategy_state)
        funnel_state = self._persist.load_telemetry_state("signal_funnel")
        if funnel_state:
            self.funnel.restore(funnel_state)

        runtime_metrics = self._persist.load_runtime_metrics()
        # Old databases predate the durable funnel JSON.  Seed the stable
        # counters from their legacy metrics once rather than discarding prior
        # paper telemetry on upgrade.
        if not funnel_state:
            self.funnel.increment("raw_signals", int(runtime_metrics.get("signals_generated", 0)))
            self.funnel.increment(
                "opportunities_created", int(runtime_metrics.get("opportunities_evaluated", 0))
            )
            self.funnel.increment(
                "expected_edge_rejections", int(runtime_metrics.get("expected_edge_rejections", 0))
            )
            self.funnel.increment(
                "cooldown_rejections", int(runtime_metrics.get("cooldown_rejections", 0))
            )
            self.funnel.increment(
                "stale_market_rejections", int(runtime_metrics.get("stale_market_rejections", 0))
            )
            self.funnel.increment("risk_rejections", int(runtime_metrics.get("risk_rejections", 0)))
            self.funnel.entry_rejections = int(runtime_metrics.get("rejected_entries", 0))

        self._rejected_entries = self.funnel.entry_rejections
        self._symbol_cooldown_rejections = self.funnel.counters["cooldown_rejections"]
        self._duplicate_signal_rejections = int(
            runtime_metrics.get("duplicate_signal_rejections", 0)
        )
        self._expected_edge_rejections = self.funnel.counters["expected_edge_rejections"]
        self._reentry_rejections = self.funnel.counters["reentry_rejections"]
        self.symbol_risk.consecutive_loss_events_count = int(
            runtime_metrics.get("consecutive_loss_events", 0)
        )
        self._exceptions = int(runtime_metrics.get("exceptions", self._exceptions))
        self._persistence_errors = int(
            runtime_metrics.get("persistence_errors", self._persistence_errors)
        )
        self._stale_feed_violation = bool(
            runtime_metrics.get("stale_feed_violation", self._stale_feed_violation)
        )
        self._stale_market_rejections = self.funnel.counters["stale_market_rejections"]
        self._risk_rejected = self.funnel.counters["risk_rejections"]
        self._total_signals = self.funnel.counters["raw_signals"]
        self._total_opportunities = self.funnel.counters["opportunities_created"]

        self.account.state.unrealized_pnl = sum(
            p.unrealized_pnl for p in self.account.state.open_positions.values()
        )
        self.account.assert_invariants()

    # ══════════════════════════════════════════════════════════════════
    async def _health_supervisor(self) -> None:
        stabilization_ticks = 3
        while self._running:
            await asyncio.sleep(5.0)
            unhealthy = self.feed_health.check_all()
            all_feeds = self.feed_health.get_all()
            if all_feeds and all(not f.is_healthy for f in all_feeds):
                if self._accepting_new:
                    self._stale_feed_violation = True
                self._accepting_new = False
                self._health_recovery_counter.clear()
            elif unhealthy:
                ticker_unhealthy = [fh for fh in unhealthy if fh.stream_type == "ticker" and fh.messages_received > 0]
                if ticker_unhealthy:
                    if self._accepting_new:
                        self._stale_feed_violation = True
                    for fh in ticker_unhealthy[:5]:
                        logger.warning("feed_unhealthy", symbol=fh.symbol, stream=fh.stream_type)
            else:
                if not self._accepting_new and self._lease and self._running:
                    key = "global"
                    self._health_recovery_counter[key] = self._health_recovery_counter.get(key, 0) + 1
                    if self._health_recovery_counter[key] >= stabilization_ticks:
                        self._accepting_new = True
                        self._health_recovery_counter.clear()

    async def _lease_heartbeat_loop(self) -> None:
        while self._running and self._lease:
            await asyncio.sleep(LEASE_HEARTBEAT_SEC)
            if self._lease and not self._lease.heartbeat():
                self._lease_heartbeat_errors += 1
                self._accepting_new = False
            else:
                self._lease_heartbeat_success += 1

    # ══════════════════════════════════════════════════════════════════
    def _reconcile_risk(self) -> None:
        """Reconcile risk state from current account positions immediately."""
        exp = self.account.state.allocated
        strat_counts: dict[str, int] = {}
        per_strat: dict[str, float] = {}
        for pos in self.account.state.open_positions.values():
            sid = pos.strategy_id or "unknown"
            strat_counts[sid] = strat_counts.get(sid, 0) + 1
            per_strat[sid] = per_strat.get(sid, 0) + pos.notional
        self.risk_engine.update_state(
            total_exposure=exp,
            current_equity=self.account.state.equity,
            open_positions_count=len(self.account.state.open_positions),
            per_market_exposure={"crypto": exp},
            per_strategy_exposure=per_strat,
            strategy_position_counts=strat_counts,
        )
        self._persist_risk_data(exp, per_strat, strat_counts)

    def _persist_risk_data(
        self, exp: float, per_strat: dict, strat_counts: dict
    ) -> None:
        if self._persist is None:
            return
        try:
            rs = self.risk_engine.get_state()
            self._persist.save_risk({
                "total_exposure": exp,
                "per_market_exposure": {"crypto": exp},
                "per_strategy_exposure": per_strat,
                "strategy_position_counts": strat_counts,
                "peak_equity": max(rs.get("peak_equity", 0), self.account.state.peak_equity),
                "consecutive_losses": rs.get("consecutive_losses", 0),
                "circuit_breaker_active": rs.get("circuit_breaker_active", False),
            })
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_account(self) -> None:
        if self._persist is None:
            return
        try:
            self.account.assert_invariants()
            self._persist.save_account({
                "cash": self.account.state.cash,
                "initial_balance": self.account.state.initial_balance,
                "allocated": self.account.state.allocated,
                "unrealized_pnl": self.account.state.unrealized_pnl,
                "equity": self.account.state.equity,
                "realized_pnl": self.account.state.realized_pnl,
                "total_fees": self.account.state.total_fees,
                "total_slippage": self.account.state.total_slippage,
                "trade_count": self.account.state.trade_count,
                "win_count": self.account.state.win_count,
                "loss_count": self.account.state.loss_count,
                "peak_equity": self.account.state.peak_equity,
                "max_drawdown_pct": self.account.state.max_drawdown_pct,
            })
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_position(self, pos_id: str, pos: Any) -> None:
        if self._persist is None:
            return
        try:
            self._persist.save_position({
                "position_id": pos_id, "symbol": pos.symbol, "direction": pos.direction,
                "quantity": pos.quantity, "entry_price": pos.entry_price,
                "entry_reference_price": pos.entry_reference_price,
                "entry_notional": pos.notional, "cost_basis": pos.notional + pos.fees_paid,
                "entry_fee": pos.fees_paid,
                "entry_slippage_cost": pos.entry_slippage_cost,
                "stop_loss_price": pos.stop_loss_price,
                "trail_activation_pct": pos.trail_activation_pct,
                "current_price": pos.current_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "strategy_id": pos.strategy_id,
                "signal_id": pos.signal_id,
                "signal_timestamp": (
                    pos.signal_timestamp.isoformat() if pos.signal_timestamp else None
                ),
                "entry_confidence": pos.entry_confidence,
                "mfe_pct": pos.max_favorable_excursion_pct,
                "mae_pct": pos.max_adverse_excursion_pct,
                "metadata": pos.metadata,
                "opened_at": pos.entry_time.isoformat(),
            })
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_trail(self, pos_id: str) -> None:
        if self._persist is None:
            return
        try:
            sym = self._get_symbol_by_pos_id(pos_id)
            if sym:
                ts = self.monitor.get_trail_state(sym)
                if ts:
                    self._persist.save_trail(pos_id, {
                        "trail_peak": ts["peak_price"], "trail_level": ts["trail_level"],
                        "activation_price": ts["activation_price"],
                        "activation_pct": ts["activation_pct"],
                        # Per-position distance is also in the durable position
                        # metadata; this field makes the trail snapshot itself
                        # inspectable for a running session.
                        "trail_distance_pct": ts.get("trail_distance_pct", 0.0),
                        "trail_activated": ts["activated"], "exit_intent_active": ts["exit_intent"],
                    })
                    self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_order(self, o: dict) -> None:
        if self._persist is None:
            return
        if self._persist.order_id_exists(o["order_id"]):
            return
        try:
            self._persist.save_order(o)
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_fill(self, f: dict) -> None:
        if self._persist is None:
            return
        if self._persist.fill_id_exists(f["fill_id"]):
            return
        try:
            self._persist.save_fill(f)
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_closed_trade(self, trade: ClosedTrade) -> None:
        if self._persist is None:
            return
        if trade.trade_id:
            trade_id = trade.trade_id
        else:
            trade_id = (
                f"ct-{trade.symbol}-{trade.exit_time.strftime('%Y%m%d%H%M%S%f')[:16]}"
                f"-{trade.entry_price:.2f}-{trade.exit_price:.2f}-{trade.quantity:.6f}"
            )
            trade.trade_id = trade_id
        if trade_id in self._persisted_trade_ids:
            return
        if self._persist.closed_trade_exists(trade_id):
            self._persisted_trade_ids.add(trade_id)
            return
        try:
            self._persist.save_closed_trade({
                "trade_id": trade_id, "symbol": trade.symbol, "direction": trade.direction,
                "entry_price": trade.entry_price, "exit_price": trade.exit_price,
                "quantity": trade.quantity, "gross_pnl": trade.gross_pnl,
                "fees": trade.fees,
                "entry_fee": trade.entry_fee,
                "exit_fee": trade.exit_fee,
                "slippage_cost": trade.slippage_cost,
                "net_pnl": trade.net_pnl, "return_pct": trade.return_pct,
                "exit_reason": trade.exit_reason, "strategy_id": trade.strategy_id,
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
                "holding_seconds": trade.holding_seconds,
                "signal_id": trade.signal_id,
                "signal_timestamp": (
                    trade.signal_timestamp.isoformat() if trade.signal_timestamp else None
                ),
                "entry_confidence": trade.entry_confidence,
                "mfe_pct": trade.max_favorable_excursion_pct,
                "mae_pct": trade.max_adverse_excursion_pct,
            })
            self._persisted_trade_ids.add(trade_id)
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

    def _persist_protection_state(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist.save_symbol_risk_state(self.symbol_risk.get_state())
            self._persist.save_signal_state(self.signal_guard.get_state())
            self._persist.save_strategy_risk_state(self.strategy_risk.get_state())
            self._persist.save_telemetry_state("signal_funnel", self.funnel.to_dict())
            funnel = self.funnel.funnel()
            self._persist.save_runtime_metrics({
                "rejected_entries": self.funnel.entry_rejections,
                "cooldown_rejections": funnel["cooldown_rejections"],
                "duplicate_signal_rejections": self._duplicate_signal_rejections,
                "expected_edge_rejections": funnel["expected_edge_rejections"],
                "reentry_attempts_prevented": (
                    funnel["cooldown_rejections"] + funnel["reentry_rejections"]
                ),
                "reentry_rejections": funnel["reentry_rejections"],
                "consecutive_loss_events": self.symbol_risk.consecutive_loss_events_count,
                "early_reentries_allowed": self.symbol_risk.early_reentries_allowed_count,
                "exceptions": self._exceptions,
                "persistence_errors": self._persistence_errors,
                "stale_feed_violation": int(self._stale_feed_violation),
                "stale_market_rejections": funnel["stale_market_rejections"],
                "risk_rejections": funnel["risk_rejections"],
                "signals_generated": funnel["raw_signals"],
                "opportunities_evaluated": funnel["opportunities_created"],
                "successful_entries": funnel["successful_entries"],
                "execution_attempts": funnel["execution_attempts"],
                **{f"funnel_{name}": value for name, value in funnel.items()},
            })
            self._persistence_writes += 5
        except Exception:
            self._persistence_errors += 1

    def _persist_runtime_state(self) -> None:
        """Persist a coherent inspectable snapshot while a soak is running."""
        self._persist_account()
        for symbol, pos in self.account.state.open_positions.items():
            pos_id = f"pos-{symbol}"
            self._persist_position(pos_id, pos)
            self._persist_trail(pos_id)
        self._persist_protection_state()

    def _persist_final_state(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist_account()
            for sym, pos in self.account.state.open_positions.items():
                pid = f"pos-{sym}"
                self._persist_position(pid, pos)
                self._persist_trail(pid)
            self._reconcile_risk()
            for trade in self.account.state.closed_trades:
                self._persist_closed_trade(trade)
            self._persist_protection_state()
            self._persist.cleanup_orphan_trails()
            self._persist.audit("GRACEFUL_SHUTDOWN", "Clean stop")
            self._persist.end_session(self._get_experiment_id(), "COMPLETED")
        except Exception:
            self._persistence_errors += 1

    def _get_symbol_by_pos_id(self, pos_id: str) -> str:
        for sym in self.account.state.open_positions:
            if f"pos-{sym}" == pos_id:
                return sym
        return ""

    # ══════════════════════════════════════════════════════════════════
    async def _sub_ticker(self, event: Any) -> None:
        self.consume_count += 1
        self._market_events_received += 1
        self._ticker_events_received += 1
        sym = str(getattr(event, "symbol", ""))
        last = float(getattr(event, "last", 0))
        bid = float(getattr(event, "bid", last * 0.9999))
        ask = float(getattr(event, "ask", last * 1.0001))
        vol = float(getattr(event, "volume_24h", 0))
        if last <= 0:
            return
        self.feed_health.record_message("binance", sym, "ticker", exchange_ts=datetime.now(UTC))
        self.features.update_price(sym, last)
        current_features = self.features.get(sym)
        self.features.update_order_book(
            sym,
            bid,
            ask,
            current_features.bid_depth_10bps,
            current_features.ask_depth_10bps,
        )
        self.features.update_volume(sym, vol)
        self.allocator.correlation.record_price(sym, last)
        self.account.update_market_price(sym, last)
        self.analytics.record_equity(self.account.state.equity)

    def process_ticker(
        self, raw_symbol: str, bid: float, ask: float, last: float, volume_24h: float = 0.0
    ) -> None:
        canonical = self._raw_to_canonical.get(raw_symbol.upper(), raw_symbol.upper())
        parts = canonical.split("-") if "-" in canonical else [canonical[:3], canonical[3:]]
        ticker_event = TickerEvent.create(
            "binance", CanonicalSymbol("binance", parts[0], parts[-1]),
            bid, ask, last, volume_24h=volume_24h,
        )
        with contextlib.suppress(RuntimeError):
            asyncio.ensure_future(self.event_bus.publish(ticker_event))
        self.publish_count += 1

    def process_order_book(
        self, raw_symbol: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]
    ) -> None:
        canonical = self._raw_to_canonical.get(raw_symbol.upper(), raw_symbol.upper())
        book = self.order_book_engine.get_or_create("binance", canonical)
        if bids:
            book.bids.apply_snapshot([BookLevel(p, q) for p, q in bids[:50]])
        if asks:
            book.asks.apply_snapshot([BookLevel(p, q) for p, q in asks[:50]])
        book.last_update_time = datetime.now(UTC)
        # Feed live visible-depth imbalance into the feature layer.  The
        # quality gate treats it as optional confirmation; missing/neutral
        # depth stays neutral rather than being fabricated as bullish flow.
        if book.best_bid > 0 and book.best_ask > 0:
            bid_depth = book.bids.depth_by_levels(5)
            ask_depth = book.asks.depth_by_levels(5)
            self.features.update_order_book(
                canonical, book.best_bid, book.best_ask, bid_depth, ask_depth
            )
        self._book_events_received += 1
        self.feed_health.record_message("binance", canonical, "book", exchange_ts=datetime.now(UTC))

    def _annotate_signal_cost(self, signal: Any) -> None:
        """Attach cost facts from the configured fill model; never invent edge."""
        symbol = signal.symbol or ""
        feat = self.features.get(symbol)
        book = self.order_book_engine.get_book("binance", symbol)
        entry_reference = (
            book.best_ask if book and book.best_ask > 0 else feat.ask
        )
        exit_reference = (
            book.best_bid if book and book.best_bid > 0 else feat.bid
        )
        if entry_reference <= 0 or exit_reference <= 0:
            return
        notional = float(signal.required_capital or min(500.0, max(50.0, self.account.state.cash * 0.05)))
        quantity = notional / entry_reference
        estimate = self.paper_exec.estimate_round_trip_cost(
            quantity, entry_reference, exit_reference
        )
        if estimate.notional <= 0:
            return
        signal.metadata.update({
            "taker_fee": self.paper_exec.taker_fee,
            "estimated_entry_fee": estimate.estimated_entry_fee,
            "estimated_exit_fee": estimate.estimated_exit_fee,
            "estimated_fee_fraction": (
                (estimate.estimated_entry_fee + estimate.estimated_exit_fee)
                / estimate.notional
            ),
            "estimated_spread_cost": estimate.estimated_spread_cost,
            "estimated_spread_cost_fraction": (
                estimate.estimated_spread_cost / estimate.notional
            ),
            "estimated_slippage": estimate.estimated_slippage,
            "estimated_slippage_fraction": (
                estimate.estimated_slippage / estimate.notional
            ),
            "estimated_round_trip_cost": estimate.estimated_round_trip_cost,
            "estimated_round_trip_cost_fraction": (
                estimate.estimated_round_trip_cost_fraction
            ),
        })

    def _record_pipeline_rejection(
        self,
        reason: str,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
        entry_attempt: bool = False,
    ) -> None:
        """Record every filtering path in one structured, non-silent ledger."""
        self.funnel.reject(
            reason,
            strategy_id=strategy_id,
            symbol=symbol,
            entry_attempt=entry_attempt,
        )
        if strategy_id:
            self.strategy_risk.record_rejection(strategy_id, reason)
        # Legacy scalar attributes remain for API/dashboard compatibility.
        self._rejected_entries = self.funnel.entry_rejections
        self._symbol_cooldown_rejections = self.funnel.counters["cooldown_rejections"]
        self._expected_edge_rejections = self.funnel.counters["expected_edge_rejections"]
        self._stale_market_rejections = self.funnel.counters["stale_market_rejections"]
        self._liquidity_rejections = self.funnel.counters["liquidity_rejections"]
        self._spread_rejections = self.funnel.counters["spread_rejections"]
        self._risk_rejected = self.funnel.counters["risk_rejections"]
        self._reentry_rejections = self.funnel.counters["reentry_rejections"]

    def _record_entry_rejection(
        self,
        reason: str,
        *,
        strategy_id: str | None = None,
        symbol: str | None = None,
    ) -> None:
        """Compatibility wrapper for a rejection after an opportunity exists."""
        mapped = {
            "duplicate_signal": "reentry",
            "strategy_risk": "risk",
            "missing_stop": "risk",
            "missing_book": "liquidity",
            "allocation": "capacity",
            "minimum_notional": "capacity",
            "participation": "liquidity",
            "entry_execution": "liquidity",
            "exit_execution": "liquidity",
            "entry_fill": "liquidity",
            "account": "capacity",
        }.get(reason, reason)
        if reason == "duplicate_signal":
            self._duplicate_signal_rejections += 1
        self._record_pipeline_rejection(
            mapped,
            strategy_id=strategy_id,
            symbol=symbol,
            entry_attempt=True,
        )

    def _record_inactive_signal(self, reason: str, strategy_id: str | None, symbol: str | None) -> None:
        self.funnel.increment("inactive_signals")
        self._record_pipeline_rejection(reason, strategy_id=strategy_id, symbol=symbol)

    def _valid_signal(self, signal: Any) -> tuple[bool, str]:
        if signal is None:
            return False, "inactive"
        if not getattr(signal, "symbol", None):
            return False, "invalid_signal"
        if getattr(signal, "direction", None) is None or signal.direction.value == "neutral":
            return False, "inactive"
        if signal.is_expired:
            return False, "stale_market"
        if signal.estimated_return is None:
            return False, "expected_edge"
        return True, "valid"

    # ══════════════════════════════════════════════════════════════════
    async def _scan_tick(self) -> None:
        # Exits remain active even when feed/risk health disables new entries.
        # Safety stops must never depend on the entry-acceptance flag.
        accepting_new = self._accepting_new

        # ── 1. CHECK STOPS / TRAILING EXITS (REALISTIC DEPTH WALK) ──
        exits = self.monitor.check_all()
        for ex in exits:
            sym = ex["symbol"]
            pos_data = self.account.state.open_positions.get(sym)
            if pos_data is None:
                continue
            qty = pos_data.quantity
            book = self.order_book_engine.get_book("binance", sym)
            bids_depth = [(lv[0], lv[1]) for lv in (book.bids.levels if book else [])] if book else None
            exit_bid = book.best_bid if book and book.best_bid > 0 else self._get_bid(sym)
            exit_ask = book.best_ask if book and book.best_ask > 0 else ex["price"]

            order_id = f"exit-{sym}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            fill = await self.paper_exec.simulate_fill(
                sym, "sell", qty,
                bid=exit_bid,
                ask=exit_ask,
                last=ex["price"],
                bids_depth=bids_depth,
            )
            if fill is None or fill.filled_qty <= 0:
                continue

            self._persist_order({
                "order_id": order_id, "client_order_id": order_id,
                "symbol": sym, "side": "sell", "requested_qty": qty,
                "filled_qty": fill.filled_qty, "remaining_qty": max(qty - fill.filled_qty, 0),
                "avg_fill_price": fill.fill_price, "status": fill.status,
            })
            self._orders_created += 1
            self._persist_fill({
                "fill_id": f"{order_id}-fill", "order_id": order_id,
                "symbol": sym, "side": "sell", "quantity": fill.filled_qty,
                "price": fill.fill_price, "notional": fill.fill_price * fill.filled_qty,
                "fees": fill.fees, "slippage_bps": fill.slippage_bps,
            })
            self._fills_created += 1
            if fill.status == "PARTIALLY_FILLED_CANCELED":
                self._partial_fills_canceled += 1
                self._partial_fills += 1
            self._slippage_bps_list.append(fill.slippage_bps)

            exit_embedded_slippage = max(
                0.0, (fill.vwap_price - fill.fill_price) * fill.filled_qty
            )
            if abs(fill.filled_qty - qty) < 0.00000001:
                trade = self.account.close_position(
                    sym, fill.fill_price, fees=fill.fees, slippage=0.0,
                    exit_reason=ex["reason"],
                    trail_peak=ex.get("trail_peak", 0.0),
                    trail_level=ex.get("trail_level", 0.0),
                    exit_reference_price=fill.vwap_price,
                    embedded_slippage_cost=exit_embedded_slippage,
                )
                self._positions_closed += 1
                if ex["reason"] == "trail_hit":
                    self._trailing_exits += 1
                elif ex["reason"] in ("hard_stop", "stop_loss"):
                    self._hard_stop_exits += 1
            else:
                trade = self.account.reduce_position(
                    sym, fill.fill_price, fill.filled_qty,
                    fees=fill.fees, slippage=0.0, exit_reason=ex["reason"],
                    exit_reference_price=fill.vwap_price,
                    embedded_slippage_cost=exit_embedded_slippage,
                )
                if sym in self.account.state.open_positions:
                    self.monitor.rearm_position(sym)

            if sym not in self.account.state.open_positions:
                self.monitor.unregister_position(sym)

            if trade:
                if sym in self.account.state.open_positions:
                    remaining_pos = self.account.state.open_positions[sym]
                    remaining_pos.metadata["partial_exit_net_pnl"] = (
                        float(remaining_pos.metadata.get("partial_exit_net_pnl", 0.0))
                        + trade.net_pnl
                    )
                    lifecycle_net_pnl = trade.net_pnl
                else:
                    lifecycle_net_pnl = (
                        float(pos_data.metadata.get("partial_exit_net_pnl", 0.0))
                        + trade.net_pnl
                    )
                self._total_trades += 1
                self.analytics.record_trade(trade.gross_pnl, trade.net_pnl, trade.fees,
                                            slippage=trade.slippage_cost,
                                            strategy_id=trade.strategy_id, exchange="binance")
                self._trade_log.append({"symbol": sym, "reason": ex["reason"],
                                        "pnl": trade.net_pnl,
                                        "time": datetime.now(UTC).isoformat()})
                self._persist_account()
                self._persist_closed_trade(trade)

                # A partial execution is a realized slice, but re-entry/churn
                # state changes only after the symbol position is fully closed.
                if sym not in self.account.state.open_positions:
                    self.funnel.increment("closed_trades")
                    next_consecutive_losses = (
                        0
                        if lifecycle_net_pnl > 0
                        else self.risk_engine.state.consecutive_losses + 1
                    )
                    self.risk_engine.update_state(
                        total_exposure=self.account.state.allocated,
                        current_equity=self.account.state.equity,
                        consecutive_losses=next_consecutive_losses,
                        open_positions_count=len(self.account.state.open_positions),
                    )
                    try:
                        signal_sequence = int(pos_data.metadata.get("signal_sequence", 0))
                    except (TypeError, ValueError):
                        signal_sequence = 0
                    self.symbol_risk.record_trade_exit(
                        sym,
                        lifecycle_net_pnl,
                        ex["reason"],
                        fill.slippage_bps,
                        self.account.state.equity,
                        exit_time=trade.exit_time,
                        return_pct=trade.return_pct,
                        direction=trade.direction,
                        strategy_id=trade.strategy_id,
                        entry_confidence=trade.entry_confidence,
                        signal_id=trade.signal_id,
                        signal_sequence=signal_sequence,
                        market_volatility_pct=float(
                            pos_data.metadata.get("entry_volatility_pct", 0.0)
                        ),
                    )
                self.strategy_risk.record_trade_exit(
                    trade.strategy_id,
                    trade.gross_pnl,
                    trade.net_pnl,
                    trade.fees,
                    trade.slippage_cost,
                    ex["reason"],
                    self.account.state.equity,
                    mfe_pct=trade.max_favorable_excursion_pct,
                    mae_pct=trade.max_adverse_excursion_pct,
                    holding_seconds=trade.holding_seconds,
                )

                pid = f"pos-{sym}"
                if self._persist is not None:
                    if sym in self.account.state.open_positions:
                        self._persist_position(pid, self.account.state.open_positions[sym])
                    else:
                        self._persist.delete_position(pid)
                        self._persist.delete_trail(pid)
                self._reconcile_risk()
                self._persist_protection_state()

        if not accepting_new:
            return

        # ── 2. GLOBAL RISK / CIRCUIT BREAKER STATUS ──
        entries_globally_blocked = (
            self.risk_engine.state.circuit_breaker_tripped
            or self.risk_engine.state.kill_switch_active
        )

        # ── 3. MARKET SCREENING & LIQUIDITY GATE ──
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10 or feat.last_price <= 0:
                self._record_pipeline_rejection("market_warmup", symbol=canonical)
                continue

            # Check Feed Health.  Do not silently turn an unhealthy feed into
            # a lack of signals; it is a stale-market safety rejection.
            ticker_health = self.feed_health.get("binance", canonical, "ticker")
            if ticker_health and not ticker_health.is_healthy:
                self._record_pipeline_rejection("stale_market", symbol=canonical)
                continue

            # Cooldown does not suppress signal observation.  Strategies must
            # still see a false condition so their signal regime can rearm.

            # Data Age
            data_age = max(0.0, (datetime.now(UTC) - feat.updated_at).total_seconds())

            # Evaluate Liquidity Gate
            book = self.order_book_engine.get_book("binance", canonical)
            self._liquidity_checks += 1
            lq_eval = self.liquidity_gate.assess_market(
                canonical, book, feat.volume_24h, data_age,
                expected_slippage_bps=feat.spread_bps / 2.0,
            )
            if not lq_eval.passed:
                if lq_eval.reason == LiquidityRejectionReason.SPREAD_TOO_WIDE:
                    reason = "spread"
                elif lq_eval.reason == LiquidityRejectionReason.MARKET_DATA_STALE:
                    reason = "stale_market"
                else:
                    reason = "liquidity"
                self._record_pipeline_rejection(reason, symbol=canonical)
                continue

            # Record this only-after-live observation before signal assessment;
            # it is never derived from a future candle or exit outcome.
            self.entry_quality.observe_market(feat)

            # Build qualified AssetSnapshot
            depth_10bps = book.depth_within_bps(10) if book else 0.0
            snapshots.append(AssetSnapshot(
                symbol=canonical, exchange="binance", asset_class=AssetClass.CRYPTO_SPOT,
                last_price=feat.last_price, bid=feat.bid, ask=feat.ask,
                spread_pct=lq_eval.spread_bps / 100.0, volume_24h=feat.volume_24h,
                price_change_1m_pct=feat.return_1m_pct,
                price_change_5m_pct=feat.return_5m_pct,
                volume_vs_avg_ratio=max(1.0, feat.relative_volume),
                bid_ask_ratio=feat.bid_ask_ratio, depth_bid_10bps=depth_10bps,
            ))

        # ── 4. STRATEGY SIGNAL GENERATION & DURABLE IDENTITY ──
        strategy_signals: list[Any] = []
        evaluated_signal_keys: set[str] = set()
        if snapshots:
            scanner_signals = self.scanner.scan(snapshots)
            diagnostics = self.scanner.last_diagnostics
            for scanner_reason, count in diagnostics.safety_rejections.items():
                normalized = scanner_reason.lower()
                if "spread" in normalized:
                    reason = "spread"
                elif "volume" in normalized or "depth" in normalized or "bid/ask" in normalized:
                    reason = "liquidity"
                else:
                    reason = "scanner_safety"
                for _ in range(count):
                    self._record_pipeline_rejection(reason, strategy_id="global_scanner")
            for _ in range(diagnostics.below_confidence_threshold + diagnostics.capped_signals):
                self._record_pipeline_rejection("below_score", strategy_id="global_scanner")

            converted_scanner_signals = self.scanner.to_strategy_signals(scanner_signals)
            strategy_signals.extend(converted_scanner_signals)
            for snap in snapshots:
                canonical = snap.symbol
                evaluated_signal_keys.add(
                    self.signal_guard.key("global_scanner", canonical)
                )
                feat = self.features.get(canonical)
                for strat in self.registry.get_enabled():
                    sid = strat.strategy_id
                    evaluated_signal_keys.add(self.signal_guard.key(sid, canonical))
                    self._strategy_evaluations += 1
                    self._strategy_evaluations_by_strategy[sid] = (
                        self._strategy_evaluations_by_strategy.get(sid, 0) + 1
                    )
                    self._strategy_evaluations_by_symbol[canonical] = (
                        self._strategy_evaluations_by_symbol.get(canonical, 0) + 1
                    )
                    try:
                        sig = await strat.analyze(features=feat)  # type: ignore[call-arg]
                        if sig is not None:
                            strategy_signals.append(sig)
                        else:
                            self._no_signal_decisions += 1
                            self._record_inactive_signal("strategy_inactive", sid, canonical)
                    except Exception:
                        self._exceptions += 1
                        self._record_pipeline_rejection(
                            "strategy_exception", strategy_id=sid, symbol=canonical
                        )

        # Activity mode uses the same identity/cost/entry gates as organic
        # signals; only its signal source is synthetic.
        if self._activity_test and not strategy_signals:
            test_signals = self._inject_test_signals()
            strategy_signals.extend(test_signals)
            for signal in test_signals:
                evaluated_signal_keys.add(
                    self.signal_guard.key(signal.strategy_id, signal.symbol)
                )

        # Raw means a concrete object emitted by a scanner/strategy.  ``None``
        # decisions were already counted as inactive above, rather than being
        # conflated with a raw signal.
        self.funnel.increment("raw_signals", len(strategy_signals))
        strategy_signals = self.signal_guard.observe_cycle(strategy_signals, evaluated_signal_keys)
        valid_signals: list[Any] = []
        for signal in strategy_signals:
            is_valid, invalid_reason = self._valid_signal(signal)
            if not is_valid:
                self._record_inactive_signal(
                    invalid_reason, signal.strategy_id, signal.symbol
                )
                continue
            self._annotate_signal_cost(signal)
            valid_signals.append(signal)
        self.funnel.increment("valid_signals", len(valid_signals))
        self._total_signals = self.funnel.counters["raw_signals"]
        self._persist_protection_state()

        if not valid_signals:
            self._total_scans += 1
            return

        # ── 5. OPPORTUNITY EVALUATION, QUALITY, & RANKING ──
        evaluated_opportunities = [
            self.opportunity_engine.evaluate(signal) for signal in valid_signals
        ]
        self.funnel.increment("opportunities_created", len(evaluated_opportunities))
        self._total_opportunities = self.funnel.counters["opportunities_created"]
        opportunities = []
        for opportunity in evaluated_opportunities:
            strategy_id = opportunity.signal.strategy_id
            symbol = opportunity.signal.symbol
            if opportunity.status.value != "ranked":
                rejection = opportunity.rejection_reason.value if opportunity.rejection_reason else "opportunity_rejected"
                if rejection == "insufficient_expected_edge":
                    reason = "expected_edge"
                elif rejection == "insufficient_confidence":
                    reason = "confidence"
                elif rejection == "low_score":
                    reason = "below_score"
                elif rejection in {"expired_signal", "stale_data"}:
                    reason = "stale_market"
                elif rejection == "liquidity":
                    reason = "liquidity"
                else:
                    reason = "opportunity_rejected"
                self._record_pipeline_rejection(
                    reason,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    entry_attempt=True,
                )
                continue

            feat = self.features.get(symbol or "")
            quality = self.entry_quality.assess(opportunity.signal, feat)
            opportunity.metadata["entry_quality"] = {
                "score": quality.quality_score,
                "required_score": quality.required_score,
                "momentum_multiple": quality.momentum_multiple,
                "required_momentum_multiple": quality.required_momentum_multiple,
                "reversal_risk": quality.reversal_risk,
                "market_structure_score": quality.market_structure_score,
                "volatility_pct": quality.volatility_pct,
                "signal_persistence": quality.signal_persistence,
                "reasons": list(quality.reasons),
            }
            if not quality.passed:
                # This is deliberately classified as a score-stage rejection:
                # the opportunity was economically viable but lacked a robust,
                # volatility-normalized entry setup.
                self._record_pipeline_rejection(
                    "below_score",
                    strategy_id=strategy_id,
                    symbol=symbol,
                    entry_attempt=True,
                )
                # Detailed component reasons remain attached to the evaluated
                # opportunity for audit/log persistence without turning one
                # rejected candidate into several percentage-denominator rows.
                continue
            self.funnel.increment("qualified_opportunities")
            opportunities.append(opportunity)

        opportunities.sort(key=lambda item: item.score.final_score, reverse=True)
        if not opportunities:
            self._total_scans += 1
            self._persist_protection_state()
            return

        if entries_globally_blocked:
            logger.warning("risk_circuit_breaker_active_entries_blocked")
            for opp in opportunities:
                self._record_entry_rejection(
                    "risk", strategy_id=opp.signal.strategy_id, symbol=opp.signal.symbol
                )
            self._total_scans += 1
            self._persist_protection_state()
            return

        # ── 6. RISK & PORTFOLIO CAPACITY ALLOCATION ──
        self.risk_engine.update_state(
            total_exposure=self.account.state.allocated,
            current_equity=self.account.state.equity,
            open_positions_count=len(self.account.state.open_positions),
        )
        tier_state = self.tier_manager.determine_tier(self.account.state.equity)
        available = max(0, tier_state.target_slots - len(self.account.state.open_positions))
        if available <= 0:
            for opp in opportunities:
                self._record_entry_rejection(
                    "capacity", strategy_id=opp.signal.strategy_id, symbol=opp.signal.symbol
                )
            self._total_scans += 1
            self._persist_protection_state()
            return

        open_positions = self.account.state.open_positions
        asset_exposure: dict[str, float] = {}
        strategy_exposure: dict[str, float] = {}
        for symbol, position in open_positions.items():
            base_asset = symbol.split("-")[0] if "-" in symbol else symbol
            asset_exposure[base_asset] = asset_exposure.get(base_asset, 0.0) + position.notional
            strategy_exposure[position.strategy_id or "unknown"] = (
                strategy_exposure.get(position.strategy_id or "unknown", 0.0) + position.notional
            )
        pf_state = PortfolioState(
            total_equity=self.account.state.equity,
            available_cash=self.account.state.cash,
            positions={symbol: pos.notional for symbol, pos in open_positions.items()},
            asset_exposure=asset_exposure,
            strategy_exposure=strategy_exposure,
            exchange_exposure={"binance": self.account.state.allocated},
            active_symbols=set(open_positions.keys()),
            total_exposure_pct=(
                self.account.state.allocated / max(self.account.state.equity, 1e-9) * 100.0
            ),
        )

        opened_this_tick = 0
        for opp in opportunities:
            strategy_id = opp.signal.strategy_id
            sym = opp.signal.symbol or "unknown"
            if opened_this_tick >= available:
                # Do not break: each ranked candidate is accounted for rather
                # than disappearing once the current slot budget is full.
                self._record_entry_rejection("capacity", strategy_id=strategy_id, symbol=sym)
                continue
            if sym in self.account.state.open_positions:
                self._record_entry_rejection("capacity", strategy_id=strategy_id, symbol=sym)
                continue

            # First establish freshness.  A continuous predicate that has
            # already opened a position is a re-entry rejection, not evidence
            # that a market failed its cooldown.
            is_fresh, _ = self.signal_guard.can_enter(opp.signal)
            if not is_fresh:
                self._record_entry_rejection(
                    "duplicate_signal", strategy_id=strategy_id, symbol=sym
                )
                continue

            quality_metadata = opp.metadata.get("entry_quality", {})
            try:
                signal_sequence = int(opp.signal.metadata.get("signal_sequence", 0))
            except (TypeError, ValueError):
                signal_sequence = 0
            reentry = self.symbol_risk.evaluate_entry(
                ReentryContext(
                    symbol=sym,
                    direction=opp.signal.direction.value,
                    strategy_id=strategy_id,
                    confidence=opp.signal.confidence,
                    signal_id=opp.signal.signal_id,
                    signal_sequence=signal_sequence,
                    fresh_signal=self.signal_guard.is_new_sequence(opp.signal),
                    market_structure_score=float(
                        quality_metadata.get("market_structure_score", 0.0)
                    ),
                    market_volatility_pct=float(quality_metadata.get("volatility_pct", 0.0)),
                ),
                self.account.state.equity,
            )
            if not reentry.allowed:
                reentry_reason = "reentry" if reentry.reason.startswith("REENTRY_") else "cooldown"
                self._record_entry_rejection(
                    reentry_reason, strategy_id=strategy_id, symbol=sym
                )
                continue

            is_strat_eligible, _ = self.strategy_risk.is_strategy_eligible(
                strategy_id, self.account.state.equity
            )
            if not is_strat_eligible:
                self._record_entry_rejection("strategy_risk", strategy_id=strategy_id, symbol=sym)
                continue

            self._risk_assessments += 1
            risk = self.risk_engine.assess(opp)
            if risk.decision.value != "approved":
                self._record_entry_rejection("risk", strategy_id=strategy_id, symbol=sym)
                continue
            self._risk_approved += 1
            self.funnel.increment("approved_opportunities")
            if risk.stop_loss_price is None:
                self._record_entry_rejection("missing_stop", strategy_id=strategy_id, symbol=sym)
                continue

            feat = self.features.get(sym)
            book = self.order_book_engine.get_book("binance", sym)
            if not book or not book.asks.levels or not book.bids.levels:
                self._record_entry_rejection("missing_book", strategy_id=strategy_id, symbol=sym)
                continue

            # Strategy evidence can only reduce (never increase) an allocation,
            # and only after the strategy manager has enough observations.
            allocation_multiplier = self.strategy_risk.allocation_multiplier(strategy_id)
            cap = PositionCapacity(
                symbol=sym,
                strategy_id=strategy_id,
                max_efficient_size=min(
                    risk.max_position_size, self.account.state.cash * 0.2
                ) * allocation_multiplier,
                is_viable=True,
            )
            decisions = self.allocator.allocate(pf_state, [(opp, risk, cap)])
            if not decisions or not decisions[0].is_allocated:
                allocation_reason = decisions[0].rejection_reason.lower() if decisions else ""
                reason = "correlation" if "correlation" in allocation_reason else "capacity"
                self._record_entry_rejection(reason, strategy_id=strategy_id, symbol=sym)
                continue

            allocated_capital = decisions[0].allocated_capital * allocation_multiplier
            if allocated_capital < 50.0:
                self._record_entry_rejection("minimum_notional", strategy_id=strategy_id, symbol=sym)
                continue

            # ── 7. EXECUTION-AWARE SIZING & ACTUAL COST GATE ──
            capital_qty = allocated_capital / max(feat.ask, 0.01)
            risk_qty = risk.max_position_size / max(feat.ask, 0.01)
            safe_qty = self.execution_estimator.compute_max_safe_quantity(
                sym,
                book,
                risk_qty=risk_qty,
                capital_qty=capital_qty,
                strategy_qty=capital_qty,
                stop_loss_pct=self.risk_engine.default_stop_loss_pct,
            )
            if safe_qty * feat.ask < 50.0:
                self._participation_rejections += 1
                self._record_entry_rejection("participation", strategy_id=strategy_id, symbol=sym)
                continue

            entry_sim = self.execution_estimator.simulate_buy_entry(sym, book, safe_qty)
            if not entry_sim.passed:
                if entry_sim.rejection_reason == LiquidityRejectionReason.ENTRY_SLIPPAGE_TOO_HIGH:
                    self._entry_slippage_rejections += 1
                elif entry_sim.rejection_reason == LiquidityRejectionReason.PARTICIPATION_TOO_HIGH:
                    self._participation_rejections += 1
                self._record_entry_rejection("entry_execution", strategy_id=strategy_id, symbol=sym)
                continue

            exit_sim = self.execution_estimator.simulate_sell_exit(
                sym,
                book,
                entry_sim.filled_qty,
                stop_loss_pct=self.risk_engine.default_stop_loss_pct,
            )
            if not exit_sim.passed:
                if exit_sim.rejection_reason == LiquidityRejectionReason.EXIT_SLIPPAGE_TOO_HIGH:
                    self._exit_slippage_rejections += 1
                self._record_entry_rejection("exit_execution", strategy_id=strategy_id, symbol=sym)
                continue

            expected_edge = self.paper_exec.estimate_expected_net_edge(
                entry_sim.filled_qty,
                entry_sim.expected_vwap,
                exit_sim.exit_vwap,
                float(opp.signal.estimated_return or 0.0),
                self._paper_config.min_expected_edge_over_cost,
            )
            if not expected_edge.is_positive_after_costs:
                self._record_entry_rejection("expected_edge", strategy_id=strategy_id, symbol=sym)
                continue

            opp.signal.metadata.update({
                "expected_gross_edge_fraction": expected_edge.expected_gross_edge_fraction,
                "expected_gross_edge_usd": expected_edge.expected_gross_edge_usd,
                "estimated_entry_fee": expected_edge.estimated_entry_fee,
                "estimated_exit_fee": expected_edge.estimated_exit_fee,
                "estimated_slippage": expected_edge.expected_slippage,
                "estimated_spread_cost": expected_edge.estimated_spread_cost,
                "estimated_round_trip_cost": expected_edge.costs.estimated_round_trip_cost,
                "estimated_round_trip_cost_fraction": (
                    expected_edge.costs.estimated_round_trip_cost_fraction
                ),
                "safety_buffer_fraction": expected_edge.safety_buffer_fraction,
                "expected_net_edge_fraction": expected_edge.expected_net_edge_fraction,
                "expected_net_edge_usd": expected_edge.expected_net_edge_usd,
                "strategy_allocation_multiplier": allocation_multiplier,
            })

            # ── 8. ORDER CREATION & PAPER EXECUTION ──
            asks_depth = [(level[0], level[1]) for level in book.asks.levels]
            self.funnel.increment("execution_attempts")
            entry_fill = await self.paper_exec.simulate_fill(
                sym,
                "buy",
                safe_qty,
                bid=book.best_bid,
                ask=book.best_ask,
                last=feat.last_price,
                asks_depth=asks_depth,
            )
            if not entry_fill or entry_fill.filled_qty <= 0:
                self._record_entry_rejection("entry_fill", strategy_id=strategy_id, symbol=sym)
                continue

            order_id = (
                f"entry-{sym}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
                f"-{uuid.uuid4().hex[:6]}"
            )
            self._persist_order({
                "order_id": order_id,
                "client_order_id": order_id,
                "symbol": sym,
                "side": "buy",
                "requested_qty": safe_qty,
                "filled_qty": entry_fill.filled_qty,
                "remaining_qty": max(safe_qty - entry_fill.filled_qty, 0),
                "avg_fill_price": entry_fill.fill_price,
                "status": entry_fill.status,
            })
            self._orders_created += 1
            self._persist_fill({
                "fill_id": f"{order_id}-fill",
                "order_id": order_id,
                "symbol": sym,
                "side": "buy",
                "quantity": entry_fill.filled_qty,
                "price": entry_fill.fill_price,
                "notional": entry_fill.fill_price * entry_fill.filled_qty,
                "fees": entry_fill.fees,
                "slippage_bps": entry_fill.slippage_bps,
            })
            self._fills_created += 1
            if entry_fill.status == "PARTIALLY_FILLED_CANCELED":
                self._partial_fills_canceled += 1
                self._partial_fills += 1
            self._slippage_bps_list.append(entry_fill.slippage_bps)

            quantity = entry_fill.filled_qty
            fill_price = entry_fill.fill_price
            hard_stop = fill_price * (
                1.0 - self.risk_engine.default_stop_loss_pct / 100.0
            )
            entry_embedded_slippage = max(
                0.0, (entry_fill.fill_price - entry_fill.vwap_price) * quantity
            )

            actual_cost_estimate = self.paper_exec.estimate_round_trip_cost(
                quantity, entry_fill.vwap_price, exit_sim.exit_vwap
            )
            quality_metadata = opp.metadata.get("entry_quality", {})
            trail_params = compute_volatility_aware_trail(
                base_trail_distance_pct=self._paper_config.trail_distance_pct,
                base_activation_pct=self._paper_config.trail_activation_pct,
                volatility_pct=float(quality_metadata.get("volatility_pct", 0.0)),
                spread_bps=feat.spread_bps,
                round_trip_cost_fraction=actual_cost_estimate.estimated_round_trip_cost_fraction,
                volatility_multiplier=self._paper_config.trail_volatility_multiplier,
                spread_multiplier=self._paper_config.trail_spread_multiplier,
                activation_volatility_multiplier=(
                    self._paper_config.trail_activation_volatility_multiplier
                ),
                max_trail_distance_pct=self._paper_config.max_trail_distance_pct,
            )
            trail_distance_fraction = trail_params.trail_distance_pct / 100.0
            fee_aware_activation_pct = trail_params.activation_pct
            if trail_distance_fraction < 1.0:
                fee_aware_activation_pct = max(
                    fee_aware_activation_pct,
                    (
                        (1.0 + actual_cost_estimate.estimated_round_trip_cost_fraction)
                        / (1.0 - trail_distance_fraction)
                        - 1.0
                    )
                    * 100.0,
                )

            signal_timestamp_raw = opp.signal.metadata.get("signal_timestamp")
            signal_timestamp = (
                _parse_iso_or_now(str(signal_timestamp_raw))
                if signal_timestamp_raw
                else opp.signal.timestamp
            )
            pos = self.account.open_position(
                sym,
                "long",
                fill_price,
                quantity,
                fees=entry_fill.fees,
                stop_loss_price=hard_stop,
                strategy_id=opp.signal.strategy_id,
                entry_reference_price=entry_fill.vwap_price,
                entry_slippage_cost=entry_embedded_slippage,
                trail_activation_pct=fee_aware_activation_pct,
                signal_id=opp.signal.signal_id,
                signal_timestamp=signal_timestamp,
                entry_confidence=opp.signal.confidence,
                metadata={
                    "estimated_round_trip_cost": (
                        actual_cost_estimate.estimated_round_trip_cost
                    ),
                    "estimated_round_trip_cost_fraction": (
                        actual_cost_estimate.estimated_round_trip_cost_fraction
                    ),
                    "expected_gross_edge_fraction": expected_edge.expected_gross_edge_fraction,
                    "expected_net_edge_fraction": expected_edge.expected_net_edge_fraction,
                    "expected_net_edge_usd": expected_edge.expected_net_edge_usd,
                    "safety_buffer_fraction": expected_edge.safety_buffer_fraction,
                    "entry_quality_score": quality_metadata.get("score", 0.0),
                    "entry_quality_required_score": quality_metadata.get("required_score", 0.0),
                    "market_structure_score": quality_metadata.get("market_structure_score", 0.0),
                    "entry_volatility_pct": quality_metadata.get("volatility_pct", 0.0),
                    "signal_persistence": quality_metadata.get("signal_persistence", 0),
                    "signal_sequence": signal_sequence,
                    "effective_trail_distance_pct": trail_params.trail_distance_pct,
                    "effective_trail_activation_pct": fee_aware_activation_pct,
                    "trail_volatility_component_pct": trail_params.volatility_component_pct,
                    "trail_spread_component_pct": trail_params.spread_component_pct,
                    "strategy_allocation_multiplier": allocation_multiplier,
                },
            )
            if not pos:
                self._record_entry_rejection("account", strategy_id=strategy_id, symbol=sym)
                continue

            # Consume only after a position was actually created.  Persisting
            # immediately makes this idempotent across process restart.
            self.signal_guard.record_consumed(opp.signal, consumed_at=pos.entry_time)
            self._persist_protection_state()
            pos_id = f"pos-{sym}"
            self.monitor.register_position(pos)
            self.funnel.increment("successful_entries")
            self._positions_opened_total += 1
            self._total_trades += 1
            opened_this_tick += 1
            self.analytics.record_allocation(
                sym,
                opp.signal.strategy_id,
                "binance",
                fill_price * quantity,
            )
            self._trade_log.append({
                "symbol": sym,
                "side": "buy",
                "notional": fill_price * quantity,
                "signal_id": opp.signal.signal_id,
                "estimated_round_trip_cost": (
                    actual_cost_estimate.estimated_round_trip_cost
                ),
                "time": datetime.now(UTC).isoformat(),
            })
            self._persist_position(pos_id, pos)
            self._persist_trail(pos_id)
            self._persist_account()
            # Keep subsequent allocations in this scan aware of capital and
            # symbol exposure already committed by an earlier candidate.
            pf_state.active_symbols.add(sym)
            pf_state.positions[sym] = pos.notional
            pf_state.available_cash = self.account.state.cash
            pf_state.total_exposure_pct = (
                self.account.state.allocated / max(self.account.state.equity, 1e-9) * 100.0
            )
            self._reconcile_risk()

        self._total_scans += 1
        for symbol in list(self.account.state.open_positions):
            self._persist_trail(f"pos-{symbol}")
        self._persist_protection_state()

    # ══════════════════════════════════════════════════════════════════
    def _inject_test_signals(self) -> list:
        now = time.monotonic()
        if self._last_test_signal_time > 0 and (now - self._last_test_signal_time < 30.0):
            return []

        from datetime import timedelta

        from src.strategies.base import SignalDirection, StrategySignal

        signals = []
        eligible = []
        for s in self._canonical_symbols:
            if s in self.account.state.open_positions:
                continue
            is_eligible, _ = self.symbol_risk.is_symbol_eligible(s, self.account.state.equity)
            if not is_eligible:
                continue
            feat = self.features.get(s)
            if feat.last_price <= 0 or feat.ask <= 0:
                continue
            bk = self.order_book_engine.get_book("binance", s)
            if bk is not None and bool(bk.bids.levels) and bool(bk.asks.levels):
                lq = self.liquidity_gate.assess_market(s, bk, feat.volume_24h, 1.0)
                if lq.passed:
                    eligible.append(s)
        if not eligible:
            return []

        self._last_test_signal_time = now
        sym = eligible[0]
        feat = self.features.get(sym)
        price = feat.last_price

        sig = StrategySignal(
            strategy_id="activity_test_v1",
            symbol=sym,
            direction=SignalDirection.LONG,
            confidence=0.99,
            estimated_return=0.01,
            estimated_risk=0.3,
            required_capital=500.0,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=120),
            entry_logic={"type": "activity_test", "price": price},
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.20,
                "activation_pct": 0.20,
                "no_fixed_take_profit": True,
            },
            metadata={
                "entry_price": price,
                "stop_loss_pct": 0.30,
                "test_mode": True,
            },
        )
        signals.append(sig)
        return signals

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_tick()
                await asyncio.sleep(self._scan_interval)
            except asyncio.CancelledError:
                break
            except Exception:
                self._exceptions += 1
                logger.exception("scan_loop_error")
                self._error_log.append({"error": "scan_loop", "time": datetime.now(UTC).isoformat()})
                if self._fatal_error is None:
                    self._fatal_error = f"fatal_scan_loop_exception_at_{datetime.now(UTC).isoformat()}"
                self._running = False
                self._accepting_new = False
                break

    async def _report_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._report_interval)
            self._touch_heartbeat()
            self._sample_resource_peak()
            self._persist_runtime_state()
            s = self.account.state
            elapsed_hours = max(1e-9, (time.monotonic() - self._start_time) / 3600.0)
            funnel = self.funnel.funnel()
            logger.info(
                "paper_status",
                equity=round(s.equity, 0),
                pnl=round(s.realized_pnl, 0),
                positions=len(s.open_positions),
                trades=s.trade_count,
                opportunities_per_hour=round(funnel["opportunities_created"] / elapsed_hours, 3),
                qualified_opportunities_per_hour=round(
                    funnel["qualified_opportunities"] / elapsed_hours, 3
                ),
                entries_per_hour=round(funnel["successful_entries"] / elapsed_hours, 3),
                rejected_entries=self.funnel.entry_rejections,
                net_expectancy=round(s.realized_pnl / s.trade_count, 6) if s.trade_count else 0.0,
                drawdown_pct=round(s.max_drawdown_pct, 6),
                pub=self.publish_count,
                con=self.consume_count,
            )

    def _touch_heartbeat(self) -> None:
        try:
            hb_dir = os.path.dirname(self._db_path) or "."
            Path(os.path.join(hb_dir, ".heartbeat")).touch()
        except Exception:
            pass

    def _get_bid(self, symbol: str) -> float:
        feat = self.features.get(symbol)
        return feat.bid if feat.bid > 0 else 0.0

    def _final_report(self) -> dict[str, Any]:
        """Return the complete paper-session diagnostic report.

        The established flat keys are kept for dashboards that already consume
        them.  The nested sections are the durable reporting contract for the
        signal funnel, performance, exits, strategy evidence, and symbols.
        """
        s = self.account.state
        duration_seconds = max(0.0, time.monotonic() - self._start_time)
        wall_secs = (
            (datetime.now(UTC) - self._wall_start).total_seconds() if self._wall_start else 0.0
        )
        rss_now = _get_memory_mb()
        slips = self._slippage_bps_list
        avg_slip = (sum(slips) / len(slips)) if slips else 0.0
        max_slip = max(slips) if slips else 0.0
        p95_slip = sorted(slips)[min(len(slips) - 1, int(len(slips) * 0.95))] if slips else 0.0
        try:
            task_count_end = len(asyncio.all_tasks())
        except RuntimeError:
            task_count_end = 0

        trades = list(s.closed_trades)
        performance = trade_metrics(trades)
        exits = exit_analysis(trades)
        strategy_analysis = self.strategy_risk.get_summary()
        symbol_analysis = grouped_trade_metrics(trades, "symbol")
        symbol_rejections = self.funnel.per_symbol_rejections()
        for symbol, reasons in symbol_rejections.items():
            symbol_analysis.setdefault(symbol, trade_metrics([]))["rejection_reasons"] = reasons

        funnel = self.funnel.funnel()
        signal_funnel = {
            "raw_signals": funnel["raw_signals"],
            "qualified_signals": funnel["valid_signals"],
            "valid_signals": funnel["valid_signals"],
            "inactive_signals": funnel["inactive_signals"],
            "opportunities": funnel["opportunities_created"],
            "opportunities_created": funnel["opportunities_created"],
            "approved_opportunities": funnel["approved_opportunities"],
            "qualified_opportunities": funnel["qualified_opportunities"],
            "entries": funnel["successful_entries"],
            "execution_attempts": funnel["execution_attempts"],
            "successful_entries": funnel["successful_entries"],
            "closed_trades": funnel["closed_trades"],
        }
        hours = duration_seconds / 3600.0
        throughput = {
            "opportunities_per_hour": round(
                funnel["opportunities_created"] / hours if hours > 0 else 0.0, 4
            ),
            "qualified_opportunities_per_hour": round(
                funnel["qualified_opportunities"] / hours if hours > 0 else 0.0, 4
            ),
            "entries_per_hour": round(
                funnel["successful_entries"] / hours if hours > 0 else 0.0, 4
            ),
            "net_expectancy": performance["expectancy"],
            "profit_factor": performance["profit_factor"],
            "max_drawdown_pct": round(s.max_drawdown_pct, 6),
        }
        status = "FAILED" if self._fatal_error else "complete"
        closed_trading_costs = performance["fees"] + performance["slippage"]
        report: dict[str, Any] = {
            "status": status,
            "fatal_error": self._fatal_error,
            "duration_seconds": duration_seconds,
            "wall_seconds": wall_secs,
            "initial_balance": self.initial_balance,
            "final_equity": round(s.equity, 2),
            "net_pnl": round(s.equity - s.initial_balance, 2),
            "realized_net_pnl": round(s.realized_pnl, 2),
            "unrealized_pnl": round(s.unrealized_pnl, 2),
            "gross_realized_pnl": performance["gross_pnl"],
            "closed_trading_costs": round(closed_trading_costs, 4),
            "total_fees": round(s.total_fees, 4),
            "total_slippage": round(s.total_slippage, 4),
            "total_trades": s.trade_count,
            "wins": s.win_count,
            "losses": s.loss_count,
            "win_rate": round(s.win_count / s.trade_count * 100.0, 2) if s.trade_count else 0.0,
            "total_signals": funnel["raw_signals"],
            "total_opportunities": funnel["opportunities_created"],
            "risk_assessments": self._risk_assessments,
            "risk_approved": self._risk_approved,
            "risk_rejected": funnel["risk_rejections"],
            "orders_created": self._orders_created,
            "fills_created": self._fills_created,
            "partial_fills": self._partial_fills,
            "partial_fills_canceled": self._partial_fills_canceled,
            "positions_opened": self._positions_opened_total,
            "positions_closed": self._positions_closed,
            "positions_currently_open": len(s.open_positions),
            "trailing_exits": self._trailing_exits,
            "hard_stop_exits": self._hard_stop_exits,
            "liquidity_checks": self._liquidity_checks,
            "liquidity_rejections": funnel["liquidity_rejections"],
            "spread_rejections": funnel["spread_rejections"],
            "entry_slippage_rejections": self._entry_slippage_rejections,
            "exit_slippage_rejections": self._exit_slippage_rejections,
            "participation_rejections": self._participation_rejections,
            "stale_market_rejections": funnel["stale_market_rejections"],
            "rejected_entries": self.funnel.entry_rejections,
            "reentry_rejections": funnel["reentry_rejections"],
            "symbol_cooldown_rejections": funnel["cooldown_rejections"],
            "cooldown_rejections": funnel["cooldown_rejections"],
            "duplicate_signal_rejections": self._duplicate_signal_rejections,
            "expected_edge_rejections": funnel["expected_edge_rejections"],
            "consecutive_loss_events": self.symbol_risk.consecutive_loss_events_count,
            "early_reentries_allowed": self.symbol_risk.early_reentries_allowed_count,
            "average_slippage_bps": round(avg_slip, 2),
            "p95_slippage_bps": round(p95_slip, 2),
            "max_slippage_bps": round(max_slip, 2),
            "publish_count": self.publish_count,
            "consume_count": self.consume_count,
            "persistence_writes": self._persistence_writes,
            "persistence_errors": self._persistence_errors,
            "lease_heartbeat_success": self._lease_heartbeat_success,
            "lease_heartbeat_errors": self._lease_heartbeat_errors,
            "exceptions": self._exceptions,
            "closed_trade_ram": len(self.account.state.closed_trades),
            "closed_trade_ram_limit": CLOSED_TRADE_RAM_LIMIT,
            "rss_start_mb": self._rss_start_mb,
            "rss_peak_mb": self._rss_peak_mb,
            "rss_end_mb": rss_now,
            "task_count_start": self._task_count_start,
            "task_count_peak": self._task_count_peak,
            "task_count_end": task_count_end,
            "queue_depth_peak": self._queue_depth_peak,
            "strategy_metrics": strategy_analysis,
            "mode": "PAPER",
            "live_trading": "DISABLED",
            # Required end-of-session diagnostics.
            "signal_funnel": signal_funnel,
            "funnel_counters": funnel,
            "rejection_breakdown": self.funnel.rejection_breakdown(),
            "trade_performance": performance,
            "exit_analysis": exits,
            "strategy_analysis": strategy_analysis,
            "symbol_analysis": symbol_analysis,
            "throughput": throughput,
        }
        return cast(dict[str, Any], finite_report_value(report))

    def stop(self) -> None:
        self._running = False
        self._accepting_new = False

    def _handle_signal(self) -> None:
        self._accepting_new = False
        self._running = False

    def _get_experiment_id(self) -> str:
        return os.environ.get("PAPER_EXPERIMENT_ID", f"paper-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}")

    def _get_commit_sha(self) -> str:
        try:
            import subprocess
            return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()[:8]
        except Exception:
            return "unknown"
