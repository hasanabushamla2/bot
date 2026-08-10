# Exchange Integration Guide

## How to Add a New Exchange Adapter

---

## 1. Architecture

Every exchange is isolated behind the `ExchangeAdapter` abstract interface
(`src/adapters/base.py`). The core engine never imports exchange-specific
code directly. Adding an exchange requires zero changes to strategy,
risk, execution, or portfolio modules.

---

## 2. Step-by-Step

### 2.1 Create the adapter module

```
src/adapters/<market_type>/<exchange>.py
```

Example: `src/adapters/crypto/binance.py`

### 2.2 Implement ExchangeAdapter

Subclass `ExchangeAdapter` and implement ALL abstract methods.

```python
from src.adapters.base import ExchangeAdapter

class MyExchangeAdapter(ExchangeAdapter):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def reconnect(self) -> None: ...
    async def get_instruments(self) -> list[NormalizedInstrument]: ...
    async def get_ticker(self, symbol: str) -> NormalizedTicker: ...
    async def get_order_book(self, symbol: str, depth: int) -> NormalizedOrderBook: ...
    async def subscribe_ticker(self, symbol: str) -> AsyncIterator[NormalizedTicker]: ...
    async def subscribe_order_book(self, symbol: str) -> AsyncIterator[NormalizedOrderBook]: ...
    async def subscribe_trades(self, symbol: str) -> AsyncIterator[NormalizedTrade]: ...
    async def place_order(self, request) -> NormalizedOrder: ...  # stub for now
    async def cancel_order(...) -> NormalizedOrder: ...            # stub for now
    async def get_order(...) -> NormalizedOrder: ...               # stub for now
    async def get_open_orders(...) -> list[NormalizedOrder]: ...   # stub for now
    async def get_order_history(...) -> list[NormalizedOrder]: ... # stub for now
    async def get_balances(...) -> list[NormalizedBalance]: ...    # stub for now
    async def health_check(self) -> bool: ...
    async def get_server_time(self) -> datetime: ...
```

### 2.3 Normalize all data

Use `src/data/normalization.py` models for every event:

```python
from src.data.normalization import (
    CanonicalSymbol, TickerEvent, TradeEvent,
    OrderBookSnapshot, OrderBookDelta, CandleEvent,
)
```

### 2.4 Symbol normalization

Use `CanonicalSymbol.from_exchange_symbol()` to normalize any
exchange-specific symbol format (BTCUSDT, BTC/USDT, XBTUSD, etc.)
into the internal `BASE-QUOTE` format.

### 2.5 Register the adapter

Add to engine initialization:

```python
from src.adapters.crypto.my_exchange import MyExchangeAdapter
adapters["my_exchange"] = MyExchangeAdapter(...)
```

### 2.6 Add tests

Create `tests/test_adapters/test_my_exchange.py`:

- Unit test symbol normalization
- Mock WebSocket messages
- Test reconnection logic
- Test rate limiting

---

## 3. Requirements Checklist

- [ ] Public market data works without API keys
- [ ] WebSocket streams: ticker, order book, trades
- [ ] REST fallbacks for snapshots and metadata
- [ ] Symbol normalization to canonical format
- [ ] Timestamp normalization to UTC
- [ ] Rate limiting (token bucket or endpoint-weight aware)
- [ ] Reconnection with exponential backoff + jitter
- [ ] Feed health monitoring integration
- [ ] Order methods raise NotImplementedError (live trading disabled)
- [ ] No secrets hard-coded or logged
- [ ] Tests cover: connection, normalization, error handling

---

## 4. Currently Implemented

| Exchange | Adapter | Status |
|----------|---------|--------|
| **Binance** | `src/adapters/crypto/binance.py` | ✅ Complete (public data) |
| Coinbase | Not yet | Planned |
| Kraken | Not yet | Planned |
| Bybit | Not yet | Planned |
| OKX | Not yet | Planned |

---

## 5. Adding Historical Data Support

If the exchange provides historical klines/candles:

```python
async def get_klines(
    self, symbol: str, interval: str = "1h",
    limit: int = 500, start_time: int | None = None,
) -> list[list[Any]]: ...
```

Normalize through `HistoricalDataLoader.from_binance_klines()` or
equivalent for the exchange's kline format.

---

## 6. Testing Against Sandbox/Testnet

1. Configure the adapter with `use_testnet=True` or equivalent.
2. Verify connectivity with `health_check()`.
3. Subscribe to a few liquid symbols.
4. Verify ticks, books, and trades arrive.
5. Confirm clean shutdown and reconnection.
