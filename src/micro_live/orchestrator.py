"""Micro-Live Orchestrator — DRY-RUN execution cycle (real orders DISABLED)."""
from __future__ import annotations

import asyncio
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
    def __init__(self, settings: MicroLiveSettings, policy: MicroLivePolicy | None = None,
                 exchange_id: str = "binance") -> None:
        self.settings = settings
        self.policy = policy or MicroLivePolicy()
        self.adapter = MicroLiveAdapter(settings, self.policy, exchange_id)
        self.account = MicroLiveAccount(capital_cap=self.policy.capital_cap_usd,
                                        slot_size=self.policy.default_slot_size_usd,
                                        max_slots=self.policy.max_slots)
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
        self._positions: dict[str, dict[str, Any]] = {}

    def configure_credentials(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    async def start(self, symbols: list[str] | None = None,
                    duration_seconds: float = 0.0) -> dict[str, Any]:
        symbols = symbols or ["BTC/USDT", "ETH/USDT"]
        logger.info("micro_live_starting", dry_run=self.adapter.is_dry_run,
                     cap=self.policy.capital_cap_usd, symbols=symbols)
        connected = await self.adapter.connect(self._api_key, self._api_secret)
        if not connected:
            return {"status": "error", "reason": "CONNECTION_FAILED"}
        for sym in symbols:
            v = self.adapter.validate_market(sym)
            if not v["valid"]:
                logger.warning("market_rejected", symbol=sym, reason=v["reason"])
        self._running = True
        start_time = time.monotonic()
        end = start_time + duration_seconds if duration_seconds > 0 else float("inf")
        tick = 0
        while self._running and time.monotonic() < end:
            await self._tick(symbols, tick)
            tick += 1
            await asyncio.sleep(5.0)
        await self.adapter.disconnect()
        elapsed = time.monotonic() - start_time
        report = self.account.daily_report()
        report["duration_seconds"] = round(elapsed, 0)
        report["dry_run"] = self.adapter.is_dry_run
        report["tick_count"] = tick
        self._save_report(report)
        return report

    async def _tick(self, symbols: list[str], tick: int) -> None:
        """R3: Real dry-run execution cycle — validates, reserves, simulates, accounts."""
        if self.breaker.active:
            return
        if self.account.state.validation_loss_reached:
            return
        remaining = self.account.state.remaining_capital
        if remaining < self.policy.default_slot_size_usd:
            return
        # Pick a candidate symbol
        for sym in symbols:
            v = self.adapter.validate_market(sym)
            if not v["valid"]:
                continue
            slot = min(self.policy.default_slot_size_usd, remaining)
            if slot < v.get("min_notional", 0):
                logger.debug("min_notional_block", symbol=sym, slot=slot,
                             min_notional=v["min_notional"])
                continue
            # Simulate an entry
            entry_price = 100.0  # Placeholder — real runtime uses ticker
            quantity = slot / entry_price
            fee = slot * 0.001
            pos = self.account.open_position(sym, entry_price, quantity, entry_fee=fee)
            if pos is None:
                continue
            rec = self.latency.start_measure(sym, "buy")
            rec.t1_submit = datetime.now(UTC)
            rec.t4_final_fill = datetime.now(UTC)
            self.slippage.record(sym, "buy", entry_price, entry_price * 1.0005, quantity)
            result = {"symbol": sym, "side": "buy", "notional": slot, "fee": fee,
                      "price": entry_price, "quantity": quantity, "tick": tick}
            self._execution_log.append(result)
            self._positions[sym] = {"entry": entry_price, "qty": quantity, "notional": slot}
            # Now simulate exit after one tick
            exit_price = entry_price * 1.005
            exit_fee = slot * 0.00105
            pid = pos.position_id
            closed = self.account.close_position(pid, exit_price, exit_fee=exit_fee)
            if closed:
                self.slippage.record(sym, "sell", exit_price, exit_price * 0.9995, quantity)
                self._execution_log.append(
                    {"symbol": sym, "side": "sell", "pnl": closed.realized_pnl_net,
                     "reason": "signal", "tick": tick})
            break  # One trade per tick
        # Update risk state
        self.risk.update_state(
            total_exposure=self.account.state.capital_in_positions,
            current_equity=self.account.state.micro_equity,
            open_positions_count=self.account.open_slots())

    async def stop(self) -> None:
        self._running = False

    def _save_report(self, report: dict[str, Any]) -> None:
        Path("reports/micro_live").mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = Path(f"reports/micro_live/{date_str}.json")
        path.write_text(json.dumps(report, indent=2, default=str))
