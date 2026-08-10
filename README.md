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
