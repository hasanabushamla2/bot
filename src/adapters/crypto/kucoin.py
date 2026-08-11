"""KuCoin Public Data Adapter — REST-based real market data.

Used when Binance is geo-blocked. Public endpoints only, no API keys.
Feeds real prices/bid/ask/depth through the existing orchestrator pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class KuCoinPublicAdapter:
    """Public market data from KuCoin REST API. No API keys needed."""

    BASE = "https://api.kucoin.com"

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._running = False

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(base_url=self.BASE, timeout=httpx.Timeout(10.0))
        self._running = True
        logger.info("kucoin_connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("kucoin_disconnected")

    async def health_check(self) -> bool:
        try:
            if not self._http:
                return False
            r = await self._http.get("/api/v1/status")
            return r.status_code == 200
        except Exception:
            return False

    async def get_ticker(self, raw_symbol: str) -> dict[str, Any] | None:
        """Get level-1 order book (best bid/ask + last price)."""
        try:
            if not self._http:
                return None
            r = await self._http.get(
                f"/api/v1/market/orderbook/level1?symbol={raw_symbol}"
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            d = data["data"]
            return {
                "bid": float(d.get("bestBid", 0)),
                "ask": float(d.get("bestAsk", 0)),
                "last": float(d.get("price", 0)),
                "bid_size": float(d.get("bestBidSize", 0)),
                "ask_size": float(d.get("bestAskSize", 0)),
                "timestamp": datetime.now(UTC),
            }
        except Exception:
            return None

    async def get_order_book(self, raw_symbol: str, depth: int = 20) -> dict[str, Any] | None:
        """Get full order book depth."""
        try:
            if not self._http:
                return None
            r = await self._http.get(
                f"/api/v1/market/orderbook/level2_20?symbol={raw_symbol}"
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            d = data["data"]
            bids = [(float(b[0]), float(b[1])) for b in d.get("bids", [])[:depth]]
            asks = [(float(a[0]), float(a[1])) for a in d.get("asks", [])[:depth]]
            return {
                "bids": bids,
                "asks": asks,
                "timestamp": datetime.now(UTC),
            }
        except Exception:
            return None

    async def get_24h_stats(self, raw_symbol: str) -> dict[str, Any] | None:
        """Get 24h statistics including real volume. Returns {volume_24h_usd, volume_24h_base, ...}."""
        try:
            if not self._http:
                return None
            r = await self._http.get(
                f"/api/v1/market/stats?symbol={raw_symbol}"
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            d = data["data"]
            vol_usd = float(d.get("volValue", 0) or 0)
            vol_base = float(d.get("vol", 0) or 0)
            return {
                "volume_24h_base": vol_base,
                "volume_24h_usd": vol_usd,
                "high_24h": float(d.get("high", 0) or 0),
                "low_24h": float(d.get("low", 0) or 0),
                "change_pct": float(d.get("changeRate", 0) or 0) * 100,
            }
        except Exception:
            return None

    async def get_server_time(self) -> datetime:
        try:
            if not self._http:
                return datetime.now(UTC)
            r = await self._http.get("/api/v1/timestamp")
            data = r.json()
            ms = data.get("data", int(asyncio.get_event_loop().time() * 1000))
            return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)
        except Exception:
            return datetime.now(UTC)
