"""Paper Trading Orchestrator — end-to-end live-data paper trading loop.

Coordinates all subsystems into a continuous paper trading pipeline:
  Live Public Data → Features → Strategies → Opportunities →
  Risk → Allocation → Paper Execution → Position Monitor → Exit →
  Capital Release → Re-rank → Repeat.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any  # type: ignore[union-attr]

from src.adapters.crypto.binance import BinanceAdapter
from src.analytics.tracker import AnalyticsTracker
from src.core.logging_config import get_logger
from src.data.normalization import CanonicalSymbol
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.position_monitor import PositionMonitor
from src.portfolio.allocator import CapitalAllocator
from src.portfolio.capacity import CapacityEstimator
from src.portfolio.capital_tiers import CapitalTierManager
from src.portfolio.liquidity import LiquidityAnalyzer
from src.portfolio.markets import AssetQualityFilter
from src.portfolio.universe import UniverseConfig, UniverseManager
from src.risk.engine import RiskEngine
from src.strategies.base import StrategySignal
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.registry import StrategyRegistry

logger = get_logger(__name__)


class PaperTradingOrchestrator:
    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
        use_testnet: bool = False,
    ) -> None:
        self.symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        self.initial_balance = initial_balance
        self.max_symbols = max_symbols
        self.use_testnet = use_testnet
        # Core
        self.adapter: BinanceAdapter | None = None
        self.features = FeatureEngine(max_instruments=500)
        self.registry = StrategyRegistry()
        self.opportunity_engine = OpportunityEngine()
        self.risk_engine = RiskEngine()
        self.tier_manager = CapitalTierManager()
        self.allocator = CapitalAllocator()
        self.universe = UniverseManager(UniverseConfig())
        self.quality_filter = AssetQualityFilter()
        self.liquidity = LiquidityAnalyzer()
        self.capacity = CapacityEstimator()
        self.account = PaperAccount(initial_balance=initial_balance)
        self.monitor = PositionMonitor(self.account)
        self.analytics = AnalyticsTracker()
        # State
        self._running = False
        self._scan_interval = 2.0
        self._report_interval = 60.0
        self._last_report = 0.0
        self._start_time = 0.0
        self._total_scans = 0
        self._total_signals = 0
        self._total_trades = 0
        self._total_opportunities = 0

    # ------------------------------------------------------------------
    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info(
            "paper_orchestrator_starting", balance=self.initial_balance, symbols=len(self.symbols)
        )
        # Init adapter
        self.adapter = BinanceAdapter(use_testnet=self.use_testnet)
        await self.adapter.connect()
        if not await self.adapter.health_check():
            logger.error("binance_health_check_failed")
            return {"status": "error", "reason": "Binance health check failed"}
        # Init strategies
        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()
        # Dynamically discover universe
        try:
            instruments = await self.adapter.get_instruments()
            for inst in instruments[: self.max_symbols * 3]:
                self.universe.register(
                    inst.symbol,
                    inst.exchange,
                    base_asset=inst.base_asset,
                    quote_asset=inst.quote_asset,
                )
            logger.info(
                "universe_instruments_loaded", count=min(len(instruments), self.max_symbols * 3)
            )
        except Exception:
            logger.warning("instrument_discovery_failed", using_fallback=True)
        self._running = True
        self._start_time = time.monotonic()
        # Main loop
        tasks = [
            asyncio.create_task(self._data_loop()),
            asyncio.create_task(self._scan_loop()),
            asyncio.create_task(self._report_loop()),
        ]
        end_time = time.monotonic() + duration_seconds if duration_seconds > 0 else float("inf")
        try:
            while self._running and time.monotonic() < end_time:
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("paper_interrupted")
        finally:
            self._running = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.adapter.disconnect()
            await self.registry.shutdown_all()
        return self._final_report()

    # ------------------------------------------------------------------
    async def _data_loop(self) -> None:
        """Subscribe to live data for all symbols."""
        while self._running:
            try:
                for sym in self.symbols[: self.max_symbols]:
                    try:
                        ticker = await self.adapter.get_ticker(sym)
                        if ticker and ticker.last > 0:
                            canonical = CanonicalSymbol.from_exchange_symbol("binance", sym)
                            self.features.update_price(canonical.symbol, ticker.last)
                            self.features.update_order_book(
                                canonical.symbol, ticker.bid, ticker.ask
                            )
                            self.features.update_volume(canonical.symbol, ticker.volume_24h)
                            liq = self.liquidity.analyze(None, ticker.volume_24h)
                            self.universe.update_liquidity(canonical.symbol, "binance", liq)
                            self.account.update_market_price(canonical.symbol, ticker.last)
                    except Exception:
                        pass
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("data_loop_error")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    async def _scan_loop(self) -> None:
        """Main scan → evaluate → trade loop."""
        while self._running:
            try:
                await self._scan_tick()
            except Exception:
                logger.exception("scan_loop_error")
                await asyncio.sleep(5.0)

    async def _scan_tick(self) -> None:
        # 1. Check existing positions for stops
        exits = self.monitor.check_all()
        for ex in exits:
            trade = self.account.close_position(
                ex["symbol"],
                ex["price"],
                fees=ex["price"] * 0.001 * 2,
                slippage=0.0005,
                exit_reason=ex["reason"],
                trail_peak=ex.get("trail_peak", 0.0),
                trail_level=ex.get("trail_level", 0.0),
            )
            self.monitor.unregister_position(ex["symbol"])
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
                logger.info(
                    "paper_exit",
                    symbol=ex["symbol"],
                    reason=ex["reason"],
                    net_pnl=round(trade.net_pnl, 2),
                )
        # 2. Generate signals
        signals: list[StrategySignal] = []
        for sym in self.symbols[: self.max_symbols]:
            feat = self.features.get(sym)
            if feat.sample_count < 10:
                continue
            for strat in self.registry.get_enabled():
                try:
                    sig = await strat.analyze(features=feat)
                    if sig is not None and not sig.is_expired:
                        signals.append(sig)
                except Exception:
                    pass
        self._total_signals += len(signals)
        # 3. Evaluate opportunities
        if signals:
            opportunities = self.opportunity_engine.evaluate_batch(signals)
            self._total_opportunities += len(opportunities)
            self.analytics.record_opportunity()
            # 4. Risk + allocate + execute if slots available
            tier_state = self.tier_manager.determine_tier(self.account.state.equity)
            max_slots = tier_state.target_slots
            current_slots = len(self.account.state.open_positions)
            available_slots = max(0, max_slots - current_slots)
            if available_slots <= 0:
                return
            for opp in opportunities[:available_slots]:
                risk = self.risk_engine.assess(opp)
                if risk.decision.value != "approved":
                    self.analytics.record_opportunity(rejected=True)
                    continue
                pos_size = min(risk.max_position_size, self.account.state.cash * 0.8)
                if pos_size < 50:
                    continue
                sym = opp.signal.symbol or "unknown"
                entry_price = feat.last_price if hasattr(self.features, "get") else 0.0
                # Use ticker
                try:
                    adapter = self.adapter
                    if adapter is not None:
                        ticker = await adapter.get_ticker(sym)
                    entry_price = ticker.last if ticker and ticker.last > 0 else feat.last_price
                except Exception:
                    entry_price = feat.last_price
                if entry_price <= 0:
                    continue
                quantity = pos_size / entry_price
                fees = pos_size * 0.001
                stop_price = risk.stop_loss_price or (entry_price * (1 - 0.003))
                pos = self.account.open_position(
                    sym,
                    "long",
                    entry_price,
                    quantity,
                    fees=fees,
                    stop_loss_price=stop_price,
                    strategy_id=opp.signal.strategy_id,
                )
                if pos:
                    self.monitor.register_position(pos)
                    self._total_trades += 1
                    self.analytics.record_allocation(
                        sym, opp.signal.strategy_id, "binance", pos_size
                    )
                    logger.info(
                        "paper_entry",
                        symbol=sym,
                        size=round(pos_size, 2),
                        stop=round(stop_price, 2),
                    )
        self._total_scans += 1
        await asyncio.sleep(self._scan_interval)

    # ------------------------------------------------------------------
    async def _report_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._report_interval)
            self._print_status()

    def _print_status(self) -> None:
        s = self.account.state
        elapsed = time.monotonic() - self._start_time
        status = (
            f"PAPER | ${s.equity:,.0f} | P&L ${s.realized_pnl:,.0f} | "
            f"Pos {len(s.open_positions)} | Trades {s.trade_count} | "
            f"W/L {s.win_count}/{s.loss_count} | T+{elapsed:.0f}s"
        )
        logger.info("paper_status", status=status)

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
            "total_opportunities": self._total_opportunities,
            "total_scans": self._total_scans,
            "open_positions": len(s.open_positions),
            "max_drawdown_pct": round(s.max_drawdown_pct, 2),
            "mode": "PAPER",
            "live_trading": "DISABLED",
        }
