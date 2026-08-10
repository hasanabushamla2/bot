# Market Data

## Quant Opportunity Engine — Real-Time Market Data Architecture

---

## 1. Overview

The Market Data Engine is the entry point for all external market information. It connects to exchange WebSocket feeds, normalizes events into an internal canonical format, maintains local order books, monitors feed health, and distributes events to downstream consumers via a non-blocking async event bus.

### Design Principles

1. **WebSocket-first**: WebSocket for low-latency streaming; REST for fallback, snapshots, and metadata.
2. **No-auth public data**: Public market data works without API keys on all supported exchanges.
3. **Exchange-agnostic**: All downstream modules consume only normalized events; exchange-specific logic stays in adapters.
4. **Bounded resources**: Queues, buffers, and caches all have explicit limits. No unbounded growth.
5. **Observable**: Every feed, connection, queue, and latency path is measured and exposed.

---

## 2. Architecture

```
Exchange WS ──→ [BinanceAdapter] ──→ Normalized Events
Exchange REST         │                      │
                      ▼                      ▼
              [Rate Limiter]         [Event Bus]
              [Feed Health]          [Order Book Engine]
              [Metrics Collector]    [Liquidity Analyzer]
                                     [Universe Manager]
```

### Components

| Module | Path | Purpose |
|--------|------|---------|
| Normalization | `src/data/normalization.py` | Canonical symbol, strict event models |
| Order Book Engine | `src/data/order_book.py` | Local order book from snapshot+delta |
| Rate Limiter | `src/data/rate_limiter.py` | Token bucket per exchange/endpoint |
| Feed Health | `src/data/feed_health.py` | Stale detection, latency percentiles |
| Event Bus | `src/data/event_bus.py` | Non-blocking async event distribution |
| Metrics | `src/data/metrics.py` | Throughput, latency, health aggregates |
| Historical | `src/data/historical.py` | CSV/REST historical data ingestion |
| Binance Adapter | `src/adapters/crypto/binance.py` | Concrete WebSocket+REST adapter |

---

## 3. Connecting to Live Data

### Quick Start

```bash
# Live market data demo (public data, NO API keys needed)
python scripts/run_market_data.py --duration 30

# With custom symbols and streams
python scripts/run_market_data.py \
    --symbols BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT \
    --streams ticker,book,trades \
    --duration 120

# Using Binance testnet
python scripts/run_market_data.py --testnet --duration 30
```

### What It Does
1. Connects to Binance WebSocket (public endpoint).
2. Subscribes to ticker, order-book, and/or trade streams for each symbol.
3. Prints normalized events with computed spread, depth, and latency.
4. Runs for the specified duration, then exits cleanly.
5. **NO orders are placed. NO API keys required.**

---

## 4. Supported Data Streams

| Stream | Binance WS Name | Frequency | Normalized Event |
|--------|----------------|-----------|-----------------|
| Ticker | `<symbol>@ticker` | 1000ms | `TickerEvent` |
| Order Book (top 20) | `<symbol>@depth20@100ms` | 100ms | `OrderBookSnapshot` |
| Trades | `<symbol>@trade` | Real-time | `TradeEvent` |
| Candles | REST only (klines) | On demand | `CandleEvent` |
| Instruments | REST `/exchangeInfo` | On startup | `InstrumentMetadata` |

---

## 5. Order Book Protocol (Binance)

For partial depth (simple, recommended for most use cases):
- Subscribe to `@depth20@100ms` — sends full top-20 snapshot every 100ms.
- No sequence-number tracking needed.
- Sufficient for spread, depth, and VWAP computation.

For full depth (future, requires careful state management):
1. Fetch REST snapshot (`GET /api/v3/depth?limit=1000`).
2. Subscribe to `@depth@100ms` (diff stream).
3. Buffer diffs until `U <= lastUpdateId+1 <= u`.
4. Apply snapshot, replay buffered diffs.
5. Handle gaps → request new REST snapshot.

The `OrderBookEngine` handles both protocols.

---

## 6. Rate Limiting

Configurable token-bucket rate limiter per exchange:

- **Binance public**: 1200 weight/min ≈ 20 req/s; burst to 100.
- **Per-endpoint weights**: Recorded via `set_endpoint_weight()`.
- **429 handling**: Automatic `Retry-After` throttling.
- **Metrics**: Current tokens, acquires, waits, rejects.

---

## 7. Feed Health Monitoring

Every stream is tracked:

| Metric | Threshold | Action |
|--------|-----------|--------|
| Last message age > 10s | STALE | Suspend universe asset |
| Last message age > 60s | DISCONNECTED | Trigger reconnect |
| Clock offset > 5s | DEGRADED | Log warning |
| Sequence gap detected | RESYNC | Request snapshot |
| Reconnection count > 10 | ALERT | Log critical |

---

## 8. Latency Tracking

Measured at every message receipt:
- **exchange_to_local**: (local_receive - exchange_timestamp) in ms
- **Percentiles**: p50, p95, p99 computed from rolling window
- **Export**: `DataEngineMetrics` snapshot to dashboard/analytics

---

## 9. Adding a New Exchange

1. Create adapter in `src/adapters/<type>/<exchange>.py`.
2. Implement `ExchangeAdapter` abstract methods.
3. Normalize all data through `src/data/normalization.py` models.
4. Register in engine initialization.
5. Add tests in `tests/test_adapters/`.

See [EXCHANGES.md](EXCHANGES.md) for detailed guide.

---

## 10. Troubleshooting

| Symptom | Check |
|---------|-------|
| No data | `health_check()` → network/DNS |
| Stale feeds | `FeedHealthMonitor.get_unhealthy()` |
| High latency | `MetricsCollector.snapshot()` → latency percentiles |
| Rate limited | `RateLimiter.get_all_stats()` → utilization |
| Order book gaps | `OrderBookEngine.needs_resync()` |
| Connection drops | Binance may geo-block; try `--testnet` or VPN |
