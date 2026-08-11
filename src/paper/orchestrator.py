"""Paper Trading Orchestrator — ROUND 12: persistence wired, accounting fixed."""

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
from src.paper.account import PaperAccount
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

# ── Runtime lease helpers ────────────────────────────────────────────
LEASE_HEARTBEAT_SEC = 15.0
LEASE_EXPIRY_SEC = 45.0


class _RuntimeLease:
    """Database-backed single-owner lease for a paper account."""

    def __init__(self, persist: PaperPersistence, account_id: str, owner_id: str) -> None:
        self._persist = persist
        self._account_id = account_id
        self._owner_id = owner_id
        self._acquired = False

    def try_acquire(self) -> bool:
        """Attempt to acquire or refresh the lease."""
        with self._persist._tx() as c:
            now = datetime.now(UTC).isoformat()
            # Check if there's an existing valid lease by another owner
            row = c.execute(
                "SELECT owner_id, expires_at FROM runtime_lease WHERE account_id=?",
                (self._account_id,),
            ).fetchone()
            if row is not None:
                if row["owner_id"] != self._owner_id:
                    if row["expires_at"] > now:
                        return False  # Another owner's lease is still valid
            # Acquire or refresh
            new_expiry = datetime.now(UTC).timestamp() + LEASE_EXPIRY_SEC
            new_expiry_str = datetime.fromtimestamp(new_expiry, tz=UTC).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO runtime_lease(account_id,owner_id,acquired_at,heartbeat_at,expires_at) "
                "VALUES(?,?,?,?,?)",
                (self._account_id, self._owner_id, now, now, new_expiry_str),
            )
        self._acquired = True
        return True

    def heartbeat(self) -> bool:
        """Extend the lease expiry. Returns False if lease was lost."""
        with self._persist._tx() as c:
            now = datetime.now(UTC).isoformat()
            row = c.execute(
                "SELECT owner_id, expires_at FROM runtime_lease WHERE account_id=?",
                (self._account_id,),
            ).fetchone()
            if row is None or row["owner_id"] != self._owner_id:
                return False
            new_expiry = datetime.now(UTC).timestamp() + LEASE_EXPIRY_SEC
            new_expiry_str = datetime.fromtimestamp(new_expiry, tz=UTC).isoformat()
            c.execute(
                "UPDATE runtime_lease SET heartbeat_at=?, expires_at=? WHERE account_id=?",
                (now, new_expiry_str, self._account_id),
            )
        return True

    def release(self) -> None:
        """Release the lease."""
        with self._persist._tx() as c:
            c.execute(
                "DELETE FROM runtime_lease WHERE account_id=? AND owner_id=?",
                (self._account_id, self._owner_id),
            )
        self._acquired = False


class PaperTradingOrchestrator:
    """Round 12: persistence wired at runtime, accounting fixed, lease + feed health."""

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

        # ---- All runtime modules ----
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

        # ---- Event counters ----
        self.publish_count = 0
        self.consume_count = 0
        self.subscriber_count = 0

        # ---- Bounded storage ----
        self._trade_log: deque[dict[str, Any]] = deque(maxlen=50000)
        self._error_log: deque[dict[str, Any]] = deque(maxlen=1000)
        self._accepting_new = False  # Start closed; open after restore
        # Signal handlers
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

        # Metrics for soak
        self._lease_heartbeat_success = 0
        self._lease_heartbeat_errors = 0
        self._persistence_writes = 0
        self._persistence_reads = 0
        self._persistence_errors = 0

    # ------------------------------------------------------------------
    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        """R12: Full startup sequence with persistence, restore, lease, health."""
        logger.info("paper_start", balance=self.initial_balance)

        # 1. Initialize persistence
        self._persist = PaperPersistence(self._db_path)
        self._persist.connect()

        # Add lease table to schema if not present
        self._persist._ensure_lease_table()

        # 2. Acquire runtime ownership
        session_id = self._get_experiment_id()
        owner_id = f"{session_id}-{uuid.uuid4().hex[:8]}"
        self._lease = _RuntimeLease(self._persist, "paper-account-1", owner_id)
        if not self._lease.try_acquire():
            logger.error("lease_acquire_failed", reason="Another owner holds the lease")
            self._persist.close()
            return {"status": "LEASE_CONFLICT", "reason": "Another process owns this account"}
        logger.info("lease_acquired", owner_id=owner_id)

        # 3. Start session
        self._persist.start_session(session_id, self._get_commit_sha())

        # 4. Restore durable state
        self._restore_state()
        self._persistence_reads += 1

        # 5. Register strategies
        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()

        # 6. Start EventBus
        await self.event_bus.start()
        self.event_bus.subscribe("ticker_events", self._sub_ticker)
        self.subscriber_count = 1

        # 7. Prepare universe
        for canonical in self._canonical_symbols:
            a = self.universe.register(canonical, "binance")
            a.data_healthy = True
            a.last_data_at = datetime.now(UTC)

        # 8. NOW accept new entries
        self._accepting_new = True

        # 9. Start background loops
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
            # Graceful shutdown
            self._running = False
            self._accepting_new = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.event_bus.shutdown()
            await self.registry.shutdown_all()
            # Persist final state
            self._persist_final_state()
            if self._lease:
                self._lease.release()
            self._persist.close()
        return self._final_report()

    # ------------------------------------------------------------------
    #  R12: Startup state restore
    # ------------------------------------------------------------------
    def _restore_state(self) -> None:
        """Restore account, positions, trails, risk from persistence."""
        if self._persist is None:
            return
        # Restore account
        saved = self._persist.load_account()
        if saved is not None:
            self.account.state.cash = saved.get("cash", self.initial_balance)
            self.account.state.allocated = saved.get("allocated", 0)
            self.account.state.realized_pnl = saved.get("realized_pnl", 0)
            self.account.state.total_fees = saved.get("total_fees", 0)
            self.account.state.total_slippage = saved.get("total_slippage", 0)
            self.account.state.trade_count = saved.get("trade_count", 0)
            self.account.state.win_count = saved.get("win_count", 0)
            self.account.state.loss_count = saved.get("loss_count", 0)
            self.account.state.peak_equity = saved.get("peak_equity", self.initial_balance)
            self.account.state.max_drawdown_pct = saved.get("max_drawdown_pct", 0)
            logger.info(
                "state_restored_account",
                cash=saved.get("cash"),
                allocated=saved.get("allocated"),
                realized=round(saved.get("realized_pnl", 0), 4),
            )

        # Restore open positions
        positions = self._persist.load_open_positions()
        for p_dict in positions:
            from src.paper.account import PaperPosition

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
            pos.trail_peak = p_dict.get("entry_price", 0)  # Will be overridden by trail
            self.account.state.open_positions[p_dict["symbol"]] = pos

            # Restore trail state
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
            # Register with monitor
            self.monitor.register_position(pos)

        # Restore risk state
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

        # Recompute derived state
        self.account.state.unrealized_pnl = sum(
            p.unrealized_pnl for p in self.account.state.open_positions.values()
        )
        logger.info(
            "state_restored",
            positions=len(positions),
            cash=round(self.account.state.cash, 2),
            allocated=round(self.account.state.allocated, 2),
        )

    # ------------------------------------------------------------------
    #  R12: Periodic health supervisor
    # ------------------------------------------------------------------
    async def _health_supervisor(self) -> None:
        """Periodically check feed health even when no messages arrive."""
        while self._running:
            await asyncio.sleep(5.0)
            unhealthy = self.feed_health.check_all()
            if unhealthy:
                for fh in unhealthy:
                    logger.warning(
                        "feed_unhealthy",
                        exchange=fh.exchange,
                        symbol=fh.symbol,
                        stream=fh.stream_type,
                        status=fh.status,
                        age=fh.stale_duration_seconds,
                    )
                # If ALL feeds are unhealthy, stop accepting new entries
                all_feeds = self.feed_health.get_all()
                if all_feeds and all(not f.is_healthy for f in all_feeds):
                    self._accepting_new = False
                    logger.error("all_feeds_unhealthy_closing_gate")

    # ------------------------------------------------------------------
    #  R12: Lease heartbeat loop
    # ------------------------------------------------------------------
    async def _lease_heartbeat_loop(self) -> None:
        """Periodically heartbeat the runtime lease."""
        while self._running and self._lease:
            await asyncio.sleep(LEASE_HEARTBEAT_SEC)
            if self._lease and not self._lease.heartbeat():
                self._lease_heartbeat_errors += 1
                logger.error("lease_heartbeat_lost")
                self._accepting_new = False
            else:
                self._lease_heartbeat_success += 1

    # ------------------------------------------------------------------
    #  R12: Persistence helpers
    # ------------------------------------------------------------------
    def _persist_account(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist.save_account(
                {
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
                }
            )
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_account_error")

    def _persist_position(self, pos_id: str, pos: Any) -> None:
        if self._persist is None:
            return
        try:
            self._persist.save_position(
                {
                    "position_id": pos_id,
                    "symbol": pos.symbol,
                    "direction": pos.direction,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "entry_notional": pos.notional,
                    "cost_basis": pos.notional + pos.fees_paid,
                    "entry_fee": pos.fees_paid,
                    "stop_loss_price": pos.stop_loss_price,
                    "strategy_id": pos.strategy_id,
                }
            )
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_position_error")

    def _persist_trail(self, pos_id: str) -> None:
        if self._persist is None:
            return
        try:
            sym = self._get_symbol_by_pos_id(pos_id)
            if sym:
                trail_state = self.monitor.get_trail_state(sym)
                if trail_state:
                    self._persist.save_trail(
                        pos_id,
                        {
                            "trail_peak": trail_state.get("peak_price", 0),
                            "trail_level": trail_state.get("trail_level", 0),
                            "trail_activated": trail_state.get("activated", False),
                            "exit_intent_active": trail_state.get("exit_intent", False),
                        },
                    )
                    self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_trail_error")

    def _persist_risk(self) -> None:
        if self._persist is None:
            return
        try:
            rs = self.risk_engine.get_state()
            self._persist.save_risk(
                {
                    "total_exposure": rs.get("total_exposure", 0),
                    "per_market_exposure": rs.get("per_market_exposure", {}),
                    "per_strategy_exposure": rs.get("per_strategy_exposure", {}),
                    "strategy_position_counts": rs.get("strategy_position_counts", {}),
                    "peak_equity": rs.get("peak_equity", 0),
                    "consecutive_losses": rs.get("consecutive_losses", 0),
                    "circuit_breaker_active": rs.get("circuit_breaker_active", False),
                }
            )
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_risk_error")

    def _persist_closed_trade(self, trade: Any) -> None:
        if self._persist is None:
            return
        try:
            trade_id = f"closed-{trade.symbol}-{trade.exit_time.isoformat()}-{uuid.uuid4().hex[:8]}"
            self._persist.save_closed_trade(
                {
                    "trade_id": trade_id,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "quantity": trade.quantity,
                    "gross_pnl": trade.gross_pnl,
                    "fees": trade.fees,
                    "slippage_cost": trade.slippage_cost,
                    "net_pnl": trade.net_pnl,
                    "return_pct": trade.return_pct,
                    "exit_reason": trade.exit_reason,
                    "strategy_id": trade.strategy_id,
                    "entry_time": trade.entry_time.isoformat(),
                    "exit_time": trade.exit_time.isoformat(),
                }
            )
            self._persistence_writes += 1
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_closed_trade_error")

    def _persist_final_state(self) -> None:
        """Persist everything during shutdown."""
        if self._persist is None:
            return
        try:
            self._persist_account()
            for pos_id, pos in self.account.state.open_positions.items():
                pid = self._get_or_create_pos_id(pos_id)
                self._persist_position(pid, pos)
                self._persist_trail(pid)
            self._persist_risk()
            # Persist all closed trades
            for trade in self.account.state.closed_trades:
                self._persist_closed_trade(trade)
            self._persist.audit("GRACEFUL_SHUTDOWN", "Clean stop")
            session_id = self._get_experiment_id()
            self._persist.end_session(session_id, "COMPLETED")
            logger.info(
                "persist_final_state",
                positions=len(self.account.state.open_positions),
                closed_trades=len(self.account.state.closed_trades),
            )
        except Exception:
            self._persistence_errors += 1
            logger.exception("persist_final_state_error")

    def _get_or_create_pos_id(self, symbol: str) -> str:
        """Get existing position ID or create one."""
        # Position IDs are symbol-based for simplicity
        return f"pos-{symbol}"

    def _get_symbol_by_pos_id(self, pos_id: str) -> str:
        """Reverse lookup of symbol from position ID."""
        for sym in self.account.state.open_positions:
            if self._get_or_create_pos_id(sym) == pos_id:
                return sym
        return ""

    # ------------------------------------------------------------------
    # EventBus subscriber
    # ------------------------------------------------------------------
    async def _sub_ticker(self, event: Any) -> None:
        """Consume ticker events and update all downstream state."""
        self.consume_count += 1
        sym = str(getattr(event, "symbol", ""))
        last = float(getattr(event, "last", 0))
        bid = float(getattr(event, "bid", last * 0.9999))
        ask = float(getattr(event, "ask", last * 1.0001))
        vol = float(getattr(event, "volume_24h", 0))
        if last <= 0:
            return
        # Feed health
        self.feed_health.record_message("binance", sym, "ticker", exchange_ts=datetime.now(UTC))
        # Order book
        book = self.order_book_engine.get_or_create("binance", sym)
        if bid > 0:
            book.bids.apply_snapshot([BookLevel(bid, 1.0)])
        if ask > 0:
            book.asks.apply_snapshot([BookLevel(ask, 1.0)])
        book.last_update_time = datetime.now(UTC)
        # Features
        self.features.update_price(sym, last)
        self.features.update_order_book(sym, bid, ask)
        self.features.update_volume(sym, vol)
        # Universe liquidity
        liq = self.liquidity.analyze(None, vol)
        liq.bid = bid
        liq.ask = ask
        liq.spread_pct = (ask - bid) / ((bid + ask) / 2) * 100 if bid > 0 else 0
        liq.depth_10bps = vol / last / 100 if last > 0 else 10.0
        liq.liquidity_score = 0.85
        self.universe.update_liquidity(sym, "binance", liq)
        # Mark to market
        self.account.update_market_price(sym, last)
        self.analytics.record_equity(self.account.state.equity)

    # ------------------------------------------------------------------
    # Public ingestion boundary
    # ------------------------------------------------------------------
    def process_ticker(
        self, raw_symbol: str, bid: float, ask: float, last: float, volume_24h: float = 0.0
    ) -> None:
        """Normalize and publish ticker to EventBus. No direct state mutation."""
        canonical = self._raw_to_canonical.get(raw_symbol.upper(), raw_symbol.upper())
        parts = canonical.split("-") if "-" in canonical else [canonical[:3], canonical[3:]]
        ticker_event = TickerEvent.create(
            "binance",
            CanonicalSymbol("binance", parts[0], parts[-1]),
            bid,
            ask,
            last,
            volume_24h=volume_24h,
        )
        with contextlib.suppress(RuntimeError):
            asyncio.ensure_future(self.event_bus.publish(ticker_event))
        self.publish_count += 1

    # ------------------------------------------------------------------
    # Scan/trade cycle
    # ------------------------------------------------------------------
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
            bid = self._get_bid(sym)
            fill = await self.paper_exec.simulate_fill(
                sym, "sell", qty, bid=bid, ask=ex["price"], last=ex["price"]
            )
            if fill is None or fill.filled_qty <= 0:
                continue
            # R12: fill_price already embeds slippage — do NOT pass additional slippage to account
            if abs(fill.filled_qty - qty) < 0.00000001:
                trade = self.account.close_position(
                    sym,
                    fill.fill_price,
                    fees=fill.fees,
                    slippage=0.0,  # R12: slippage embedded in fill_price
                    exit_reason=ex["reason"],
                    trail_peak=ex.get("trail_peak", 0.0),
                    trail_level=ex.get("trail_level", 0.0),
                )
            else:
                trade = self.account.reduce_position(
                    sym,
                    fill.fill_price,
                    fill.filled_qty,
                    fees=fill.fees,
                    slippage=0.0,  # R12: slippage embedded in fill_price
                    exit_reason=ex["reason"],
                )
            if sym not in self.account.state.open_positions:
                self.monitor.unregister_position(sym)
            if trade:
                self._total_trades += 1
                self.analytics.record_trade(
                    trade.gross_pnl,
                    trade.net_pnl,
                    trade.fees,
                    slippage=trade.slippage_cost,
                    strategy_id=trade.strategy_id,
                    exchange="binance",
                )
                self._trade_log.append(
                    {
                        "symbol": sym,
                        "reason": ex["reason"],
                        "pnl": trade.net_pnl,
                        "time": datetime.now(UTC).isoformat(),
                    }
                )
                # R12: Persist account, trade, position state after exit
                self._persist_account()
                self._persist_closed_trade(trade)
                pid = self._get_or_create_pos_id(sym)
                if self._persist is not None:
                    if sym in self.account.state.open_positions:
                        self._persist_position(pid, self.account.state.open_positions[sym])
                    else:
                        self._persist.delete_position(pid)

        # 2. Build snapshots
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            # R12: FeedHealth gate — use authoritative is_healthy
            health = self.feed_health.get("binance", canonical, "ticker")
            if health and not health.is_healthy:
                continue
            # Universe gate
            asset = self.universe.get(canonical, "binance")
            if asset and asset.status.value not in ("active", "watch"):
                continue
            # Quality filter with real liquidity data
            book = self.order_book_engine.get_book("binance", canonical)
            liq = self.liquidity.analyze(None, feat.volume_24h)
            if book:
                liq.bid = book.best_bid
                liq.ask = book.best_ask
                liq.spread_pct = book.spread_bps / 100.0 if book.spread_bps > 0 else 0.05
                liq.depth_10bps = book.depth_within_bps(10)
                liq.liquidity_score = 0.85
            qr = self.quality_filter.assess(
                canonical,
                "binance",
                liquidity=liq,
                volume_24h=feat.volume_24h,
                spread_pct=liq.spread_pct,
                data_age_seconds=max(0.0, (datetime.now(UTC) - feat.updated_at).total_seconds()),
                daily_trades=5000,
            )
            if not qr.qualified:
                self.analytics.record_opportunity(rejected=True)
                continue
            snapshots.append(
                AssetSnapshot(
                    symbol=canonical,
                    exchange="binance",
                    asset_class=AssetClass.CRYPTO_SPOT,
                    last_price=feat.last_price,
                    bid=feat.bid,
                    ask=feat.ask,
                    spread_pct=liq.spread_pct,
                    volume_24h=feat.volume_24h,
                    price_change_1m_pct=feat.return_1m_pct,
                    price_change_5m_pct=feat.return_5m_pct,
                    volume_vs_avg_ratio=max(1.0, feat.relative_volume),
                    bid_ask_ratio=feat.bid_ask_ratio,
                    depth_bid_10bps=liq.depth_10bps,
                )
            )
        self.analytics.record_opportunity()
        if not snapshots:
            return

        # 3. Scanner → signals
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
                    pass
        self._total_signals += len(strategy_signals)
        if not strategy_signals:
            return

        # 4. Opportunity
        opportunities = self.opportunity_engine.evaluate_batch(strategy_signals)
        if not opportunities:
            return

        # 5. Risk
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
            risk = self.risk_engine.assess(opp)
            if risk.decision.value != "approved":
                self.analytics.record_opportunity(rejected=True)
                continue
            if risk.stop_loss_price is None:
                self.analytics.record_opportunity(rejected=True)
                continue
            if sym in self.account.state.open_positions:
                continue
            # Allocator sizing
            feat = self.features.get(sym)
            cap = PositionCapacity(
                symbol=sym,
                strategy_id=opp.signal.strategy_id,
                max_efficient_size=min(risk.max_position_size, self.account.state.cash * 0.2),
                is_viable=True,
            )
            decisions = self.allocator.allocate(pf_state, [(opp, risk, cap)])
            if not decisions or not decisions[0].is_allocated:
                continue
            pos_size = decisions[0].allocated_capital
            if pos_size < 50:
                continue

            # Depth-walk entry via PaperExecutionEngine
            entry_fill = await self.paper_exec.simulate_fill(
                sym,
                "buy",
                pos_size / max(feat.ask, 0.01),
                bid=feat.bid,
                ask=feat.ask,
                last=feat.last_price,
            )
            if not entry_fill or entry_fill.filled_qty <= 0:
                continue
            quantity = entry_fill.filled_qty
            pos = self.account.open_position(
                sym,
                "long",
                entry_fill.fill_price,
                quantity,
                fees=entry_fill.fees,
                stop_loss_price=risk.stop_loss_price,
                strategy_id=opp.signal.strategy_id,
            )
            if pos:
                pos_id = self._get_or_create_pos_id(sym)
                self.monitor.register_position(pos)
                self._total_trades += 1
                self.analytics.record_allocation(
                    sym, opp.signal.strategy_id, "binance", entry_fill.fill_price * quantity
                )
                self._trade_log.append(
                    {
                        "symbol": sym,
                        "side": "buy",
                        "notional": entry_fill.fill_price * quantity,
                        "time": datetime.now(UTC).isoformat(),
                    }
                )
                # R12: Persist position, account, risk on organic entry
                self._persist_position(pos_id, pos)
                self._persist_account()
                self._persist_risk()
        self._total_scans += 1

    # ------------------------------------------------------------------
    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_tick()
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("scan_loop_error")
                self._error_log.append(
                    {"error": "scan_loop", "time": datetime.now(UTC).isoformat()}
                )
                await asyncio.sleep(10.0)

    async def _report_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._report_interval)
            s = self.account.state
            # Docker health heartbeat file
            self._touch_heartbeat()
            logger.info(
                "paper_status",
                equity=round(s.equity, 0),
                pnl=round(s.realized_pnl, 0),
                positions=len(s.open_positions),
                trades=s.trade_count,
                pub=self.publish_count,
                con=self.consume_count,
                persist_w=self._persistence_writes,
                persist_e=self._persistence_errors,
                lease_ok=self._lease_heartbeat_success,
            )

    def _touch_heartbeat(self) -> None:
        """Write Docker healthcheck heartbeat file."""
        try:
            hb_dir = os.path.dirname(self._db_path)
            hb_path = os.path.join(hb_dir, ".heartbeat")
            Path(hb_path).parent.mkdir(parents=True, exist_ok=True)
            Path(hb_path).touch()
        except Exception:
            pass

    # ------------------------------------------------------------------
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
            "wins": s.win_count,
            "losses": s.loss_count,
            "win_rate": s.win_count / s.trade_count if s.trade_count else 0,
            "total_signals": self._total_signals,
            "total_scans": self._total_scans,
            "publish_count": self.publish_count,
            "consume_count": self.consume_count,
            "subscriber_count": self.subscriber_count,
            "mode": "PAPER",
            "live_trading": "DISABLED",
            "persistence_writes": self._persistence_writes,
            "persistence_errors": self._persistence_errors,
            "lease_heartbeat_success": self._lease_heartbeat_success,
            "lease_heartbeat_errors": self._lease_heartbeat_errors,
        }

    def stop(self) -> None:
        self._running = False
        self._accepting_new = False

    def _handle_signal(self) -> None:
        logger.info("shutdown_signal_received")
        self._accepting_new = False
        self._running = False

    def _get_experiment_id(self) -> str:
        return os.environ.get(
            "PAPER_EXPERIMENT_ID", f"paper-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
        )

    def _get_commit_sha(self) -> str:
        try:
            import subprocess

            return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()[:8]
        except Exception:
            return "unknown"

    def estimate_10day_usage(self) -> dict[str, Any]:
        return {
            "estimated_trades_10day": max(1, self._total_trades) * 10,
            "trade_log_entries": len(self._trade_log),
            "bounded_ok": len(self._trade_log) <= 50000 and len(self._error_log) <= 1000,
        }
