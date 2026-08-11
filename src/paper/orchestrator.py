"""Paper Trading Orchestrator — ROUND 13: all 24 blockers closed."""

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

from src.analytics.tracker import AnalyticsTracker
from src.core.logging_config import get_logger
from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import BookLevel, CanonicalSymbol, TickerEvent
from src.data.order_book import OrderBookEngine
from src.db.persist import PaperPersistence
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import ClosedTrade, PaperAccount, PaperPosition
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


class _RuntimeLease:
    """R13: Atomic lease acquisition via BEGIN IMMEDIATE."""

    def __init__(self, persist: PaperPersistence, account_id: str, owner_id: str) -> None:
        self._persist = persist
        self._account_id = account_id
        self._owner_id = owner_id
        self._acquired = False

    def try_acquire(self) -> bool:
        """R13: Atomic lease acquisition using BEGIN IMMEDIATE."""
        if self._persist._conn is None:
            return False
        with self._persist._tx() as c:
            # SQLite: BEGIN IMMEDIATE taken before SELECT prevents races
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
    """Round 13: all 24 auditor blockers closed."""

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

        self._trade_log: deque[dict[str, Any]] = deque(maxlen=50000)
        self._error_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self._accepting_new = False
        self._health_recovery_counter: dict[str, int] = {}

        try:
            signal.signal(signal.SIGTERM, lambda s, f: self._handle_signal())
            signal.signal(signal.SIGINT, lambda s, f: self._handle_signal())
        except Exception:
            pass

        self._running = False
        self._scan_interval = 5.0
        self._report_interval = 60.0
        self._start_time = 0.0
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
        self._positions_closed = 0
        self._trailing_exits = 0
        self._hard_stop_exits = 0
        self._lease_heartbeat_success = 0
        self._lease_heartbeat_errors = 0
        self._persistence_writes = 0
        self._persistence_reads = 0
        self._persistence_errors = 0
        self._exceptions = 0

    # ══════════════════════════════════════════════════════════════════
    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info("paper_start", balance=self.initial_balance)

        self._persist = PaperPersistence(self._db_path)
        self._persist.connect()
        self._persist._ensure_lease_table()

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
    #  R13: Startup state restore with closed trades restore
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
            logger.info("state_restored_account", cash=s.cash)

        # R13: Restore closed trades into RAM (bounded recent history)
        db_trades = self._persist.load_closed_trades()
        for t_dict in db_trades[:200]:  # bounded
            ct = ClosedTrade(
                symbol=t_dict["symbol"],
                direction=t_dict.get("direction", "long"),
                entry_price=t_dict["entry_price"],
                exit_price=t_dict["exit_price"],
                quantity=t_dict["quantity"],
                gross_pnl=t_dict.get("gross_pnl", 0),
                fees=t_dict.get("fees", 0),
                slippage_cost=t_dict.get("slippage_cost", 0),
                net_pnl=t_dict.get("net_pnl", 0),
                return_pct=t_dict.get("return_pct", 0),
                exit_reason=t_dict.get("exit_reason", ""),
                strategy_id=t_dict.get("strategy_id", ""),
            )
            self.account.state.closed_trades.append(ct)

        # R13: Restore positions — register_position first, THEN restore trail
        positions = self._persist.load_open_positions()
        for p_dict in positions:
            pos = PaperPosition(
                symbol=p_dict["symbol"],
                direction=p_dict.get("direction", "long"),
                entry_price=p_dict["entry_price"],
                quantity=p_dict["quantity"],
                notional=p_dict.get("entry_notional", p_dict["entry_price"] * p_dict["quantity"]),
                stop_loss_price=p_dict.get("stop_loss_price", 0),
                fees_paid=p_dict.get("entry_fee", 0),
                strategy_id=p_dict.get("strategy_id", ""),
            )
            self.account.state.open_positions[p_dict["symbol"]] = pos
            self.monitor.register_position(pos)

            # R13 FIX: now restore trail AFTER register_position to avoid overwrite
            trail = self._persist.load_trail(p_dict["position_id"])
            if trail:
                saved_trail = {
                    "symbol": p_dict["symbol"],
                    "direction": p_dict.get("direction", "long"),
                    "entry_price": p_dict["entry_price"],
                    "peak_price": trail.get("trail_peak", p_dict["entry_price"]),
                    "trail_level": trail.get("trail_level", 0),
                    "activated": bool(trail.get("trail_activated")),
                    "exit_intent": bool(trail.get("exit_intent_active")),
                }
                self.monitor.restore_trail_state(saved_trail)
                # Apply trail state to position
                pos.trail_peak = trail.get("trail_peak", p_dict["entry_price"])
                pos.trail_activated = bool(trail.get("trail_activated"))
                pos.trail_level = trail.get("trail_level", 0)

        # Restore risk
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
    #  R13: Health supervisor with recovery
    # ══════════════════════════════════════════════════════════════════
    async def _health_supervisor(self) -> None:
        stabilization_ticks = 3
        while self._running:
            await asyncio.sleep(5.0)
            unhealthy = self.feed_health.check_all()
            all_feeds = self.feed_health.get_all()
            if all_feeds and all(not f.is_healthy for f in all_feeds):
                self._accepting_new = False
                self._health_recovery_counter.clear()
                logger.error("all_feeds_unhealthy_closing_gate")
            elif unhealthy:
                for fh in unhealthy:
                    logger.warning("feed_unhealthy", symbol=fh.symbol, stream=fh.stream_type)
            else:
                # All feeds healthy — allow recovery after stabilization
                if not self._accepting_new and self._lease and self._running:
                    key = "global"
                    self._health_recovery_counter[key] = self._health_recovery_counter.get(key, 0) + 1
                    if self._health_recovery_counter[key] >= stabilization_ticks:
                        self._accepting_new = True
                        logger.info("health_recovered_gate_reopened")
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
    #  R13: Persistence helpers with order/fill memory
    # ══════════════════════════════════════════════════════════════════
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

    def _persist_risk(self) -> None:
        if self._persist is None:
            return
        try:
            rs = self.risk_engine.get_state()
            # R13: Ensure exposure reflects actual open positions
            exp = self.account.state.allocated
            strat_counts: dict[str, int] = {}
            per_strat: dict[str, float] = {}
            for pos in self.account.state.open_positions.values():
                sid = pos.strategy_id or "unknown"
                strat_counts[sid] = strat_counts.get(sid, 0) + 1
                per_strat[sid] = per_strat.get(sid, 0) + pos.notional
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
        # R13: Deterministic trade_id — no random suffix
        trade_id = f"ct-{trade.symbol}-{trade.exit_time.strftime('%Y%m%d%H%M%S')}-{trade.entry_price:.0f}-{trade.exit_price:.0f}"
        if self._persist.closed_trade_exists(trade_id):
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
            self._persist_risk()
            # R13: Persist closed trades only if not already persisted (idempotency)
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
    #  R13: Event bus subscriber — ticker only (book is separate)
    # ══════════════════════════════════════════════════════════════════
    async def _sub_ticker(self, event: Any) -> None:
        self.consume_count += 1
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

    # ══════════════════════════════════════════════════════════════════
    #  R13: Public ingestion — ticker + order book
    # ══════════════════════════════════════════════════════════════════
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
        """R13: Ingest real order-book depth into OrderBookEngine."""
        canonical = self._raw_to_canonical.get(raw_symbol.upper(), raw_symbol.upper())
        book = self.order_book_engine.get_or_create("binance", canonical)
        if bids:
            book.bids.apply_snapshot([BookLevel(p, q) for p, q in bids[:50]])
        if asks:
            book.asks.apply_snapshot([BookLevel(p, q) for p, q in asks[:50]])
        book.last_update_time = datetime.now(UTC)
        self.feed_health.record_message("binance", canonical, "book", exchange_ts=datetime.now(UTC))

    # ══════════════════════════════════════════════════════════════════
    #  R13: Scan/trade cycle — real depth, trail persistence, order/fill
    # ══════════════════════════════════════════════════════════════════
    async def _scan_tick(self) -> None:
        if not self._accepting_new:
            return

        # 1. Check stops/trails — use real book depth for exits
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

            # R13: Persist order + fill
            self._persist_order({
                "order_id": order_id, "client_order_id": order_id,
                "symbol": sym, "side": "sell", "requested_qty": qty,
                "filled_qty": fill.filled_qty, "remaining_qty": fill.remaining_qty,
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
                self._positions_closed += 0
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
                # R13: Persist risk after exit
                self._persist_risk()

        # 2. Build snapshots
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            # R13: Check both ticker AND book health
            ticker_health = self.feed_health.get("binance", canonical, "ticker")
            book_health = self.feed_health.get("binance", canonical, "book")
            if ticker_health and not ticker_health.is_healthy:
                continue
            # Book health gate (only if book feed registered)
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
                try:
                    sig = await strat.analyze(features=feat)  # type: ignore[call-arg]
                    if sig and not sig.is_expired:
                        strategy_signals.append(sig)
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

        # 5. Risk — R13: update state with real position data
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

            # R13: Use real asks-depth for BUY execution
            book = self.order_book_engine.get_book("binance", sym)
            asks_depth = ([(lv[0], lv[1]) for lv in book.asks.levels[:20]] if book and book.asks else None)
            bids_depth = ([(lv[0], lv[1]) for lv in book.bids.levels[:20]] if book and book.bids else None)

            entry_fill = await self.paper_exec.simulate_fill(
                sym, "buy", pos_size / max(feat.ask, 0.01),
                bid=feat.bid, ask=feat.ask, last=feat.last_price,
                asks_depth=asks_depth, bids_depth=bids_depth,
            )
            if not entry_fill or entry_fill.filled_qty <= 0:
                continue

            order_id = f"entry-{sym}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            self._persist_order({
                "order_id": order_id, "client_order_id": order_id,
                "symbol": sym, "side": "buy",
                "requested_qty": pos_size / max(feat.ask, 0.01),
                "filled_qty": entry_fill.filled_qty,
                "remaining_qty": entry_fill.remaining_qty,
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
            pos = self.account.open_position(
                sym, "long", entry_fill.fill_price, quantity,
                fees=entry_fill.fees, stop_loss_price=risk.stop_loss_price,
                strategy_id=opp.signal.strategy_id,
            )
            if pos:
                pos_id = f"pos-{sym}"
                self.monitor.register_position(pos)
                self._total_trades += 1
                self.analytics.record_allocation(
                    sym, opp.signal.strategy_id, "binance",
                    entry_fill.fill_price * quantity,
                )
                self._trade_log.append({
                    "symbol": sym, "side": "buy",
                    "notional": entry_fill.fill_price * quantity,
                    "time": datetime.now(UTC).isoformat(),
                })
                self._persist_position(pos_id, pos)
                self._persist_account()
                self._persist_risk()
        self._total_scans += 1

        # R13: Persist trail state at runtime on every scan (not just shutdown)
        for sym in list(self.account.state.open_positions.keys()):
            self._persist_trail(f"pos-{sym}")

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_tick()
                await asyncio.sleep(self._scan_interval)
            except Exception:
                self._exceptions += 1
                logger.exception("scan_loop_error")
                self._error_log.append({"error": "scan_loop", "time": datetime.now(UTC).isoformat()})
                await asyncio.sleep(10.0)

    async def _report_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._report_interval)
            self._touch_heartbeat()
            self._persist_risk()
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

    def _get_ask(self, symbol: str) -> float:
        feat = self.features.get(symbol)
        return feat.ask if feat.ask > 0 else 0.0

    def _final_report(self) -> dict[str, Any]:
        s = self.account.state
        return {
            "status": "complete",
            "duration_seconds": time.monotonic() - self._start_time,
            "initial_balance": self.initial_balance,
            "final_equity": round(s.equity, 2),
            "net_pnl": round(s.realized_pnl, 2),
            "total_fees": round(s.total_fees, 4),
            "total_slippage": round(s.total_slippage, 4),
            "total_trades": s.trade_count,
            "wins": s.win_count, "losses": s.loss_count,
            "win_rate": s.win_count / s.trade_count if s.trade_count else 0,
            "total_signals": self._total_signals,
            "total_scans": self._total_scans,
            "total_opportunities": self._total_opportunities,
            "risk_assessments": self._risk_assessments,
            "risk_approved": self._risk_approved,
            "risk_rejected": self._risk_rejected,
            "orders_created": self._orders_created,
            "fills_created": self._fills_created,
            "partial_fills": self._partial_fills,
            "positions_opened": len(s.open_positions),
            "positions_closed": self._positions_closed,
            "trailing_exits": self._trailing_exits,
            "hard_stop_exits": self._hard_stop_exits,
            "publish_count": self.publish_count,
            "consume_count": self.consume_count,
            "persistence_writes": self._persistence_writes,
            "persistence_errors": self._persistence_errors,
            "lease_heartbeat_success": self._lease_heartbeat_success,
            "lease_heartbeat_errors": self._lease_heartbeat_errors,
            "exceptions": self._exceptions,
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
