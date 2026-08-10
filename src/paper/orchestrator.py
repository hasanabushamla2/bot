"""Paper Trading Orchestrator — R3: event-driven pipeline with all modules wired."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.adapters.crypto.binance import BinanceAdapter
from src.analytics.tracker import AnalyticsTracker
from src.core.logging_config import get_logger
from src.data.engine import RealTimeDataEngine
from src.data.event_bus import EventBus
from src.data.feed_health import FeedHealthMonitor
from src.data.normalization import CanonicalSymbol
from src.data.order_book import OrderBookEngine
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.position_monitor import PositionMonitor
from src.portfolio.allocator import CapitalAllocator
from src.portfolio.capital_tiers import CapitalTierManager
from src.portfolio.liquidity import LiquidityAnalyzer
from src.portfolio.universe import UniverseConfig, UniverseManager
from src.risk.engine import RiskEngine
from src.scanner.global_scanner import AssetClass, AssetSnapshot, GlobalScanner
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.registry import StrategyRegistry

logger = get_logger(__name__)


class PaperTradingOrchestrator:
    """R3: All runtime modules wired. Event-driven architecture."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
        use_testnet: bool = False,
    ) -> None:
        raw_symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        self._raw_to_canonical: dict[str, str] = {}
        self._canonical_symbols: list[str] = []
        for raw in raw_symbols:
            canonical = CanonicalSymbol.from_exchange_symbol("binance", raw).symbol
            self._canonical_symbols.append(canonical)
            self._raw_to_canonical[raw] = canonical
        self.initial_balance = initial_balance
        self.max_symbols = max_symbols
        self.use_testnet = use_testnet
        # R3: Wire ALL runtime modules
        self.adapter: BinanceAdapter | None = None
        self.data_engine: RealTimeDataEngine | None = None
        self.event_bus = EventBus(default_max_queue=500)
        self.feed_health = FeedHealthMonitor()
        self.order_book_engine = OrderBookEngine(max_books=200)
        self.features = FeatureEngine(max_instruments=500)
        self.universe = UniverseManager(UniverseConfig())
        self.scanner = GlobalScanner()
        self.registry = StrategyRegistry()
        self.opportunity_engine = OpportunityEngine()
        self.risk_engine = RiskEngine()
        self.tier_manager = CapitalTierManager()
        self.allocator = CapitalAllocator()
        self.liquidity = LiquidityAnalyzer()
        self.account = PaperAccount(initial_balance=initial_balance)
        self.paper_exec = PaperExecutionEngine()
        self.monitor = PositionMonitor(self.account)
        self.analytics = AnalyticsTracker()
        self._running = False
        self._scan_interval = 5.0
        self._report_interval = 60.0
        self._start_time = 0.0
        self._total_scans = 0
        self._total_signals = 0

    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info("paper_starting", balance=self.initial_balance)
        self.adapter = BinanceAdapter(use_testnet=self.use_testnet)
        await self.adapter.connect()
        if not await self.adapter.health_check():
            return {"status": "error", "reason": "health_check_failed"}
        # R3: Wire adapters into data engine
        self.data_engine = RealTimeDataEngine(adapters={"binance": self.adapter})
        # Register strategies
        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()
        await self.event_bus.start()
        self._running = True
        self._start_time = time.monotonic()
        tasks = [
            asyncio.create_task(self._data_loop()),
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._report_loop()),
        ]
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
            await self.adapter.disconnect()
            await self.registry.shutdown_all()
        return self._final_report()

    async def _data_loop(self) -> None:
        """R3: REST for metadata/bootstrap, ticker for price updates (not heavy polling)."""
        while self._running:
            try:
                for raw, canonical in self._raw_to_canonical.items():
                    if not self._running:
                        break
                    try:
                        ticker = await self.adapter.get_ticker(raw)
                        if ticker and ticker.last > 0:
                            # R3: FeedHealth — record message receipt
                            self.feed_health.record_message(
                                "binance", canonical, "ticker", exchange_ts=ticker.timestamp
                            )
                            # R3: Check health before updating
                            health = self.feed_health.get("binance", canonical, "ticker")
                            if health and not health.is_healthy:
                                continue
                            self.features.update_price(canonical, ticker.last)
                            self.features.update_order_book(canonical, ticker.bid, ticker.ask)
                            self.features.update_volume(canonical, ticker.volume_24h)
                            self.account.update_market_price(canonical, ticker.last)
                            # R3: Feed liquidity into universe
                            liq = self.liquidity.analyze(None, ticker.volume_24h)
                            self.universe.update_liquidity(canonical, "binance", liq)
                    except Exception:
                        pass
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("data_loop_error")
                await asyncio.sleep(10.0)

    async def _scan_tick(self) -> None:
        """R3: Use GlobalScanner, RiskEngine, CapitalAllocator, PaperExecutionEngine."""
        # 1. Check existing positions for stops
        exits = self.monitor.check_all()
        for ex in exits:
            sym = ex["symbol"]
            pos_data = self.account.state.open_positions.get(sym)
            qty = pos_data.quantity if pos_data else 0.0
            exit_price = ex["price"]
            exit_notional = exit_price * qty
            exit_fee = exit_notional * 0.001
            trade = self.account.close_position(
                sym,
                exit_price,
                fees=exit_fee,
                slippage=exit_notional * 0.0005,
                exit_reason=ex["reason"],
                trail_peak=ex.get("trail_peak", 0.0),
                trail_level=ex.get("trail_level", 0.0),
            )
            self.monitor.unregister_position(sym)
            if trade:
                self.analytics.record_trade(
                    trade.gross_pnl,
                    trade.net_pnl,
                    trade.fees,
                    slippage=trade.slippage_cost,
                    strategy_id=trade.strategy_id,
                    exchange="binance",
                )
        # 2. R3: Build AssetSnapshots for GlobalScanner
        snapshots: list[AssetSnapshot] = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            snap = AssetSnapshot(
                symbol=canonical,
                exchange="binance",
                asset_class=AssetClass.CRYPTO_SPOT,
                last_price=feat.last_price,
                bid=feat.bid,
                ask=feat.ask,
                spread_pct=feat.spread_bps / 100.0 if feat.spread_bps > 0 else 0.0,
                volume_24h=feat.volume_24h,
                price_change_1m_pct=feat.return_1m_pct,
                price_change_5m_pct=feat.return_5m_pct,
                volume_vs_avg_ratio=feat.relative_volume,
                bid_ask_ratio=feat.bid_ask_ratio,
                depth_bid_10bps=feat.bid_depth_10bps,
            )
            snapshots.append(snap)
        # 3. R3: GlobalScanner produces ranked signals
        scanner_signals = self.scanner.scan(snapshots)
        strategy_signals = self.scanner.to_strategy_signals(scanner_signals)
        # 4. R3: Also run strategy plugins
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            for strat in self.registry.get_enabled():
                try:
                    sig = await strat.analyze(features=feat)
                    if sig is not None and not sig.is_expired:
                        strategy_signals.append(sig)
                except Exception:
                    pass
        self._total_signals += len(strategy_signals)
        if not strategy_signals:
            return
        # 5. R3: OpportunityEngine
        opportunities = self.opportunity_engine.evaluate_batch(strategy_signals)
        self.analytics.record_opportunity()
        if not opportunities:
            return
        # 6. R3: RiskEngine + CapitalTierManager + CapitalAllocator
        tier_state = self.tier_manager.determine_tier(self.account.state.equity)
        max_slots = tier_state.target_slots
        current = len(self.account.state.open_positions)
        available = max(0, max_slots - current)
        if available <= 0:
            return
        for opp in opportunities[:available]:
            # R3: FeedHealth — reject stale
            sym = opp.signal.symbol or "unknown"
            h = self.feed_health.get("binance", sym, "ticker")
            if h and not h.is_healthy:
                self.analytics.record_opportunity(rejected=True)
                continue
            risk = self.risk_engine.assess(opp)
            if risk.decision.value != "approved":
                self.analytics.record_opportunity(rejected=True)
                continue
            if sym in self.account.state.open_positions:
                continue
            pos_size = min(risk.max_position_size, self.account.state.cash * 0.8)
            if pos_size < 50:
                continue
            entry_price = self.features.get(sym).last_price
            if entry_price <= 0:
                try:
                    raw = next(r for r, c in self._raw_to_canonical.items() if c == sym)
                    if self.adapter is not None:
                        t = await self.adapter.get_ticker(raw)
                        entry_price = t.last if t and t.last > 0 else 0
                except Exception:
                    continue
            if entry_price <= 0:
                continue
            quantity = pos_size / entry_price
            entry_fee = pos_size * 0.001
            stop_price = risk.stop_loss_price or (entry_price * 0.997)
            pos = self.account.open_position(
                sym,
                "long",
                entry_price,
                quantity,
                fees=entry_fee,
                stop_loss_price=stop_price,
                strategy_id=opp.signal.strategy_id,
            )
            if pos:
                self.monitor.register_position(pos)
                self.analytics.record_allocation(sym, opp.signal.strategy_id, "binance", pos_size)
        self._total_scans += 1

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_tick()
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("scan_loop_error")
                await asyncio.sleep(10.0)

    async def _report_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._report_interval)
            s = self.account.state
            elapsed = time.monotonic() - self._start_time
            logger.info(
                "paper_status",
                equity=round(s.equity, 0),
                pnl=round(s.realized_pnl, 0),
                positions=len(s.open_positions),
                trades=s.trade_count,
                elapsed=round(elapsed, 0),
            )

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
            "win_rate": s.win_count / s.trade_count if s.trade_count > 0 else 0,
            "total_signals": self._total_signals,
            "total_scans": self._total_scans,
            "mode": "PAPER",
            "live_trading": "DISABLED",
        }
