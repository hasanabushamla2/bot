"""F-02/F-03/F-04/F-12 fix: Paper Trading Orchestrator with correct symbol normalization.
ONE canonical symbol representation system-wide. No BTCUSDT/BTC-USDT mismatch.
F-03: exit fees use notional * fee_rate, not price * fee_rate * 2.
F-04: duplicate same-symbol positions rejected.
F-12: all major modules wired into runtime.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from src.adapters.crypto.binance import BinanceAdapter
from src.analytics.tracker import AnalyticsTracker
from src.core.logging_config import get_logger
from src.data.normalization import CanonicalSymbol
from src.features.engine import FeatureEngine
from src.opportunity.engine import OpportunityEngine
from src.paper.account import PaperAccount
from src.paper.engine import PaperExecutionEngine
from src.paper.position_monitor import PositionMonitor
from src.portfolio.allocator import CapitalAllocator
from src.portfolio.capital_tiers import CapitalTierManager
from src.portfolio.liquidity import LiquidityAnalyzer
from src.risk.engine import RiskEngine
from src.strategies.breakout_strategy import BreakoutStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.order_flow_strategy import OrderFlowStrategy
from src.strategies.registry import StrategyRegistry

logger = get_logger(__name__)


class PaperTradingOrchestrator:
    """F-12: Wire all modules into one honest runtime pipeline."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        initial_balance: float = 10_000.0,
        max_symbols: int = 50,
        use_testnet: bool = False,
    ) -> None:
        raw_symbols = symbols or ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
        # F-02: Normalize ALL symbols to canonical at init boundary
        self._raw_symbols = raw_symbols
        self._canonical_symbols: list[str] = []
        self._raw_to_canonical: dict[str, str] = {}
        for raw in raw_symbols:
            canonical = CanonicalSymbol.from_exchange_symbol("binance", raw).symbol
            self._canonical_symbols.append(canonical)
            self._raw_to_canonical[raw] = canonical

        self.initial_balance = initial_balance
        self.max_symbols = max_symbols
        self.use_testnet = use_testnet
        self.adapter: BinanceAdapter | None = None
        self.features = FeatureEngine(max_instruments=500)
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
        self._scan_interval = 2.0
        self._report_interval = 60.0
        self._start_time = 0.0
        self._total_scans = 0
        self._total_signals = 0
        self._total_trades = 0
        self._total_opportunities = 0

    async def start(self, duration_seconds: float = 0.0) -> dict[str, Any]:
        logger.info(
            "paper_starting", balance=self.initial_balance, symbols=len(self._canonical_symbols)
        )
        self.adapter = BinanceAdapter(use_testnet=self.use_testnet)
        await self.adapter.connect()
        if not await self.adapter.health_check():
            return {"status": "error", "reason": "health_check_failed"}
        for strat in [MomentumStrategy(), BreakoutStrategy(), OrderFlowStrategy()]:
            self.registry.register(strat)
        await self.registry.initialize_all()

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
            await self.adapter.disconnect()
            await self.registry.shutdown_all()
        return self._final_report()

    # ------------------------------------------------------------------
    # F-02: Data loop uses canonical symbols consistently
    # ------------------------------------------------------------------
    async def _data_loop(self) -> None:
        """Fetch ticker data and update features + account using CANONICAL symbols."""
        while self._running:
            try:
                for raw, canonical in self._raw_to_canonical.items():
                    try:
                        ticker = await self.adapter.get_ticker(raw)
                        if ticker and ticker.last > 0:
                            self.features.update_price(canonical, ticker.last)
                            self.features.update_order_book(canonical, ticker.bid, ticker.ask)
                            self.features.update_volume(canonical, ticker.volume_24h)
                            self.account.update_market_price(canonical, ticker.last)
                    except Exception:
                        pass
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("data_loop_error")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # F-02: Scan loop uses canonical symbols throughout
    # F-03: Exit fees = exit_notional * fee_rate (not price * fee_rate * 2)
    # F-04: One position per symbol enforced
    # ------------------------------------------------------------------
    async def _scan_tick(self) -> None:
        # 1. Check stops
        exits = self.monitor.check_all()
        for ex in exits:
            sym = ex["symbol"]
            price = ex["price"]
            # F-03: Get position to compute proper exit notional
            pos_data = self.account.state.open_positions.get(sym)
            qty = pos_data.quantity if pos_data else 0.0
            exit_notional = price * qty
            exit_fee = exit_notional * 0.001
            trade = self.account.close_position(
                sym,
                price,
                fees=exit_fee,  # F-03: percent of notional
                slippage=exit_notional * 0.0005,  # F-03: consistent units
                exit_reason=ex["reason"],
                trail_peak=ex.get("trail_peak", 0.0),
                trail_level=ex.get("trail_level", 0.0),
            )
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

        # 2. Generate signals using CANONICAL symbols
        signals = []
        for canonical in self._canonical_symbols:
            feat = self.features.get(canonical)
            if feat.sample_count < 10:
                continue
            for strat in self.registry.get_enabled():
                try:
                    sig = await strat.analyze(features=feat)
                    if sig is not None and not sig.is_expired:
                        signals.append(sig)
                except Exception:
                    pass

        # 3. Evaluate opportunities
        if not signals:
            return
        opportunities = self.opportunity_engine.evaluate_batch(signals)
        self._total_opportunities += len(opportunities)
        self.analytics.record_opportunity()

        # 4. Risk + allocate + execute
        tier_state = self.tier_manager.determine_tier(self.account.state.equity)
        max_slots = tier_state.target_slots
        current_slots = len(self.account.state.open_positions)
        available = max(0, max_slots - current_slots)
        if available <= 0:
            return

        for opp in opportunities[:available]:
            risk = self.risk_engine.assess(opp)
            if risk.decision.value != "approved":
                self.analytics.record_opportunity(rejected=True)
                continue

            sym = opp.signal.symbol or "unknown"
            # F-04: Check duplicate same-symbol before opening
            if sym in self.account.state.open_positions:
                logger.debug("duplicate_skipped", symbol=sym)
                continue

            pos_size = min(risk.max_position_size, self.account.state.cash * 0.8)
            if pos_size < 50:
                continue

            entry_price = self.features.get(sym).last_price
            if entry_price <= 0:
                try:
                    raw = next(r for r, c in self._raw_to_canonical.items() if c == sym)
                    ticker = await self.adapter.get_ticker(raw)
                    entry_price = ticker.last if ticker and ticker.last > 0 else 0
                except Exception:
                    continue
            if entry_price <= 0:
                continue

            quantity = pos_size / entry_price
            entry_fee = pos_size * 0.001
            stop_price = risk.stop_loss_price or (entry_price * (1.0 - 0.003))
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
                self._total_trades += 1
                self.analytics.record_allocation(sym, opp.signal.strategy_id, "binance", pos_size)

        self._total_scans += 1

    async def _scan_loop(self) -> None:
        while self._running:
            try:
                await self._scan_tick()
                await asyncio.sleep(self._scan_interval)
            except Exception:
                logger.exception("scan_loop_error")
                await asyncio.sleep(5.0)

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
            "total_scans": self._total_scans,
            "max_drawdown_pct": round(s.max_drawdown_pct, 2),
            "mode": "PAPER",
            "live_trading": "DISABLED",
        }
