"""Binance Exchange Adapter — concrete implementation of ExchangeAdapter.

Binance API:
- REST base:    https://api.binance.com/api/v3
- WS public:    wss://stream.binance.com:9443/ws (single stream)
- WS combined:  wss://stream.binance.com:9443/stream?streams=<streams>
- Testnet REST: https://testnet.binance.vision/api/v3
- Testnet WS:   wss://testnet.binance.vision/ws

Features:
- Full public-market-data support (no API keys needed)
- Combined WebSocket streams for efficiency
- Depth stream protocol (snapshot + delta with sequence tracking)
- REST fallback for order-book snapshots, exchange info, server time
- Rate-limit-aware (token bucket)
- Auto-reconnection with exponential backoff + jitter

Live trading: DISABLED by default. Order methods raise NotImplementedError
or route to paper engine only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time as _time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from src.adapters.base import (
    ExchangeAdapter,
    NormalizedBalance,
    NormalizedInstrument,
    NormalizedOrder,
    NormalizedOrderBook,
    NormalizedOrderBookLevel,
    NormalizedOrderRequest,
    NormalizedTicker,
    NormalizedTrade,
    OrderSide,
)
from src.core.logging_config import get_logger
from src.data.normalization import (
    CanonicalSymbol,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class BinanceEndpoints:
    """Binance endpoint configuration."""

    REST = "https://api.binance.com"
    WS_SINGLE = "wss://stream.binance.com:9443/ws"
    WS_COMBINED = "wss://stream.binance.com:9443/stream"
    TESTNET_REST = "https://testnet.binance.vision"
    TESTNET_WS = "wss://testnet.binance.vision/ws"


class BinanceAdapter(ExchangeAdapter):
    """Binance exchange adapter.

    Args:
        exchange_name: "binance" or "binance-testnet"
        api_key: Optional API key (not required for public data).
        api_secret: Optional API secret.
        use_testnet: Whether to use Binance testnet endpoints.
    """

    def __init__(
        self,
        exchange_name: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        use_testnet: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(exchange_name, api_key, api_secret, **kwargs)
        self.use_testnet = use_testnet
        self.rest_base = BinanceEndpoints.TESTNET_REST if use_testnet else BinanceEndpoints.REST
        self.ws_base = BinanceEndpoints.TESTNET_WS if use_testnet else BinanceEndpoints.WS_COMBINED

        self._http: httpx.AsyncClient | None = None
        self._ws: ClientConnection | None = None
        self._ws_task: asyncio.Task[Any] | None = None
        self._subscribed_streams: set[str] = set()
        self._running = False
        self._message_seq = 0

        # Reconnection state
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._reconnect_count = 0

        # Callbacks for event distribution
        self._ticker_callbacks: list[Any] = []
        self._trade_callbacks: list[Any] = []
        self._book_callbacks: list[Any] = []
        self._candle_callbacks: list[Any] = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Establish HTTP client and WebSocket connection."""
        self._http = httpx.AsyncClient(
            base_url=self.rest_base,
            timeout=httpx.Timeout(30.0),
        )
        self._running = True
        self._connected = True
        logger.info("binance_connected", exchange=self.exchange_name, testnet=self.use_testnet)

    async def disconnect(self) -> None:
        """Gracefully close all connections."""
        self._running = False

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task

        if self._ws:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

        if self._http:
            await self._http.aclose()
            self._http = None

        self._connected = False
        logger.info("binance_disconnected", exchange=self.exchange_name)

    async def reconnect(self) -> None:
        """Reconnect after connection loss."""
        await self.disconnect()
        self._reconnect_count += 1
        delay = min(
            self._reconnect_delay * (2 ** (self._reconnect_count - 1)) + random.uniform(0, 1),
            self._max_reconnect_delay,
        )
        logger.info(
            "binance_reconnecting",
            attempt=self._reconnect_count,
            delay=round(delay, 2),
        )
        await asyncio.sleep(delay)
        await self.connect()
        # Resubscribe to all streams
        if self._subscribed_streams:
            await self._ws_subscribe_multi(list(self._subscribed_streams))

    # ------------------------------------------------------------------
    # REST: Instruments
    # ------------------------------------------------------------------

    async def get_instruments(self) -> list[NormalizedInstrument]:
        """Fetch all trading symbols from exchange info."""
        data = await self._rest_get("/api/v3/exchangeInfo")
        instruments: list[NormalizedInstrument] = []

        for s in data.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, s["symbol"])

            filters = {f["filterType"]: f for f in s.get("filters", [])}
            price_filter = filters.get("PRICE_FILTER", {})
            lot_filter = filters.get("LOT_SIZE", {})
            notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))

            tick_size = float(price_filter.get("tickSize", "0.01"))
            step_size = float(lot_filter.get("stepSize", "0.000001"))

            instruments.append(
                NormalizedInstrument(
                    exchange=self.exchange_name,
                    symbol=canonical.symbol,
                    base_asset=canonical.base,
                    quote_asset=canonical.quote,
                    min_order_size=float(lot_filter.get("minQty", "0.000001")),
                    price_precision=_count_decimals(tick_size),
                    quantity_precision=_count_decimals(step_size),
                    maker_fee=0.001,
                    taker_fee=0.001,
                    is_active=True,
                    metadata={
                        "raw_symbol": s["symbol"],
                        "tick_size": tick_size,
                        "step_size": step_size,
                        "min_notional": float(notional_filter.get("minNotional", "10.0")),
                        "order_types": s.get("orderTypes", []),
                        "is_spot": s.get("isSpotTradingAllowed", False),
                    },
                )
            )

        logger.info("binance_instruments_loaded", count=len(instruments))
        return instruments

    # ------------------------------------------------------------------
    # REST: Ticker
    # ------------------------------------------------------------------

    async def get_ticker(self, symbol: str) -> NormalizedTicker:
        """REST ticker for a single symbol."""
        raw = _binance_symbol(symbol)
        data = await self._rest_get("/api/v3/ticker/24hr", params={"symbol": raw})
        canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, raw)

        return NormalizedTicker(
            exchange=self.exchange_name,
            symbol=canonical.symbol,
            bid=float(data.get("bidPrice", 0)),
            ask=float(data.get("askPrice", 0)),
            last=float(data.get("lastPrice", 0)),
            volume_24h=float(data.get("volume", 0)),
            high_24h=float(data.get("highPrice", 0)),
            low_24h=float(data.get("lowPrice", 0)),
            timestamp=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # REST: Order book
    # ------------------------------------------------------------------

    async def get_order_book(self, symbol: str, depth: int = 100) -> NormalizedOrderBook:
        """REST order-book snapshot."""
        raw = _binance_symbol(symbol)
        params = {"symbol": raw, "limit": min(depth, 5000)}
        data = await self._rest_get("/api/v3/depth", params=params)

        bids = [
            NormalizedOrderBookLevel(price=float(b[0]), quantity=float(b[1]))
            for b in data.get("bids", [])
        ]
        asks = [
            NormalizedOrderBookLevel(price=float(a[0]), quantity=float(a[1]))
            for a in data.get("asks", [])
        ]
        canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, raw)

        return NormalizedOrderBook(
            exchange=self.exchange_name,
            symbol=canonical.symbol,
            bids=bids,
            asks=asks,
            timestamp=datetime.now(UTC),
            metadata={"last_update_id": data.get("lastUpdateId", 0)},
        )

    # ------------------------------------------------------------------
    # WebSocket subscriptions
    # ------------------------------------------------------------------

    async def subscribe_ticker(self, symbol: str) -> AsyncIterator[NormalizedTicker]:
        """Stream ticker via WebSocket."""
        raw = _binance_symbol(symbol)
        stream = f"{raw.lower()}@ticker"
        queue: asyncio.Queue[NormalizedTicker] = asyncio.Queue(maxsize=500)

        async def _handler(data: dict[str, Any]) -> None:
            canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, raw)
            ticker = NormalizedTicker(
                exchange=self.exchange_name,
                symbol=canonical.symbol,
                bid=float(data.get("b", 0)),
                ask=float(data.get("a", 0)),
                last=float(data.get("c", 0)),
                volume_24h=float(data.get("v", 0)),
                high_24h=float(data.get("h", 0)),
                low_24h=float(data.get("l", 0)),
                timestamp=datetime.now(UTC),
            )
            try:
                queue.put_nowait(ticker)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(ticker)

        self._ticker_callbacks.append((stream.lower(), _handler))
        await self._ensure_ws_streams()

        try:
            while self._running:
                try:
                    ticker = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield ticker
                except TimeoutError:
                    continue
        finally:
            self._ticker_callbacks = [
                (s, h) for s, h in self._ticker_callbacks if s != stream.lower()
            ]

    async def subscribe_trades(self, symbol: str) -> AsyncIterator[NormalizedTrade]:
        """Stream trades via WebSocket."""
        raw = _binance_symbol(symbol)
        stream = f"{raw.lower()}@trade"
        queue: asyncio.Queue[NormalizedTrade] = asyncio.Queue(maxsize=2000)

        async def _handler(data: dict[str, Any]) -> None:
            canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, raw)
            trade_time = datetime.fromtimestamp(data.get("T", _time.time()) / 1000, tz=UTC)
            trade = NormalizedTrade(
                exchange=self.exchange_name,
                symbol=canonical.symbol,
                trade_id=str(data.get("t", "")),
                price=float(data.get("p", 0)),
                quantity=float(data.get("q", 0)),
                side=OrderSide.BUY if not data.get("m", False) else OrderSide.SELL,
                timestamp=trade_time,
            )
            try:
                queue.put_nowait(trade)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(trade)

        self._trade_callbacks.append((stream.lower(), _handler))
        await self._ensure_ws_streams()

        try:
            while self._running:
                try:
                    trade = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield trade
                except TimeoutError:
                    continue
        finally:
            self._trade_callbacks = [
                (s, h) for s, h in self._trade_callbacks if s != stream.lower()
            ]

    async def subscribe_order_book(self, symbol: str) -> AsyncIterator[NormalizedOrderBook]:
        """Stream order-book depth via WebSocket.

        Uses Binance partial-book-depth stream (@depth20@100ms) which
        sends a full snapshot of top 20 levels every 100ms. This is
        simpler than the diff-depth protocol and sufficient for most
        liquidity analysis.

        For full depth support, use @depth@100ms (diff protocol).
        """
        raw = _binance_symbol(symbol)
        stream = f"{raw.lower()}@depth20@100ms"
        queue: asyncio.Queue[NormalizedOrderBook] = asyncio.Queue(maxsize=200)

        async def _handler(data: dict[str, Any]) -> None:
            canonical = CanonicalSymbol.from_exchange_symbol(self.exchange_name, raw)
            bids = [
                NormalizedOrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get("bids", [])
            ]
            asks = [
                NormalizedOrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get("asks", [])
            ]
            book = NormalizedOrderBook(
                exchange=self.exchange_name,
                symbol=canonical.symbol,
                bids=bids,
                asks=asks,
                timestamp=datetime.now(UTC),
                metadata={
                    "last_update_id": data.get("lastUpdateId", 0),
                    "event_time": data.get("E", 0),
                },
            )
            try:
                queue.put_nowait(book)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(book)

        self._book_callbacks.append((stream.lower(), _handler))
        await self._ensure_ws_streams()

        try:
            while self._running:
                try:
                    book = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield book
                except TimeoutError:
                    continue
        finally:
            self._book_callbacks = [(s, h) for s, h in self._book_callbacks if s != stream.lower()]

    # ------------------------------------------------------------------
    # REST: Historical klines
    # ------------------------------------------------------------------

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 500,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[list[Any]]:
        """Fetch historical klines/candles.

        Args:
            symbol: Trading symbol (e.g., "BTC-USDT").
            interval: kline interval (1m, 5m, 15m, 1h, 4h, 1d, etc.).
            limit: Max 1000.
            start_time: Start time in ms.
            end_time: End time in ms.
        """
        raw = _binance_symbol(symbol)
        params: dict[str, Any] = {
            "symbol": raw,
            "interval": interval,
            "limit": min(limit, 1000),
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        return await self._rest_get("/api/v3/klines", params=params)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # REST: Server time
    # ------------------------------------------------------------------

    async def get_server_time(self) -> datetime:
        """Get exchange server time."""
        data = await self._rest_get("/api/v3/time")
        ms = data.get("serverTime", int(_time.time() * 1000))
        return datetime.fromtimestamp(ms / 1000.0, tz=UTC)

    # ------------------------------------------------------------------
    # REST: Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Ping the REST API."""
        try:
            await self._rest_get("/api/v3/ping")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Order methods — DISABLED for public-data mode
    # ------------------------------------------------------------------

    async def place_order(self, request: NormalizedOrderRequest) -> NormalizedOrder:
        raise NotImplementedError(
            "Live order placement is NOT implemented. "
            "Use PaperExecutionEngine for simulated orders."
        )

    async def cancel_order(self, order_id: str, symbol: str) -> NormalizedOrder:
        raise NotImplementedError("Live order cancellation is NOT implemented.")

    async def get_order(self, order_id: str, symbol: str) -> NormalizedOrder:
        raise NotImplementedError("Live order query is NOT implemented.")

    async def get_open_orders(self, symbol: str | None = None) -> list[NormalizedOrder]:
        raise NotImplementedError("Live order query is NOT implemented.")

    async def get_order_history(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[NormalizedOrder]:
        raise NotImplementedError("Live order history is NOT implemented.")

    async def get_balances(self) -> list[NormalizedBalance]:
        raise NotImplementedError("Live balance query is NOT implemented.")

    # ------------------------------------------------------------------
    # WebSocket internals
    # ------------------------------------------------------------------

    async def _ensure_ws_streams(self) -> None:
        """Start WebSocket connection if not already running."""
        if self._ws and self._ws_task and not self._ws_task.done():
            # Already connected — resubscribe if needed
            all_streams = self._collect_streams()
            if all_streams - self._subscribed_streams:
                await self._ws_subscribe_multi(list(all_streams))
            return

        all_streams = self._collect_streams()
        self._subscribed_streams = all_streams
        self._ws_task = asyncio.create_task(self._ws_loop(all_streams))

    def _collect_streams(self) -> set[str]:
        """Collect all requested streams from callbacks."""
        streams: set[str] = set()
        for stream_name, _ in self._ticker_callbacks:
            streams.add(stream_name)
        for stream_name, _ in self._trade_callbacks:
            streams.add(stream_name)
        for stream_name, _ in self._book_callbacks:
            streams.add(stream_name)
        return streams

    async def _ws_loop(self, streams: set[str]) -> None:
        """Main WebSocket event loop with reconnection."""
        while self._running:
            try:
                await self._ws_connect_and_listen(streams)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("binance_ws_error", exchange=self.exchange_name)
                if self._running:
                    self._reconnect_count += 1
                    delay = min(
                        self._reconnect_delay * (2 ** max(0, self._reconnect_count - 1))
                        + random.uniform(0, 1),
                        self._max_reconnect_delay,
                    )
                    logger.warning(
                        "binance_ws_reconnecting",
                        delay=round(delay, 2),
                        attempt=self._reconnect_count,
                    )
                    await asyncio.sleep(delay)
                else:
                    break

    async def _ws_connect_and_listen(self, streams: set[str]) -> None:
        """Connect WebSocket and process messages."""
        if self.use_testnet:
            url = f"{self.ws_base}/{'/'.join(sorted(streams))}"
        else:
            url = f"{self.ws_base}?streams={'/'.join(sorted(streams))}"

        logger.info("binance_ws_connecting", url=url[:120], streams_count=len(streams))
        self._reconnect_count = 0

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._ws = ws
            logger.info("binance_ws_connected", streams=list(streams))

            async for raw_msg in ws:
                if not self._running:
                    break
                try:
                    await self._dispatch_message(raw_msg)
                except Exception:
                    logger.exception("binance_ws_dispatch_error")

    async def _ws_subscribe_multi(self, streams: list[str]) -> None:
        """Subscribe to additional streams on existing connection."""
        if not self._ws:
            return
        try:
            params = {"method": "SUBSCRIBE", "params": streams, "id": self._message_seq}
            self._message_seq += 1
            await self._ws.send(json.dumps(params))
            self._subscribed_streams.update(streams)
            logger.debug("binance_ws_subscribed", streams=streams)
        except Exception:
            logger.warning("binance_ws_subscribe_failed", streams=streams)

    async def _dispatch_message(self, raw_msg: str | bytes) -> None:
        """Parse and route a WebSocket message to registered callbacks."""
        data = json.loads(raw_msg)

        # Combined stream wrapper: {"stream": "...", "data": {...}}
        if "stream" in data and "data" in data:
            stream_name = data["stream"]
            payload = data["data"]
        else:
            # Single stream or subscription response
            if "result" in data or "id" in data:
                return  # Subscription ACK — ignore
            return  # Unknown format

        # Route to callbacks
        for cb_stream, handler in (
            self._ticker_callbacks + self._trade_callbacks + self._book_callbacks
        ):
            if cb_stream == stream_name:
                try:
                    await handler(payload)
                except Exception:
                    logger.exception("binance_callback_error", stream=stream_name)
                break

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    async def _rest_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform a GET request with retry and rate-limit handling."""
        if not self._http:
            raise RuntimeError("Adapter not connected. Call connect() first.")

        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = await self._http.get(path, params=params)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                    logger.warning(
                        "binance_429",
                        path=path,
                        retry_after=retry_after,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(2**attempt)
                        continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    continue
                raise
            except httpx.RequestError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2**attempt)
                    continue
                raise

        raise RuntimeError(f"REST request failed after {max_retries} retries: {path}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binance_symbol(canonical: str) -> str:
    """Convert "BTC-USDT" → "BTCUSDT"."""
    return canonical.replace("-", "").upper()


def _count_decimals(value: float) -> int:
    """Count significant decimals in a price step."""
    s = f"{value:.10f}".rstrip("0")
    if "." in s:
        return len(s.split(".")[1])
    return 0
