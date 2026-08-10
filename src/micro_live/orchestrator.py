"""Micro-Live Orchestrator — $50 real-money validation loop (SPOT ONLY)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.core.logging_config import get_logger
from src.micro_live.account import MicroLiveAccount
from src.micro_live.adapter import MicroLiveAdapter
from src.micro_live.config import MicroLivePolicy, MicroLiveSettings
from src.micro_live.fees import RealFeeService
from src.micro_live.monitors import (
    CircuitBreakerState,
    LatencyMonitor,
    SlippageMonitor,
    StopExecutionAudit,
)
from src.risk.engine import RiskEngine

logger = get_logger(__name__)


class MicroLiveOrchestrator:
    def __init__(
        self,
        settings: MicroLiveSettings,
        policy: MicroLivePolicy | None = None,
        exchange_id: str = "binance",
    ) -> None:
        self.settings = settings
        self.policy = policy or MicroLivePolicy()
        self.exchange_id = exchange_id
        self.adapter = MicroLiveAdapter(settings, self.policy, exchange_id)
        self.account = MicroLiveAccount(
            capital_cap=self.policy.capital_cap_usd,
            slot_size=self.policy.default_slot_size_usd,
            max_slots=self.policy.max_slots,
        )
        self.fees = RealFeeService()
        self.latency = LatencyMonitor()
        self.slippage = SlippageMonitor()
        self.stop_audit = StopExecutionAudit()
        self.breaker = CircuitBreakerState()
        self.risk = RiskEngine()
        self._running = False
        self._execution_log: list[dict[str, Any]] = []
        self._api_key = ""
        self._api_secret = ""

    def configure_credentials(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    async def start(
        self, symbols: list[str] | None = None, duration_seconds: float = 0.0
    ) -> dict[str, Any]:
        symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        logger.info(
            "micro_live_starting",
            dry_run=self.adapter.is_dry_run,
            cap=self.policy.capital_cap_usd,
            symbols=symbols,
        )
        connected = await self.adapter.connect(self._api_key, self._api_secret)
        if not connected:
            return {"status": "error", "reason": "CONNECTION_FAILED"}
        # Validate markets
        for sym in symbols:
            validation = self.adapter.validate_market(sym)
            if not validation["valid"]:
                logger.warning(
                    "market_rejected_micro_live", symbol=sym, reason=validation["reason"]
                )
        # Check withdrawal permissions
        if self.adapter.check_withdrawal_permission():
            logger.warning("WITHDRAWAL_PERMISSION_DETECTED")
        # Fetch real balance if armed
        if not self.adapter.is_dry_run:
            balance = await self.adapter.fetch_balance()
            logger.info("micro_live_balance", usdt=balance.get("USDT", 0))
        # Simple loop
        self._running = True
        start_time = time.monotonic()
        end_time = start_time + duration_seconds if duration_seconds > 0 else float("inf")
        tick = 0
        while self._running and time.monotonic() < end_time:
            await self._tick(symbols, tick)
            tick += 1
            await self._sleep(10.0)
        await self.adapter.disconnect()
        elapsed = time.monotonic() - start_time
        report = self.account.daily_report()
        report["duration_seconds"] = round(elapsed, 0)
        report["dry_run"] = self.adapter.is_dry_run
        report["execution_log_count"] = len(self._execution_log)
        self._save_report(report)
        return report

    async def _tick(self, symbols: list[str], tick: int) -> None:
        if self.breaker.active:
            logger.warning("micro_live_circuit_breaker_active", reason=self.breaker.reason)
            return
        # Check validation loss
        if self.account.check_validation_loss():
            self.breaker.trip("VALIDATION_LOSS_LIMIT")
        if self.breaker.active:
            return
        # Only process if we have capacity
        if self.account.state.remaining_capital < self.policy.min_slot_size_usd:
            return

    async def stop(self) -> None:
        self._running = False

    async def _sleep(self, seconds: float) -> None:
        import asyncio

        await asyncio.sleep(seconds)

    def simulate_entry(self, symbol: str, entry_price: float, quantity: float) -> dict[str, Any]:
        """Simulate a micro-live entry for testing/dry-run."""
        notional = entry_price * quantity
        fee = notional * 0.001
        rec = self.latency.start_measure(symbol, "buy")
        rec.t1_submit = datetime.now(UTC)
        rec.t2_ack = datetime.now(UTC)
        rec.t4_final_fill = datetime.now(UTC)
        rec.total_execution_ms = 50.0
        self.slippage.record(symbol, "buy", entry_price, entry_price * 1.0005, quantity)
        self.account.reserve_capital(notional)
        self.account.execute_buy(notional, fee)
        result = {
            "symbol": symbol,
            "side": "buy",
            "notional": notional,
            "fee": fee,
            "price": entry_price,
            "quantity": quantity,
        }
        self._execution_log.append(result)
        return result

    def simulate_exit(
        self,
        symbol: str,
        exit_price: float,
        entry_price: float,
        quantity: float,
        reason: str = "signal",
    ) -> dict[str, Any]:
        """Simulate a micro-live exit."""
        entry_notional = entry_price * quantity
        exit_notional = exit_price * quantity
        fee = exit_notional * 0.001
        gross_pnl = exit_notional - entry_notional
        pnl = gross_pnl - fee - entry_notional * 0.001
        self.account.execute_sell(exit_notional, fee, pnl)
        self.slippage.record(symbol, "sell", exit_price, exit_price * 0.9995, quantity)
        if reason == "hard_stop":
            target_stop = entry_price * 0.997
            self.stop_audit.record(symbol, entry_price, target_stop, exit_price, fee, 100.0)
            self.account.state.hard_stop_exits += 1
        elif reason == "trail_hit":
            self.account.state.trail_exits += 1
        result = {
            "symbol": symbol,
            "side": "sell",
            "exit_notional": exit_notional,
            "fee": fee,
            "pnl": pnl,
            "reason": reason,
        }
        self._execution_log.append(result)
        return result

    def _save_report(self, report: dict[str, Any]) -> None:
        Path("reports/micro_live").mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = Path(f"reports/micro_live/{date_str}.json")
        path.write_text(json.dumps(report, indent=2, default=str))
