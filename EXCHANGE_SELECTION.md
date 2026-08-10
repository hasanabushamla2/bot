# Exchange Selection — Phase 2 Adapter Decision

## Evaluated Candidates

| Criteria | Binance | Coinbase | Kraken | Bybit | OKX |
|----------|---------|----------|--------|-------|-----|
| **Public WS (no auth)** | ✅ Full | ✅ Level2/ticker | ✅ Full | ✅ Full | ✅ Full |
| **Combined streams** | ✅ Yes | ❌ Separate | ❌ Separate | ⚠️ Limited | ⚠️ Limited |
| **REST API quality** | ✅ Excellent | ✅ Good | ⚠️ Quirky | ✅ Good | ✅ Good |
| **Order-book depth** | ✅ 5k levels | ✅ Full | ✅ 500 levels | ✅ 200 levels | ✅ 400 levels |
| **Trade stream** | ✅ Real-time | ✅ Real-time | ✅ Real-time | ✅ Real-time | ✅ Real-time |
| **Candles/kline** | ✅ 1s–1M | ✅ 1m–6h | ✅ 1m–1d | ✅ 1m–1M | ✅ 1m–3M |
| **Historical data** | ✅ klines REST | ⚠️ Limited | ⚠️ Limited | ✅ klines REST | ✅ klines REST |
| **Sandbox/testnet** | ✅ testnet | ✅ sandbox | ❌ No spot sandbox | ✅ testnet | ✅ demo |
| **Documentation** | ✅ Excellent | ✅ Good | ⚠️ Adequate | ✅ Good | ✅ Good |
| **Rate limits (public)** | 1200 w/min | 10 r/s | Varies | 50 r/s | 20 r/s |
| **Symbol coverage** | ✅ 600+ | ⚠️ 250+ | ⚠️ 200+ | ✅ 500+ | ✅ 500+ |
| **Liquidity** | ✅ Highest | ✅ High | ✅ High | ✅ High | ✅ High |
| **API reliability** | ✅ Very stable | ⚠️ Occasional | ✅ Stable | ✅ Stable | ⚠️ Occasional |
| **Python SDK quality** | ⚠️ python-binance | ⚠️ coinbase-advanced | ❌ No official | ⚠️ pybit | ⚠️ python-okx |
| **Regulatory** | ⚠️ Varies by region | ✅ US-regulated | ✅ EU-regulated | ⚠️ Varies | ⚠️ Varies |

## Recommendation: Binance

### Selected: Binance (testnet for paper, public API for market data)

### Why Binance

1. **No-auth public WebSocket** — Every public stream (ticker, depth, trades, klines) works without API keys. Perfect for development and paper trading.

2. **Combined streams** — Single WebSocket connection can carry multiple symbol+stream combinations (e.g. `btcusdt@ticker/ethusdt@depth20@100ms`). Drastically reduces connection count vs. one-connection-per-stream exchanges.

3. **Testnet available** — `testnet.binance.vision` provides a full sandbox when we need order execution testing in Phase 8. Currently unnecessary (Phase 2 is public data only).

4. **Historical klines** — REST endpoint serves OHLCV data back to exchange inception without authentication. Critical for backtesting data ingestion.

5. **Highest liquidity** — As the largest crypto exchange by volume, Binance provides the deepest books and tightest spreads. Liquidity metrics computed from Binance data will be representative.

6. **Excellent documentation** — Well-maintained API docs, change logs, and migration guides. Reduces implementation risk.

7. **Rate-limit friendliness** — 1200 weight per minute for public endpoints (~20 requests/sec average) is generous for a single-user system.

8. **Mature infrastructure** — Years of production stability. WebSocket feeds rarely drop.

### Limitations

1. **Testnet resets** — Binance testnet occasionally resets or goes offline for maintenance. Mitigated by using public production streams for Phase 2 market data.

2. **Regional restrictions** — Some jurisdictions block Binance. The adapter uses the global endpoints; users in restricted regions must use VPN or alternative exchange.

3. **No US entity** — Binance.US is a separate, much smaller exchange with different API endpoints. This adapter targets Binance.com.

4. **No official Python SDK** — Third-party libraries like `python-binance` exist but we build our own adapter for full control, zero dependency risk, and compatibility with our `ExchangeAdapter` interface.

### Adding More Exchanges Later

Each exchange requires only a new adapter class implementing `ExchangeAdapter` from `src/adapters/base.py`. The normalization layer, WebSocket manager, order-book engine, and all downstream modules are exchange-agnostic.

**Next priority after Binance**: Coinbase (US-regulated, good sandbox) or Bybit (modern API, good testnet).

### Initial Symbols

For Phase 2 development and testing, we subscribe to highly liquid Binance spot pairs:

| Symbol | Rationale |
|--------|-----------|
| BTCUSDT | Highest liquidity crypto pair |
| ETHUSDT | Second most liquid |
| SOLUSDT | High volume, moderate correlation to BTC |
| BNBUSDT | Native exchange token, different dynamics |
| XRPUSDT | High volume, provides diversification |

Additional symbols added dynamically via `UniverseManager` based on liquidity/volume thresholds.
