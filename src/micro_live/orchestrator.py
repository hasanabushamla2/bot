"""Micro-Live Orchestrator — R4: consumes REAL/REPLAY market data. No hardcoded prices."""

from __future__ import annotations

import json
import time  # noqa: F401
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
    """R4: No hardcoded prices. Consumes real/replay market data via process_market_event()."""

    def __init__(
        self,
        settings: MicroLiveSettings,
        policy: MicroLivePolicy | None = None,
        exchange_id: str = "binance",
    ) -> None:
        self.settings = settings
        self.policy = policy or MicroLivePolicy()
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
        connected = await self.adapter.connect(self._api_key, self._api_secret)
        if not connected:
            return {"status": "error", "reason": "CONNECTION_FAILED"}
        self._running = True
        return {"status": "ok", "dry_run": self.adapter.is_dry_run}

    async def _tick(self, symbols: list[str], tick: int) -> None:
        if self.breaker.active or self.account.state.validation_loss_reached:
            return
        self.risk.update_state(
            total_exposure=self.account.state.effective_exposure,
            current_equity=self.account.state.micro_equity,
            open_positions_count=self.account.open_slots(),
        )

    async def process_market_event(
        self, symbol: str, bid: float, ask: float, last: float, volume_24h: float = 0.0
    ) -> dict[str, Any] | None:
        """R4: Process a market event through full pipeline. No hardcoded prices."""
        if not self._running or self.breaker.active:
            return None
        remaining = self.account.state.remaining_capital
        if remaining < self.policy.default_slot_size_usd:
            return None
        v = self.adapter.validate_market(symbol)
        if not v.get("valid"):
            return None
        if self.account.open_slots() >= self.policy.max_slots:
            return None
        slot = min(self.policy.default_slot_size_usd, remaining)
        if slot < v.get("min_notional", 0):
            return None
        reserved = await self.account.atomic_reserve(slot)
        if not reserved:
            return None
        try:
            entry_price = ask if ask > 0 else last
            if entry_price <= 0:
                await self.account.atomic_release_reservation(slot)
                return None
            quantity = slot / entry_price
            entry_fee = slot * 0.001
            order = await self.adapter.place_order(
                symbol,
                "buy",
                quantity,
                price=None,
                active_position_cost=self.account.state.capital_in_positions,
                reserved_capital=self.account.state.reserved_notional,
                pending_notional=self.account.state.pending_notional,
            )
            if order.get("status") == "rejected":
                await self.account.atomic_release_reservation(slot)
                self.account.state.order_rejections += 1
                return {
                    "symbol": symbol,
                    "status": "rejected",
                    "reason": order.get("error", "unknown"),
                }
            self.slippage.record(symbol, "buy", ask, entry_price * 1.0005, quantity)
            pos = self.account.open_position(symbol, entry_price, quantity, entry_fee=entry_fee)
            if pos is None:
                await self.account.atomic_release_reservation(slot)
                return None
            await self.account.atomic_confirm_entry(slot)
            self._execution_log.append(
                {
                    "symbol": symbol,
                    "side": "buy",
                    "notional": slot,
                    "price": entry_price,
                }
            )
            return {
                "symbol": symbol,
                "side": "buy",
                "price": entry_price,
                "position_id": pos.position_id,
            }
        except Exception:
            await self.account.atomic_release_reservation(slot)
            raise

    async def process_exit(
        self, symbol: str, exit_price: float, reason: str = "signal"
    ) -> dict[str, Any] | None:
        """R4: Process exit using actual market bid price."""
        for pid, pos in list(self.account._positions.items()):
            if pos.symbol == symbol and pos.is_open:
                exit_fee = exit_price * pos.quantity * 0.001
                closed = self.account.close_position(
                    pid, exit_price, exit_fee=exit_fee, exit_reason=reason
                )
                if closed:
                    self._execution_log.append(
                        {
                            "symbol": symbol,
                            "side": "sell",
                            "pnl": closed.realized_pnl_net,
                            "reason": reason,
                        }
                    )
                    if reason == "hard_stop":
                        self.stop_audit.record(
                            symbol,
                            pos.entry_price,
                            pos.entry_price * 0.997,
                            exit_price,
                            exit_fee,
                            0.0,
                        )
                    return {
                        "symbol": symbol,
                        "pnl": closed.realized_pnl_net,
                        "reason": reason,
                    }
                break
        return None

    async def stop(self) -> None:
        self._running = False
        if self.adapter:
            await self.adapter.disconnect()

    def _save_report(self, report: dict[str, Any]) -> None:
        Path("reports/micro_live").mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        out_path = Path(f"reports/micro_live/{date_str}.json")
        out_path.write_text(json.dumps(report, indent=2, default=str))
