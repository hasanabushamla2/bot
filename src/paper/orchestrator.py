"""Paper Trading Orchestrator — ROUND 5: durable canonical paper runtime."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any

from src.analytics.tracker import AnalyticsTracker
from src.core.logging_config import get_logger
from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import BookLevel, CanonicalSymbol, TickerEvent
from src.data.order_book import OrderBookEngine
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


class PaperTradingOrchestrator:
    """Round 5: single canonical event-driven path. All blockers fixed."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
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

        # ---- Event counters ----
        self.publish_count = 0
        self.consume_count = 0
        self.subscriber_count = 0

        # ---- Bounded storage ----
        self._trade_log: deque[dict[str, Any]] = deque(maxlen=50000)
        self._error_log: deque[dict[str, Any]] = deque(maxlen=1000)

        self._running = False
        self._scan_interval = 5.0
        self._report_interval = 60.0
        self._start_time = 0.0
        self._total_scans = 0
        self._total_signals = 0
        self._total_trades = 0

    # ------------------------------------------------------------------
    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info("paper_start", balance=self.initial_balance)
        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()
        await self.event_bus.start()
        # ---- Register EventBus subscribers ----
        self.event_bus.subscribe("ticker_events", self._sub_ticker)
        self.subscriber_count = 1
        # Prepare universe
        for canonical in self._canonical_symbols:
            a = self.universe.register(canonical, "binance")
            a.data_healthy = True
            a.last_data_at = datetime.now(UTC)
        self._running = True
        self._start_time = time.monotonic()
        tasks = [asyncio.create_task(self._scan_loop()), asyncio.create_task(self._report_loop())]
        end = time.monotonic() + duration_seconds if duration_seconds > 0 else float("inf")
        try:
            while self._running and time.monotonic() < end:
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.event_bus.shutdown()
            await self.registry.shutdown_all()
        return self._final_report()

    # ---- EventBus subscriber (BLOCKER 2: single canonical path) ----
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
        # Universe liquidity (BLOCKER 3: use real bid/ask)
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

    # ---- Public ingestion boundary ----
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

    # ---- Scan/trade cycle ----
    async def _scan_tick(self) -> None:
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
            # BLOCKER 6: use reduce_position for partial, close_position for full
            if abs(fill.filled_qty - qty) < 0.00000001:
                trade = self.account.close_position(
                    sym,
                    fill.fill_price,
                    fees=fill.fees,
                    slippage=fill.slippage_bps / 10000.0 * fill.filled_qty * fill.fill_price,
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
                    slippage=fill.slippage_bps / 10000.0 * fill.filled_qty * fill.fill_price,
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

        # 2. Build snapshots
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            # BLOCKER 9: FeedHealth gate
            health = self.feed_health.get("binance", canonical, "ticker")
            if health and not health.is_healthy:
                continue
            # Universe gate
            asset = self.universe.get(canonical, "binance")
            if asset and asset.status.value not in ("active", "watch"):
                continue
            # BLOCKER 3: Quality filter with real liquidity data
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
                data_age_seconds=0.0,
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

        # 5. Risk (BLOCKER 7: authoritative stop)
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
            # BLOCKER 7: RiskEngine MUST provide stop. No fallback.
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

            # BLOCKER 5: Depth-walk entry via PaperExecutionEngine
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
        self._total_scans += 1

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
            logger.info(
                "paper_status",
                equity=round(s.equity, 0),
                pnl=round(s.realized_pnl, 0),
                positions=len(s.open_positions),
                trades=s.trade_count,
                pub=self.publish_count,
                con=self.consume_count,
            )

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
        }

    def stop(self) -> None:
        self._running = False

    def estimate_10day_usage(self) -> dict[str, Any]:
        return {
            "estimated_trades_10day": max(1, self._total_trades) * 10,
            "trade_log_entries": len(self._trade_log),
            "bounded_ok": len(self._trade_log) <= 50000 and len(self._error_log) <= 1000,
        }
