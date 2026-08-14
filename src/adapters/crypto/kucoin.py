"""KuCoin Public Data Adapter — R26: dynamic universe scanner.

Public endpoints only, no API keys.
Feeds real prices/bid/ask/depth through the existing orchestrator pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx

from src.core.logging_config import get_logger

logger = get_logger(__name__)

STABLECOINS = {"USDC", "DAI", "BUSD", "TUSD", "USDP", "USDD", "FDUSD", "USDT", "UST", "USDE", "PYUSD"}
LEVERAGE_KW = ("3L", "3S", "UP", "DOWN", "BEAR", "BULL", "2L", "2S", "5L", "5S")
PRIORITY_SYMBOLS = {
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT", "ADA-USDT",
    "DOGE-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT", "MATIC-USDT", "UNI-USDT",
    "ARB-USDT", "OP-USDT", "SUI-USDT", "NEAR-USDT", "APT-USDT", "ATOM-USDT",
    "FIL-USDT", "LTC-USDT", "ETC-USDT", "INJ-USDT", "TIA-USDT", "SEI-USDT",
    "RUNE-USDT", "RNDR-USDT", "WIF-USDT", "JUP-USDT", "BONK-USDT", "PEPE-USDT",
    "WLD-USDT", "STRK-USDT", "ENA-USDT", "OM-USDT", "FTM-USDT", "ALGO-USDT",
    "ICP-USDT", "VET-USDT", "GRT-USDT", "AAVE-USDT", "MKR-USDT", "SNX-USDT",
    "COMP-USDT", "CRV-USDT", "LDO-USDT", "SAND-USDT", "MANA-USDT", "AXS-USDT",
    "EGLD-USDT", "QNT-USDT", "FLOW-USDT", "MINA-USDT", "IMX-USDT", "STX-USDT",
    "KAS-USDT", "TAO-USDT", "FET-USDT", "AGIX-USDT", "TRX-USDT", "TON-USDT",
    "SHIB-USDT", "FLOKI-USDT", "ORDI-USDT", "SATS-USDT", "NOT-USDT", "ZRO-USDT",
    "EIGEN-USDT", "CFX-USDT", "APE-USDT", "AXL-USDT", "BLUR-USDT", "DYDX-USDT",
    "ENS-USDT", "GALA-USDT", "GMX-USDT", "HBAR-USDT", "MAGIC-USDT",
    "OCEAN-USDT", "PENDLE-USDT", "PYTH-USDT", "RDNT-USDT", "SSV-USDT", "STG-USDT",
    "SUPER-USDT", "SUSHI-USDT", "SYN-USDT", "TRB-USDT", "UMA-USDT", "WOO-USDT",
    "YFI-USDT", "ZRX-USDT",
}


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
        try:
            if not self._http:
                return None
            r = await self._http.get(f"/api/v1/market/orderbook/level1?symbol={raw_symbol}")
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
        try:
            if not self._http:
                return None
            r = await self._http.get(f"/api/v1/market/orderbook/level2_20?symbol={raw_symbol}")
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            d = data["data"]
            bids = [(float(b[0]), float(b[1])) for b in d.get("bids", [])[:depth]]
            asks = [(float(a[0]), float(a[1])) for a in d.get("asks", [])[:depth]]
            return {"bids": bids, "asks": asks, "timestamp": datetime.now(UTC)}
        except Exception:
            return None

    async def get_24h_stats(self, raw_symbol: str) -> dict[str, Any] | None:
        try:
            if not self._http:
                return None
            r = await self._http.get(f"/api/v1/market/stats?symbol={raw_symbol}")
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            d = data["data"]
            return {
                "volume_24h_base": float(d.get("vol", 0) or 0),
                "volume_24h_usd": float(d.get("volValue", 0) or 0),
                "high_24h": float(d.get("high", 0) or 0),
                "low_24h": float(d.get("low", 0) or 0),
                "change_pct": float(d.get("changeRate", 0) or 0) * 100,
            }
        except Exception:
            return None

    async def get_all_symbols(self) -> list[dict[str, Any]]:
        """Fetch all trading symbols from KuCoin."""
        try:
            if not self._http:
                return []
            r = await self._http.get("/api/v1/symbols")
            if r.status_code != 200:
                return []
            data = r.json()
            if data.get("code") != "200000":
                return []
            raw_data = data.get("data", [])
            return raw_data if isinstance(raw_data, list) else []
        except Exception:
            return []

    async def get_all_tickers(self) -> dict[str, dict[str, Any]] | None:
        """Get ticker for ALL symbols in one call."""
        try:
            if not self._http:
                return None
            r = await self._http.get("/api/v1/market/allTickers")
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "200000":
                return None
            tickers = data["data"]["ticker"]
            return {
                t["symbol"]: {
                    "last": float(t.get("last", 0) or 0),
                    "bid": float(t.get("buy", 0) or 0),
                    "ask": float(t.get("sell", 0) or 0),
                    "volume_24h_usd": float(t.get("volValue", 0) or 0),
                    "change_pct": float(t.get("changeRate", 0) or 0) * 100,
                }
                for t in tickers
                if float(t.get("volValue", 0) or 0) > 0
            }
        except Exception:
            return None

    def filter_liquid_usdt_pairs(
        self, symbols: list[dict[str, Any]], min_volume_usd: float = 100_000
    ) -> list[str]:
        """Return eligible USDT symbols from exchange metadata only.

        Volume is deliberately not claimed here: KuCoin's symbol metadata does
        not contain a contemporaneous quote-volume field.  Call
        :meth:`rank_liquid_usdt_pairs` with ``get_all_tickers`` output before
        running a live feed so the ``min_volume_usd`` contract is actually
        enforced.
        """
        del min_volume_usd  # retained for compatibility with older callers
        result: list[str] = []
        for s in symbols:
            sym = str(s.get("symbol", ""))
            base = str(s.get("baseCurrency", ""))
            if s.get("quoteCurrency") != "USDT":
                continue
            if not s.get("enableTrading", False):
                continue
            if base in STABLECOINS:
                continue
            if any(kw in sym for kw in LEVERAGE_KW):
                continue
            result.append(sym)
        priority = [symbol for symbol in result if symbol in PRIORITY_SYMBOLS]
        rest = [symbol for symbol in result if symbol not in PRIORITY_SYMBOLS]
        filtered = priority + rest
        logger.info(
            "kucoin_eligible_usdt_metadata",
            total=len(symbols),
            eligible=len(filtered),
            priority=len(priority),
        )
        return filtered

    def rank_liquid_usdt_pairs(
        self,
        symbols: list[dict[str, Any]],
        tickers: dict[str, dict[str, Any]],
        *,
        min_volume_usd: float = 100_000.0,
        max_symbols: int = 300,
        max_spread_bps: float = 35.0,
    ) -> list[str]:
        """Select a feed-budget-compatible, liquid universe from live data.

        The old runner subscribed to every metadata-eligible pair, but only
        refreshed 25 books every three seconds.  With thousands of symbols,
        most books were stale before a full cycle completed.  This selector is
        intentionally a *market-data capacity* control, not a trade-count
        target: it keeps a compact, liquid set whose books can be refreshed
        within the stale-data budget.
        """
        if max_symbols < 0:
            return []
        eligible = self.filter_liquid_usdt_pairs(symbols)
        ranked: list[tuple[int, float, str]] = []
        for symbol in eligible:
            ticker = tickers.get(symbol)
            if not ticker:
                continue
            try:
                volume = float(ticker.get("volume_24h_usd", 0.0))
                bid = float(ticker.get("bid", 0.0))
                ask = float(ticker.get("ask", 0.0))
            except (TypeError, ValueError):
                continue
            if volume < min_volume_usd or bid <= 0.0 or ask <= bid:
                continue
            mid = (bid + ask) / 2.0
            spread_bps = (ask - bid) / mid * 10_000.0 if mid > 0 else float("inf")
            if spread_bps > max_spread_bps:
                continue
            # Keep widely traded core symbols represented, then rank the
            # remainder by current quote volume.  This is deterministic for a
            # given ticker snapshot and does not use future returns.
            priority_rank = 0 if symbol in PRIORITY_SYMBOLS else 1
            ranked.append((priority_rank, -volume, symbol))

        ranked.sort()
        # Zero explicitly means all candidates that passed live liquidity filters.
        pool = ranked if max_symbols == 0 else ranked[:max_symbols]
        selected = [symbol for _, _, symbol in pool]
        logger.info(
            "kucoin_ranked_liquid_universe",
            metadata_eligible=len(eligible),
            ticker_eligible=len(ranked),
            selected=len(selected),
            max_symbols=max_symbols,
            min_volume_usd=min_volume_usd,
            max_spread_bps=max_spread_bps,
        )
        return selected

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
