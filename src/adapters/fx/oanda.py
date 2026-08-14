"""Read-only OANDA Practice pricing adapter for FX and spot gold.

Credentials are read from environment variables.  This module has no order
creation methods and cannot place trades.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class OandaPrice:
    instrument: str
    bid: float
    ask: float
    timestamp: datetime
    tradeable: bool

    @property
    def canonical_symbol(self) -> str:
        return self.instrument.replace("_", "-")

    @property
    def spread_bps(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 10_000.0 if mid > 0 and self.ask > self.bid else float("inf")


class OandaPracticePricingAdapter:
    """Pricing-only connector for an OANDA practice account."""

    PRACTICE_BASE = "https://api-fxpractice.oanda.com"
    LIVE_BASE = "https://api-fxtrade.oanda.com"

    def __init__(
        self,
        token: str | None = None,
        account_id: str | None = None,
        environment: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token = token or os.environ.get("OANDA_API_TOKEN", "")
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        self.environment = environment or os.environ.get("OANDA_ENV", "practice")
        self._client = client
        self._owns_client = client is None

    async def connect(self) -> None:
        if not self.token or not self.account_id:
            raise RuntimeError("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID locally")
        if self._client is None:
            base = self.PRACTICE_BASE if self.environment == "practice" else self.LIVE_BASE
            self._client = httpx.AsyncClient(
                base_url=base,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=httpx.Timeout(20.0),
            )

    async def disconnect(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None

    async def fetch_prices(
        self,
        instruments: list[str] | None = None,
    ) -> dict[str, OandaPrice]:
        if self._client is None:
            raise RuntimeError("OANDA adapter is not connected")
        requested = instruments or [
            "XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD",
            "USD_CAD", "USD_CHF", "NZD_USD", "EUR_GBP", "EUR_JPY",
        ]
        response = await self._client.get(
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": ",".join(requested)},
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        result: dict[str, OandaPrice] = {}
        for raw in payload.get("prices", []):
            bids = raw.get("bids") or []
            asks = raw.get("asks") or []
            if not bids or not asks:
                continue
            instrument = str(raw.get("instrument", ""))
            try:
                timestamp = datetime.fromisoformat(str(raw.get("time", "")).replace("Z", "+00:00"))
            except ValueError:
                timestamp = datetime.now(UTC)
            result[instrument] = OandaPrice(
                instrument=instrument,
                bid=float(bids[0]["price"]),
                ask=float(asks[0]["price"]),
                timestamp=timestamp,
                tradeable=bool(raw.get("tradeable", False)),
            )
        return result
