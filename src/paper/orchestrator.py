"""Paper Trading Orchestrator — ROUND 14: 9-blocker evidence closure."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
from src.core.logging_config import get_logger
from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import BookLevel, CanonicalSymbol, TickerEvent
from src.data.order_book import OrderBookEngine
from src.db.persist import PaperPersistence
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import CLOSED_TRADE_RAM_LIMIT, ClosedTrade, PaperAccount, PaperPosition
from src.paper.engine import PaperExecutionEngine
from src.paper.position_monitor import PositionMonitor
from src.portfolio.allocator import AllocatorConfig, CapitalAllocator, PortfolioState
from src.portfolio.capacity import PositionCapacity
from src.portfolio.capital_tiers import CapitalTierConfig, CapitalTierManager
from src.portfolio.liquidity import LiquidityAnalyzer
from src.portfolio.markets import AssetQualityFilter, QualityFilterConfig
from src.portfolio.universe import UniverseConfig, UniverseManager
from src.risk.engine import RiskEngine
from src.scanner.global_scanner import AssetClass, AssetSnapshot, GlobalScanner
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.registry import StrategyRegistry

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
    """Round 14: 9-blocker closure."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
        db_path: str = "data/paper_trading.db",
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

        self.event_bus = EventBus(default_max_queue=500)
        self.order_book_engine = OrderBookEngine(max_books=200)
        self.feed_health = FeedHealthMonitor()
        self.features = FeatureEngine(max_instruments=500)
        self.universe = UniverseManager(UniverseConfig())
        self.quality_filter = AssetQualityFilter(QualityFilterConfig())
        self.scanner = GlobalScanner()
        self.registry = StrategyRegistry()
        self.opportunity_engine = OpportunityEngine(min_net_return=0.001)
        self.risk_engine = RiskEngine()
        self.tier_manager = CapitalTierManager(CapitalTierConfig())
        self.allocator = CapitalAllocator(AllocatorConfig())
        self.liquidity = LiquidityAnalyzer()
        self.account = PaperAccount(initial_balance=initial_balance)
        self.paper_exec = PaperExecutionEngine()
        self.monitor = PositionMonitor(self.account)
        self.analytics = AnalyticsTracker()
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
        self._positions_opened_total = 0  # R15: cumulative counter
        self._positions_closed = 0
        self._trailing_exits = 0
        self._hard_stop_exits = 0
        self._lease_heartbeat_success = 0
        self._lease_heartbeat_errors = 0
        self._persistence_writes = 0
        self._persistence_reads = 0
        self._persistence_errors = 0
        self._exceptions = 0

        self._rss_start_mb: float = 0.0
        self._rss_peak_mb: float = 0.0
        self._task_count_start: int = 0
        self._task_count_peak: int = 0
        self._queue_depth_peak: int = 0
        self._db_bytes_start: int = 0
        self._trade_log: deque[dict[str, Any]] = deque(maxlen=50000)
        self._error_log: deque[dict[str, Any]] = deque(maxlen=1000)

        # R14: closed-trade dedup set to prevent re-persistence
        self._persisted_trade_ids: set[str] = set()

        # R16: runtime safety flags for soak auto-fail detection
        self._fatal_error: str | None = None
        self._stale_feed_violation: bool = False

        # R21: Strategy pipeline observability counters
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
        saved = self._persist.load_account()
        if saved is not None:
            s = self.account.state
            s.cash = saved.get("cash", self.initial_balance)
            s.allocated = saved.get("allocated", 0)
            s.realized_pnl = saved.get("realized_pnl", 0)
            s.total_fees = saved.get("total_fees", 0)
            s.total_slippage = saved.get("total_slippage", 0)
            s.trade_count = saved.get("trade_count", 0)
            s.win_count = saved.get("win_count", 0)
            s.loss_count = saved.get("loss_count", 0)
            s.peak_equity = saved.get("peak_equity", self.initial_balance)

        # R15: Restore closed trades with original timestamps and trade_id from DB
        db_trades = self._persist.load_recent_closed_trades(200)
        for t_dict in db_trades:
            entry_time = _parse_iso_or_now(t_dict.get("entry_time", ""))
            exit_time = _parse_iso_or_now(t_dict.get("exit_time", ""))
            ct = ClosedTrade(
                symbol=t_dict["symbol"], direction=t_dict.get("direction", "long"),
                entry_price=t_dict["entry_price"], exit_price=t_dict["exit_price"],
                quantity=t_dict["quantity"], gross_pnl=t_dict.get("gross_pnl", 0),
                fees=t_dict.get("fees", 0), slippage_cost=t_dict.get("slippage_cost", 0),
                net_pnl=t_dict.get("net_pnl", 0), return_pct=t_dict.get("return_pct", 0),
                exit_reason=t_dict.get("exit_reason", ""),
                strategy_id=t_dict.get("strategy_id", ""),
                entry_time=entry_time,
                exit_time=exit_time,
                trade_id=t_dict.get("trade_id", ""),
            )
            self.account.state.closed_trades.append(ct)
            if ct.trade_id:
                self._persisted_trade_ids.add(ct.trade_id)

        positions = self._persist.load_open_positions()
        for p_dict in positions:
            pos = PaperPosition(
                symbol=p_dict["symbol"], direction=p_dict.get("direction", "long"),
                entry_price=p_dict["entry_price"], quantity=p_dict["quantity"],
                notional=p_dict.get("entry_notional", p_dict["entry_price"] * p_dict["quantity"]),
                stop_loss_price=p_dict.get("stop_loss_price", 0),
                fees_paid=p_dict.get("entry_fee", 0),
                strategy_id=p_dict.get("strategy_id", ""),
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

        self.account.state.unrealized_pnl = sum(
            p.unrealized_pnl for p in self.account.state.open_positions.values()
        )

    # ══════════════════════════════════════════════════════════════════
    async def _health_supervisor(self) -> None:
        stabilization_ticks = 3
        while self._running:
            await asyncio.sleep(5.0)
            unhealthy = self.feed_health.check_all()
            all_feeds = self.feed_health.get_all()
            if all_feeds and all(not f.is_healthy for f in all_feeds):
                # R16: Record stale-feed violation WHILE still accepting new
                if self._accepting_new:
                    self._stale_feed_violation = True
                self._accepting_new = False
                self._health_recovery_counter.clear()
            elif unhealthy:
                # R16: Any unhealthy feed while accepting is a violation
                if self._accepting_new:
                    self._stale_feed_violation = True
                for fh in unhealthy:
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
        """R14 P-01: Reconcile risk state from current account positions immediately."""
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
            self._persist.save_account({
                "cash": self.account.state.cash,
                "initial_balance": self.account.state.initial_balance,
                "allocated": self.account.state.allocated,
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
                "entry_notional": pos.notional, "cost_basis": pos.notional + pos.fees_paid,
                "entry_fee": pos.fees_paid, "stop_loss_price": pos.stop_loss_price,
                "strategy_id": pos.strategy_id,
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
        """R15 P-02: Use existing durable trade_id or generate deterministic one."""
        if self._persist is None:
            return
        # Use existing durable trade_id if set (restored trades)
        if trade.trade_id:
            trade_id = trade.trade_id
        else:
            trade_id = (
                f"ct-{trade.symbol}-{trade.exit_time.strftime('%Y%m%d%H%M%S%f')[:16]}"
                f"-{trade.entry_price:.2f}-{trade.exit_price:.2f}-{trade.quantity:.6f}"
            )
            trade.trade_id = trade_id  # Store for future reference
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
                "fees": trade.fees, "slippage_cost": trade.slippage_cost,
                "net_pnl": trade.net_pnl, "return_pct": trade.return_pct,
                "exit_reason": trade.exit_reason, "strategy_id": trade.strategy_id,
                "entry_time": trade.entry_time.isoformat(),
                "exit_time": trade.exit_time.isoformat(),
            })
            self._persisted_trade_ids.add(trade_id)
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1

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
            # R14 P-02: Only persist trades not already persisted
            for trade in self.account.state.closed_trades:
                self._persist_closed_trade(trade)
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
        self.features.update_order_book(sym, bid, ask)
        self.features.update_volume(sym, vol)
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
        self._book_events_received += 1
        self.feed_health.record_message("binance", canonical, "book", exchange_ts=datetime.now(UTC))

    # ══════════════════════════════════════════════════════════════════
    async def _scan_tick(self) -> None:
        if not self._accepting_new:
            return

        # 1. Check stops/trails
        exits = self.monitor.check_all()
        for ex in exits:
            sym = ex["symbol"]
            pos_data = self.account.state.open_positions.get(sym)
            if pos_data is None:
                continue
            qty = pos_data.quantity
            book = self.order_book_engine.get_book("binance", sym)
            bids_depth = [(lv[0], lv[1]) for lv in (book.bids.levels[:20] if book else [])] if book else None
            bid = book.best_bid if book and book.best_bid > 0 else self._get_bid(sym)

            order_id = f"exit-{sym}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            fill = await self.paper_exec.simulate_fill(
                sym, "sell", qty, bid=bid, ask=ex["price"], last=ex["price"],
                bids_depth=bids_depth,
            )
            if fill is None or fill.filled_qty <= 0:
                continue

            # R14 D-01: requested_qty = qty (immutable, captured before await)
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
            if fill.status == "PARTIALLY_FILLED":
                self._partial_fills += 1

            if abs(fill.filled_qty - qty) < 0.00000001:
                trade = self.account.close_position(
                    sym, fill.fill_price, fees=fill.fees, slippage=0.0,
                    exit_reason=ex["reason"],
                    trail_peak=ex.get("trail_peak", 0.0),
                    trail_level=ex.get("trail_level", 0.0),
                )
                self._positions_closed += 1
                if ex["reason"] == "trail_hit":
                    self._trailing_exits += 1
                elif ex["reason"] == "hard_stop":
                    self._hard_stop_exits += 1
            else:
                trade = self.account.reduce_position(
                    sym, fill.fill_price, fill.filled_qty,
                    fees=fill.fees, slippage=0.0, exit_reason=ex["reason"],
                )
                if sym in self.account.state.open_positions:
                    self.monitor.rearm_position(sym)

            if sym not in self.account.state.open_positions:
                self.monitor.unregister_position(sym)

            if trade:
                self._total_trades += 1
                self.analytics.record_trade(trade.gross_pnl, trade.net_pnl, trade.fees,
                                            slippage=trade.slippage_cost,
                                            strategy_id=trade.strategy_id, exchange="binance")
                self._trade_log.append({"symbol": sym, "reason": ex["reason"],
                                        "pnl": trade.net_pnl,
                                        "time": datetime.now(UTC).isoformat()})
                self._persist_account()
                self._persist_closed_trade(trade)
                pid = f"pos-{sym}"
                if self._persist is not None:
                    if sym in self.account.state.open_positions:
                        self._persist_position(pid, self.account.state.open_positions[sym])
                    else:
                        self._persist.delete_position(pid)
                # R14 P-01: reconcile risk immediately after exit
                self._reconcile_risk()

        # 2. Build snapshots
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            ticker_health = self.feed_health.get("binance", canonical, "ticker")
            book_health = self.feed_health.get("binance", canonical, "book")
            if ticker_health and not ticker_health.is_healthy:
                continue
            if book_health and not book_health.is_healthy:
                continue
            asset = self.universe.get(canonical, "binance")
            if asset and asset.status.value not in ("active", "watch"):
                continue
            book = self.order_book_engine.get_book("binance", canonical)
            liq = self.liquidity.analyze(None, feat.volume_24h)
            if book:
                liq.bid = book.best_bid
                liq.ask = book.best_ask
                liq.spread_pct = book.spread_bps / 100.0 if book.spread_bps > 0 else 0.05
                liq.depth_10bps = book.depth_within_bps(10)
                liq.liquidity_score = 0.85
            qr = self.quality_filter.assess(
                canonical, "binance", liquidity=liq, volume_24h=feat.volume_24h,
                spread_pct=liq.spread_pct,
                data_age_seconds=max(0.0, (datetime.now(UTC) - feat.updated_at).total_seconds()),
                daily_trades=5000,
            )
            if not qr.qualified:
                self.analytics.record_opportunity(rejected=True)
                continue
            snapshots.append(AssetSnapshot(
                symbol=canonical, exchange="binance", asset_class=AssetClass.CRYPTO_SPOT,
                last_price=feat.last_price, bid=feat.bid, ask=feat.ask,
                spread_pct=liq.spread_pct, volume_24h=feat.volume_24h,
                price_change_1m_pct=feat.return_1m_pct,
                price_change_5m_pct=feat.return_5m_pct,
                volume_vs_avg_ratio=max(1.0, feat.relative_volume),
                bid_ask_ratio=feat.bid_ask_ratio, depth_bid_10bps=liq.depth_10bps,
            ))
        self.analytics.record_opportunity()
        if not snapshots:
            return

        # 3. Signals
        scanner_signals = self.scanner.scan(snapshots)
        strategy_signals = self.scanner.to_strategy_signals(scanner_signals)
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            for strat in self.registry.get_enabled():
                self._strategy_evaluations += 1
                sid = strat.strategy_id
                self._strategy_evaluations_by_strategy[sid] = \
                    self._strategy_evaluations_by_strategy.get(sid, 0) + 1
                self._strategy_evaluations_by_symbol[canonical] = \
                    self._strategy_evaluations_by_symbol.get(canonical, 0) + 1
                try:
                    sig = await strat.analyze(features=feat)  # type: ignore[call-arg]
                    if sig and not sig.is_expired:
                        strategy_signals.append(sig)
                    else:
                        self._no_signal_decisions += 1
                        # Determine the rejection reason
                        if feat.sample_count < 20:
                            reason = "insufficient_history"
                        elif sid == "momentum_v1":
                            if feat.momentum_5m <= 0 and feat.trend_strength <= 0:
                                reason = "no_momentum"
                            elif feat.trend_strength <= 0:
                                reason = "no_trend"
                            else:
                                reason = "threshold_not_met"
                        elif sid == "breakout_v1":
                            if feat.relative_volume < 2.0:
                                reason = "low_volume"
                            elif feat.breakout_position_pct < 80:
                                reason = "no_breakout"
                            else:
                                reason = "threshold_not_met"
                        elif sid == "order_flow_v1":
                            if feat.bid_ask_ratio < 1.2:
                                reason = "balanced_book"
                            elif feat.trade_flow_ratio < 1.5:
                                reason = "neutral_flow"
                            else:
                                reason = "threshold_not_met"
                        else:
                            reason = "unknown"
                        self._no_signal_reasons[reason] = \
                            self._no_signal_reasons.get(reason, 0) + 1
                except Exception:
                    self._exceptions += 1
        self._total_signals += len(strategy_signals)
        if not strategy_signals:
            return

        # 4. Opportunity
        opportunities = self.opportunity_engine.evaluate_batch(strategy_signals)
        self._total_opportunities += len(opportunities)
        if not opportunities:
            return

        # 5. Risk update from state
        self.risk_engine.update_state(
            total_exposure=self.account.state.allocated,
            current_equity=self.account.state.equity,
            open_positions_count=len(self.account.state.open_positions),
        )
        tier_state = self.tier_manager.determine_tier(self.account.state.equity)
        available = max(0, tier_state.target_slots - len(self.account.state.open_positions))
        if available <= 0:
            return

        pf_state = PortfolioState(
            total_equity=self.account.state.equity,
            available_cash=self.account.state.cash,
            active_symbols=set(self.account.state.open_positions.keys()),
        )

        for opp in opportunities[:available]:
            sym = opp.signal.symbol or "unknown"
            self._risk_assessments += 1
            risk = self.risk_engine.assess(opp)
            if risk.decision.value != "approved":
                self._risk_rejected += 1
                continue
            self._risk_approved += 1
            if risk.stop_loss_price is None:
                continue
            if sym in self.account.state.open_positions:
                continue

            feat = self.features.get(sym)
            cap = PositionCapacity(
                symbol=sym, strategy_id=opp.signal.strategy_id,
                max_efficient_size=min(risk.max_position_size, self.account.state.cash * 0.2),
                is_viable=True,
            )
            decisions = self.allocator.allocate(pf_state, [(opp, risk, cap)])
            if not decisions or not decisions[0].is_allocated:
                continue
            pos_size = decisions[0].allocated_capital
            if pos_size < 50:
                continue

            book = self.order_book_engine.get_book("binance", sym)
            asks_depth = ([(lv[0], lv[1]) for lv in book.asks.levels[:20]] if book and book.asks else None)

            # R14 D-01: Capture requested_qty from immutable values BEFORE await
            requested_qty = pos_size / max(feat.ask, 0.01)

            entry_fill = await self.paper_exec.simulate_fill(
                sym, "buy", requested_qty,
                bid=feat.bid, ask=feat.ask, last=feat.last_price,
                asks_depth=asks_depth,
            )
            if not entry_fill or entry_fill.filled_qty <= 0:
                continue

            order_id = f"entry-{sym}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            # R14 D-01: Use captured requested_qty, not recomputed from feat
            self._persist_order({
                "order_id": order_id, "client_order_id": order_id,
                "symbol": sym, "side": "buy",
                "requested_qty": requested_qty,
                "filled_qty": entry_fill.filled_qty,
                "remaining_qty": max(requested_qty - entry_fill.filled_qty, 0),
                "avg_fill_price": entry_fill.fill_price,
                "status": entry_fill.status,
            })
            self._orders_created += 1
            self._persist_fill({
                "fill_id": f"{order_id}-fill", "order_id": order_id,
                "symbol": sym, "side": "buy",
                "quantity": entry_fill.filled_qty,
                "price": entry_fill.fill_price,
                "notional": entry_fill.fill_price * entry_fill.filled_qty,
                "fees": entry_fill.fees, "slippage_bps": entry_fill.slippage_bps,
            })
            self._fills_created += 1
            if entry_fill.status == "PARTIALLY_FILLED":
                self._partial_fills += 1

            quantity = entry_fill.filled_qty
            fill_price = entry_fill.fill_price

            # R14 S-01: Anchor hard stop to ACTUAL entry fill
            hard_stop = fill_price * (1.0 - 0.003)  # exactly -0.30%

            pos = self.account.open_position(
                sym, "long", fill_price, quantity,
                fees=entry_fill.fees, stop_loss_price=hard_stop,
                strategy_id=opp.signal.strategy_id,
            )
            if pos:
                pos_id = f"pos-{sym}"
                self.monitor.register_position(pos)
                self._positions_opened_total += 1  # R15: cumulative
                self._total_trades += 1
                self.analytics.record_allocation(
                    sym, opp.signal.strategy_id, "binance",
                    fill_price * quantity,
                )
                self._trade_log.append({
                    "symbol": sym, "side": "buy",
                    "notional": fill_price * quantity,
                    "time": datetime.now(UTC).isoformat(),
                })
                self._persist_position(pos_id, pos)
                self._persist_account()
                # R14 P-01: reconcile risk immediately after entry
                self._reconcile_risk()
        self._total_scans += 1

        # R21: Per-scan diagnostic
        logger.info(
            "strategy_diagnostic",
            scan=self._total_scans,
            evaluations=self._strategy_evaluations,
            by_strategy=self._strategy_evaluations_by_strategy.copy(),
            signals=self._total_signals,
            no_signal=self._no_signal_decisions,
            no_signal_reasons=self._no_signal_reasons.copy(),
            market_events=self._market_events_received,
            ticker=self._ticker_events_received,
            book=self._book_events_received,
        )

        # Persist trail state at runtime
        for sym in list(self.account.state.open_positions.keys()):
            self._persist_trail(f"pos-{sym}")

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
                # R16: Record the first fatal error and stop
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
            s = self.account.state
            logger.info("paper_status", equity=round(s.equity, 0), pnl=round(s.realized_pnl, 0),
                        positions=len(s.open_positions), trades=s.trade_count,
                        pub=self.publish_count, con=self.consume_count)

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
        s = self.account.state
        wall_secs = (datetime.now(UTC) - self._wall_start).total_seconds() if self._wall_start else 0
        rss_now = _get_memory_mb()
        return {
            "status": "complete",
            "duration_seconds": time.monotonic() - self._start_time,
            "wall_seconds": wall_secs,
            "initial_balance": self.initial_balance,
            "final_equity": round(s.equity, 2),
            "net_pnl": round(s.realized_pnl, 2),
            "total_fees": round(s.total_fees, 4),
            "total_slippage": round(s.total_slippage, 4),
            "total_trades": s.trade_count,
            "wins": s.win_count, "losses": s.loss_count,
            "total_signals": self._total_signals,
            "total_opportunities": self._total_opportunities,
            "risk_assessments": self._risk_assessments,
            "risk_approved": self._risk_approved,
            "risk_rejected": self._risk_rejected,
            "orders_created": self._orders_created,
            "fills_created": self._fills_created,
            "partial_fills": self._partial_fills,
            "positions_opened": self._positions_opened_total,
            "positions_closed": self._positions_closed,
            "positions_currently_open": len(s.open_positions),
            "trailing_exits": self._trailing_exits,
            "hard_stop_exits": self._hard_stop_exits,
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
            "task_count_end": len(asyncio.all_tasks()),
            "queue_depth_peak": self._queue_depth_peak,
            "mode": "PAPER", "live_trading": "DISABLED",
        }

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
