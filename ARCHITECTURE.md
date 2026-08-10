# Architecture

## Quant Opportunity Engine — System Architecture

Version: 0.1.0 | Phase 1

---

## 1. Architectural Philosophy

This system follows a **plugin-based event-driven architecture** with strict separation of concerns.

Every major component is an independent, testable module. No component makes assumptions about which markets, exchanges, or strategies are in use.

### Core Principles

1. **Interface-Driven**: Every integration point is defined by an abstract interface (ABC). The core engine depends only on interfaces, never on concrete exchange implementations.

2. **No Bypass**: The Risk Engine is a mandatory gate. Strategies cannot place orders. The Execution Engine cannot skip risk checks.

3. **Safety-First**: Live trading is disabled by default. A dual-gate safety mechanism (environment variable + configuration flag) must be explicitly tripped before any real-money order is possible.

4. **Realism**: Backtesting and paper trading use the same code paths as live trading. No simplified "fast path" that would exaggerate performance.

5. **Auditability**: Every order has a trace explaining WHY it was placed — which strategy, which signal, what score, what risk assessment.

---

## 2. System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                     MARKET / EXCHANGE APIs                        │
│   Coinbase │ Binance │ Kraken │ Bybit │ Forex │ Gold │ Future   │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                  REAL-TIME DATA ENGINE                            │
│  WebSocket subscriptions  │  REST fallback  │  Order book mgmt  │
│  Reconnection  │  Heartbeat  │  Stale detection  │  Dedup       │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                    MULTI-MARKET SCANNER                           │
│  Normalizes symbols, timestamps, prices across exchanges         │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                      FEATURE ENGINE                               │
│  Computes derived features from raw market data                  │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│               MULTI-STRATEGY ENGINE (Plugin System)               │
│  Arb │ Pairs │ MeanRev │ Momentum │ Breakout │ OBI │ Vol │ ...  │
│  Each strategy: independent plugin, produces Signal objects      │
└──────────────────────────┬───────────────────────────────────────┘
                           │ Signals
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                  OPPORTUNITY RANKING ENGINE                       │
│  Score = f(net_return, fill_prob, correlation, strategy_exp)     │
│  Ranked by risk-adjusted expected net value                      │
└──────────────────────────┬───────────────────────────────────────┘
                           │ Ranked Opportunities
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                       RISK ENGINE (Mandatory Gate)                │
│  Position sizing │ Exposure limits │ Drawdown │ Circuit breaker  │
│  Kill switch │ Correlation limits │ Stop-loss enforcement       │
└──────────────────────────┬───────────────────────────────────────┘
                           │ Approved Opportunities
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     EXECUTION ENGINE                              │
│  Order state machine │ Idempotency │ Retries │ Reconciliation    │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     EXCHANGE / BROKER                             │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│              POSITION & ORDER MONITORING                          │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                 DATABASE / EVENT STORE                            │
│  PostgreSQL: orders, fills, positions, signals, audit log        │
│  Redis: caching, pub/sub, rate limiting                          │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│               ANALYTICS / DASHBOARD / ALERTS                      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Module Descriptions

### 3.1 Core (`src/core/`)
- **config.py**: Layered configuration (YAML defaults → env vars → runtime). Pydantic-based with validation.
- **exceptions.py**: Domain exception hierarchy. Every error has a specific type.
- **logging_config.py**: Structured JSON logging with automatic secret redaction.

### 3.2 Data Engine (`src/data/`)
- WebSocket-first with REST fallback.
- Automatic reconnection with exponential backoff.
- Stale-data detection (configurable threshold).
- Duplicate/out-of-order message detection via sequence numbers.
- Local order book maintenance.

### 3.3 Exchange Adapters (`src/adapters/`)
- `base.py`: Abstract `ExchangeAdapter` defining the complete interface.
- Each exchange gets a concrete implementation.
- Normalized types: `NormalizedTicker`, `NormalizedOrderBook`, `NormalizedOrder`, `NormalizedBalance`, etc.
- Adding a new exchange = writing a new adapter class. Zero changes to the core engine.

### 3.4 Strategies (`src/strategies/`)
- `base.py`: `BaseStrategy` abstract class with `analyze()` method.
- `registry.py`: Strategy registry for discovery and lifecycle management.
- Each strategy is a standalone module that produces `StrategySignal` objects.
- Strategies NEVER place orders or access exchange APIs directly.

### 3.5 Opportunity Engine (`src/opportunity/`)
- Consumes signals from all strategies.
- Computes `OpportunityScore` = f(gross_return, fees, spread, slippage, fill_prob, correlation, strategy_expectancy).
- Rejects opportunities below configurable thresholds.
- Ranks remaining by risk-adjusted expected net value.

### 3.6 Risk Engine (`src/risk/`)
- Completely independent. Strategies CANNOT bypass.
- Configurable limits: position size, total exposure, per-market, per-strategy, drawdown.
- Circuit breaker: auto-trips on consecutive losses or excessive drawdown.
- Kill switch: manual emergency stop.
- Stop-loss computation per position.

### 3.7 Execution Engine (`src/execution/`)
- `state_machine.py`: Deterministic FSM for order lifecycle.
- `engine.py`: Order placement with idempotency keys, retries, timeouts, rate limiting.
- Reconciliation on restart: detects orders filled/canceled while system was down.
- Balance verification before placing orders.

### 3.8 Backtesting Engine (`src/backtesting/`)
- Anti-bias protections: train/val/test period separation enforced.
- Walk-forward analysis support.
- Realistic simulation: fees, spread, slippage, latency.
- Stop-loss and take-profit simulation.
- Partial fills where data supports it.

### 3.9 Paper Trading (`src/paper/`)
- Same interfaces as live mode.
- Uses real market data for fills.
- Simulates realistic fees, slippage, latency.
- No simplified "paper-only" path.

### 3.10 Database (`src/db/`)
- **models.py**: SQLAlchemy ORM models for all entities.
- **repository.py**: Async data access layer.
- PostgreSQL for persistence; Redis for caching and pub/sub.

### 3.11 Analytics (`src/analytics/`)
- Tracks every metric: win rate, P&L, Sharpe, Sortino, drawdown, fees, slippage, latency.
- Strategy/market/exchange breakdowns.
- All numbers from actual data — never fabricated.

### 3.12 Dashboard (`src/dashboard/`)
- FastAPI-based internal monitoring dashboard.
- Real-time equity, P&L, positions, opportunities.
- Paper/Live mode indicator — CANNOT be confused.
- Health status for all data streams.

---

## 4. Data Flow

### Normal Operation (Hot Path)

```
1. WebSocket ticker arrives → Data Engine normalizes → Scanner
2. Scanner routes to subscribed Strategy plugins
3. Strategy.analyze() produces Signal (or None)
4. Opportunity Engine scores Signal → Opportunity
5. Risk Engine assesses Opportunity → Approved/Rejected
6. Execution Engine places order → Exchange
7. Fill confirmation → Position Manager updates state
8. Analytics Tracker records trade metrics
9. Dashboard updates in real-time
```

### Latency Budget (Target)

| Step | Target Latency |
|------|---------------|
| WebSocket → Normalized | < 5ms |
| Normalize → Strategy Input | < 1ms |
| Strategy Analysis | < 10ms |
| Opportunity Scoring | < 5ms |
| Risk Assessment | < 2ms |
| Order Placement (API call) | < 100ms |
| **Total Signal → Order** | **< 150ms** |

Note: These are software-level targets. Internet latency to exchanges is additional and varies.

---

## 5. Database Entity Relationship

```
Exchange ──1:N──> Instrument
Strategy (logical) ──1:N──> Signal
Signal ──1:1──> Opportunity (via signal_id)
Opportunity ──1:1──> Order (via opportunity_id)
Order ──1:N──> Fill
Order ──1:N──> Position (via related orders)
AccountSnapshot (periodic snapshots)
StrategyMetric (rolling performance)
AuditEvent (immutable append-only log)
```

---

## 6. Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| **Language** | Python 3.11+ | Quant/research ecosystem, rapid development |
| **Async Runtime** | asyncio | Required for concurrent WebSocket + REST operations |
| **Web Framework** | FastAPI | Dashboard, health checks, async-native |
| **Database** | PostgreSQL 16 | ACID, JSONB, mature, excellent async support |
| **ORM** | SQLAlchemy 2.0 (async) | Mature, supports async sessions |
| **Cache/PubSub** | Redis 7 | Rate limiting, caching, inter-process signaling |
| **Validation** | Pydantic v2 | Configuration and data validation |
| **Logging** | structlog | Structured JSON logging with secret filtering |
| **Serialization** | orjson | Fast JSON for high-throughput paths |
| **Numeric** | NumPy, SciPy, Pandas | Quant analysis, backtesting metrics |
| **HTTP Client** | httpx | Async HTTP for REST fallback and exchange APIs |
| **WebSocket** | websockets | Low-level WebSocket client |
| **Testing** | pytest + hypothesis | Unit, integration, property-based |
| **Linting** | ruff | Fast Python linter and formatter |
| **Types** | mypy (strict mode) | Static type checking |
| **Container** | Docker + Compose | Reproducible deployment |
| **Migrations** | Alembic | Database schema versioning |

### Future Considerations
- **Rust/C++**: If profiling shows Python is a bottleneck in the hot path, performance-critical modules (data normalization, order book management) can be moved to Rust via PyO3.
- **Kafka/Redpanda**: For high-volume event streaming if the system scales to many users.
- **TimescaleDB**: For efficient time-series market data storage.
- **Kubernetes**: For production multi-exchange deployment.

---

## 7. Failure Modes & Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| WebSocket disconnect | Heartbeat timeout | Auto-reconnect with exponential backoff |
| Exchange API down | HTTP 5xx / timeout | Exponential backoff, fall back to REST polling |
| Stale data | Last-message timestamp > threshold | Mark stream unhealthy, skip signals for that market |
| Duplicate event | Sequence number tracking | Drop duplicate, increment counter |
| Out-of-order event | Sequence number comparison | Log warning, process if within tolerance |
| Order HTTP response lost | Timeout after placement | Reconciliation: query exchange for order state |
| Partial fill | Fill quantity < order quantity | Update order state to PARTIALLY_FILLED |
| Order rejected | Exchange error response | Mark REJECTED, do NOT retry blindly |
| Process crash mid-trade | State persisted in PostgreSQL | On restart: reconcile all open orders with exchange |
| Database connection lost | Connection pool errors | Retry with backoff, circuit-breaker on repeated failure |
| Redis connection lost | Connection errors | Degrade gracefully: skip caching, continue trading |
| Circuit breaker trip | Drawdown/consecutive-loss threshold | Halt new positions, alert, require manual reset |
| Kill switch activated | Manual or automated trigger | Immediately halt all new orders, cancel open orders |

---

## 8. Security Architecture

See [SECURITY.md](SECURITY.md) for full details.

Key points:
- API keys ONLY via environment variables.
- Trading-only permissions (NO withdrawals).
- Live trading requires explicit dual-gate.
- Secrets redacted in all logs.
- Non-root Docker user.
- Input validation on all external data.

---

## 9. Implementation Roadmap

### Phase 1 ✅ (Current)
- Repository foundation
- Architecture definition
- Core interfaces and abstractions
- Database models
- Configuration management
- Test infrastructure

### Phase 2 (Next)
- Market data engine implementation
- WebSocket connection management
- Exchange adapter (at least one, e.g., Coinbase sandbox)
- Data normalization pipeline
- REST fallback implementation

### Phase 3
- Backtesting engine completion
- Historical data loading
- Period separation enforcement
- Walk-forward analysis

### Phase 4
- Initial strategy plugins (2-3 strategies)
- Strategy registration system
- Signal generation pipeline

### Phase 5
- Opportunity ranking engine
- Scoring algorithm tuning
- Correlation computation

### Phase 6
- Risk engine completion
- Circuit breaker logic
- Kill switch implementation
- Stop-loss management

### Phase 7
- Execution engine
- Order state machine
- Idempotency and reconciliation
- Exchange adapter order methods

### Phase 8
- Paper trading integration
- End-to-end paper trading flow
- Realistic simulation parameters

### Phase 9
- Dashboard completion
- Analytics tracking
- Alert system

### Phase 10
- Comprehensive testing
- Failure simulation
- Performance profiling
- Security audit
- Documentation finalization

---

## 10. Configuration

Configuration layers (in priority order):
1. `config/default.yaml` — safe defaults
2. Environment variables (`.env`) — secrets, deployment-specific
3. Runtime overrides (dashboard/API) — not persisted

The safety gate for live trading requires BOTH:
- `MODE=live` in environment
- `LIVE_TRADING_ENABLED=true` in environment
