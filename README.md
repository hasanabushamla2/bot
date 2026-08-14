# Quant Opportunity Engine

**Multi-Market / Multi-Strategy Algorithmic Trading System**

Version: 0.1.0 | Phase 1 — Architecture & Foundation

---

## Overview

A modular, extensible, real-time quantitative trading engine designed to:
- Continuously scan supported markets 24/7.
- Detect and rank high-quality trading opportunities across multiple strategies.
- Execute only opportunities that pass strict expected-value and risk checks.
- Support realistic backtesting and paper trading before any live-money functionality.

**Currently in Phase 1: Architecture definition and repository foundation.**

---

## Architecture

```
Market APIs → Data Engine → Scanner → Feature Engine → Strategy Plugins
    → Opportunity Ranking → Risk Engine (Gate) → Execution Engine
    → Exchange/Broker → Position Monitoring → Database/Event Store
    → Analytics/Dashboard/Alerts
```

Every component is modular and independently testable. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick Start

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Start dashboard (development)
uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8080 --reload

# Full system with Docker
docker compose up -d
```

---

## Project Structure

```
bot/
├── src/
│   ├── core/           # Configuration, exceptions, logging
│   ├── data/           # Real-time data engine
│   ├── adapters/       # Exchange adapter interfaces
│   ├── strategies/     # Strategy plugin system
│   ├── opportunity/    # Opportunity ranking engine
│   ├── risk/           # Risk engine (mandatory gate)
│   ├── execution/      # Order execution & state machine
│   ├── backtesting/    # Backtesting with anti-bias protections
│   ├── paper/          # Paper trading (same interfaces as live)
│   ├── analytics/      # Performance metrics tracking
│   ├── dashboard/      # Monitoring dashboard
│   └── db/             # Database models & repository
├── tests/              # Comprehensive test suite
├── config/             # YAML configuration files
├── scripts/            # Utility scripts
├── docs/               # Additional documentation
└── migrations/         # Alembic migrations
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system architecture and design decisions |
| [SECURITY.md](SECURITY.md) | Security architecture and checklist |
| [RISK_MODEL.md](RISK_MODEL.md) | Risk management framework |
| [BACKTESTING.md](BACKTESTING.md) | Backtesting engine and anti-bias protections |
| [PAPER_TRADING.md](PAPER_TRADING.md) | Paper trading system design |
| [OPERATIONS.md](OPERATIONS.md) | Daily operations guide |

---

## Safety

- **Live trading is DISABLED by default.**
- Two independent conditions must be true: `MODE=live` AND `LIVE_TRADING_ENABLED=true`.
- API keys require trading permission only — NO withdrawals.
- Secrets are NEVER hard-coded or logged.

---

## Development

```bash
# Lint
make lint

# Format
make format

# Type check
make type-check

# Full check
make check

# Run specific test suite
pytest tests/test_execution/ -v
```

---

## Technology Stack

Python 3.11+ | asyncio | FastAPI | PostgreSQL | Redis | SQLAlchemy | Pydantic | structlog | NumPy/SciPy/Pandas | Docker

See [ARCHITECTURE.md](ARCHITECTURE.md) for rationale.

---

## License

MIT

---

## Status

🟢 **Phase 1 COMPLETE** — Architecture, interfaces, database models, test infrastructure.

⬜ Phase 2 — Market data engine implementation (NOT STARTED — awaiting authorization).

## High-Activity Paper Profile

The KuCoin public-data runner is simulation-only and defaults to the explicit
`aggressive-paper` profile. It can deploy up to 100% of the simulated balance,
dividing available cash by the number of currently qualified opportunities (up
to 20 positions, with at least two symbols required for full deployment). It
keeps 1x spot-only execution, hard stops, liquidity/cost checks, correlation
controls, and the circuit breaker enabled.

```bash
# Broad liquid universe (default cap: 300), simulated $10,000
python scripts/run_live_paper.py --duration 3600 --fresh-db

# Every currently eligible pair; the order-book refresh batch scales with size
python scripts/run_live_paper.py --duration 3600 --max-symbols 0 --fresh-db

# Conservative allocation profile
python scripts/run_live_paper.py --profile safe --duration 3600 --fresh-db
```

The aggressive-paper profile is quality-first: it requires at least 0.65 signal
confidence, stronger normalized entry confirmation, and estimated gross reward
of at least 1.2 times the hard stop plus modeled round-trip costs. Its pre-screen
allows shallower altcoin books to reach execution-aware sizing, where quantity
is reduced to safe visible depth. Two consecutive losses pause a strategy for
one hour.

A larger universe and faster two-second scan cadence create more opportunities;
they do not guarantee more trades, a particular win rate, or profitability.
Evaluate results over multiple market regimes using the persisted paper database
before changing any entry-quality threshold.
