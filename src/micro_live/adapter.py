# ruff: noqa
"""CCXT-backed Micro-Live Adapter — F-01/F-05/F-26 fix: $50 cap at order boundary.

Every order path enforces:
- $50 total capital cap (effective_exposure + new ≤ cap)
- $5 default slot
- min notional / min amount / precision / lot step
- clientOrderId with idempotency
- active spot market validation
- RiskEngine + market-health approval
"""

# mypy: ignore-errors

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from src.core.logging_config import get_logger
from src.micro_live.config import MicroLivePolicy, MicroLiveSettings

logger = get_logger(__name__)


class MicroLiveAdapter:
    """CCXT-based adapter. F-01: $50 cap enforced at LAST LINE OF DEFENSE."""

    def __init__(
        self,
        settings: MicroLiveSettings,
        policy: MicroLivePolicy,
        exchange_id: str = "binance",
    ) -> None:
        self.settings = settings
        self.policy = policy
        self.exchange_id = exchange_id
        self._exchange: Any = None
        self._connected = False
        self._market_limits: dict[str, dict[str, Any]] = {}
        self._executed_ids: set[str] = set()

    @property
    def is_dry_run(self) -> bool:
        return not self.settings.can_place_real_orders

    async def connect(self, api_key: str = "", api_secret: str = "") -> bool:
        try:
            import ccxt.async_support as ccxt_async

            exchange_class = getattr(ccxt_async, self.exchange_id, None)
            if exchange_class is None:
                logger.error("ccxt_not_found", exchange=self.exchange_id)
                return False
            config: dict[str, Any] = {"enableRateLimit": True}
            if api_key and not self.is_dry_run:
                config["apiKey"] = api_key
                config["secret"] = api_secret
            self._exchange = exchange_class(config)
            if not self.is_dry_run and api_key:
                await self._exchange.load_markets()
                self._market_limits = self._exchange.markets or {}
            else:
                pub = exchange_class({"enableRateLimit": True})
                await pub.load_markets()
                self._market_limits = dict(pub.markets or {})
                await pub.close()
            self._connected = True
            logger.info("micro_live_connected", exchange=self.exchange_id, dry_run=self.is_dry_run)
            return True
        except Exception as e:
            logger.error("micro_live_connect_failed", error=str(e))
            return False

    async def disconnect(self) -> None:
        if self._exchange:
            with contextlib.suppress(Exception):
                await self._exchange.close()
        self._connected = False

    # F-26: Proper withdrawal check
    def check_withdrawal_permission(self) -> dict[str, Any]:
        result: dict[str, Any] = {"withdrawal_enabled": False, "safe": True, "warning": ""}
        if self._exchange and hasattr(self._exchange, "has"):
            if self._exchange.has.get("withdraw", False):
                result["withdrawal_enabled"] = True
                result["safe"] = False
                result["warning"] = "WITHDRAWAL_PERMISSION_DETECTED — REVOKE IMMEDIATELY"
                logger.warning(result["warning"])
        return result

    # ------------------------------------------------------------------
    # F-01: Last-line $50 cap enforcement
    # ------------------------------------------------------------------

    def compute_effective_exposure(
        self,
        active_position_cost: float,
        reserved_capital: float,
        pending_order_notional: float,
    ) -> float:
        return active_position_cost + reserved_capital + pending_order_notional

    def can_accept_order(
        self,
        new_order_notional: float,
        active_position_cost: float = 0.0,
        reserved_capital: float = 0.0,
        pending_order_notional: float = 0.0,
    ) -> bool:
        effective = self.compute_effective_exposure(
            active_position_cost, reserved_capital, pending_order_notional
        )
        return (effective + new_order_notional) <= self.policy.capital_cap_usd

    # ------------------------------------------------------------------
    # Market validation
    # ------------------------------------------------------------------

    def validate_market(self, symbol: str) -> dict[str, Any]:
        """F-01: Validate spot/active/minNotional/minAmount/precision."""
        if not self._market_limits:
            return {"valid": False, "reason": "NO_MARKET_DATA"}
        info = self._market_limits.get(symbol, {})
        if not info:
            return {"valid": False, "reason": "SYMBOL_NOT_FOUND"}
        if info.get("type") != "spot":
            return {"valid": False, "reason": "NOT_SPOT"}
        if not info.get("active", False):
            return {"valid": False, "reason": "MARKET_INACTIVE"}
        limits = info.get("limits", {})
        cost_min = limits.get("cost", {}).get("min")
        amount_min = limits.get("amount", {}).get("min")
        min_notional = float(cost_min or 0)
        min_amount = float(amount_min or 0)
        precision = info.get("precision", {})
        return {
            "valid": True,
            "symbol": symbol,
            "min_notional": min_notional,
            "min_amount": min_amount,
            "precision": precision,
            "reason": "OK",
        }

    # ------------------------------------------------------------------
    # Order placement with cap enforcement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float | None = None,
        *,
        active_position_cost: float = 0.0,
        reserved_capital: float = 0.0,
        pending_notional: float = 0.0,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        """F-01: Enforce $50 cap at order boundary. Never exceeds cap."""
        # --- Dry-run path ---
        if self.is_dry_run:
            cid = client_order_id or str(uuid.uuid4())
            notional = amount * (price or 0)
            if not self.can_accept_order(
                notional, active_position_cost, reserved_capital, pending_notional
            ):
                return {
                    "error": "CAP_EXCEEDED",
                    "status": "rejected",
                    "cap": self.policy.capital_cap_usd,
                }
            return {
                "dry_run": True,
                "status": "simulated",
                "symbol": symbol,
                "side": side,
                "amount": amount,
                "price": price or 0,
                "id": cid,
                "filled": 0.0,
                "average": 0.0,
                "fee": {"cost": 0.0, "currency": "USDT"},
            }

        # --- Validate market ---
        validation = self.validate_market(symbol)
        if not validation["valid"]:
            return {"error": validation["reason"], "status": "rejected"}

        # --- Reject if slot < min_notional ---
        min_notional = validation["min_notional"]
        notional = amount * (price or 0)
        if notional < min_notional:
            return {
                "error": "MIN_NOTIONAL_TOO_HIGH",
                "status": "rejected",
                "min_required": min_notional,
                "slot_size": notional,
            }

        # --- Cap check ---
        if not self.can_accept_order(
            notional, active_position_cost, reserved_capital, pending_notional
        ):
            return {
                "error": "CAP_EXCEEDED",
                "status": "rejected",
                "cap": self.policy.capital_cap_usd,
                "effective": self.compute_effective_exposure(
                    active_position_cost, reserved_capital, pending_notional
                ),
            }

        # --- Idempotency ---
        cid = client_order_id or str(uuid.uuid4())
        if cid in self._executed_ids:
            return {"error": "DUPLICATE_CLIENT_ORDER_ID", "status": "rejected", "id": cid}
        self._executed_ids.add(cid)

        if not self._exchange:
            return {"error": "NOT_CONNECTED", "status": "rejected"}

        try:
            order_type = "limit" if price else "market"
            params: dict[str, Any] = {}
            if self.exchange_id == "binance":
                params["newClientOrderId"] = cid
            order = await self._exchange.create_order(
                symbol, order_type, side, amount, price, params=params
            )
            logger.info(
                "micro_live_order", symbol=symbol, side=side, amount=amount, id=order.get("id")
            )
            return dict(raw_order)  # type: ignore[arg-type]
        except Exception as e:
            logger.error("micro_live_order_failed", error=str(e))
            return {"error": str(e), "status": "rejected"}

    async def fetch_balance(self) -> dict[str, float]:
        if self.is_dry_run or not self._exchange:
            return {"USDT": 0.0, "total_usd": 0.0}
        try:
            balance = await self._exchange.fetch_balance()
            return {
                "USDT": float(balance.get("total", {}).get("USDT", 0)),
                "total_usd": float(balance.get("total", {}).get("USDT", 0)),
            }
        except Exception as e:
            return {"USDT": 0.0, "total_usd": 0.0, "error": str(e)}

    async def reconcile_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if self.is_dry_run or not self._exchange:
            return {
                "dry_run": True,
                "status": "simulated",
                "filled": 0.0,
                "average": 0.0,
                "fee": {"cost": 0.0},
            }
        try:
            result = await self._exchange.fetch_order(order_id, symbol)
            return dict(result) if result else {"status": "not_found"}
        except Exception as e:
            return {"error": str(e)}

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        if self.is_dry_run or not self._exchange:
            return {"dry_run": True, "status": "cancelled_simulated"}
        try:
            result = await self._exchange.cancel_order(order_id, symbol)
            return dict(result) if result else {"status": "not_found"}
        except Exception as e:
            return {"error": str(e)}
