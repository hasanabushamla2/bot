# Operations

## Quant Opportunity Engine — Operational Guide

---

## 1. Quick Start

```bash
# Clone and enter
cd bot

# Create .env from template
cp .env.example .env
# Edit .env with your settings (exchange API keys, etc.)

# Install dependencies
make dev-install

# Initialize database (requires PostgreSQL running)
make init-db

# Run tests
make test

# Start paper trading
make run-paper

# Open dashboard
# http://localhost:8080
```

---

## 2. Docker Deployment

```bash
# Build and start all services (PostgreSQL, Redis, Engine)
docker compose up -d

# View logs
docker compose logs -f engine

# Stop
docker compose down
```

---

## 3. Daily Operations

### Morning Checklist
- [ ] Dashboard shows "PAPER MODE" (or "LIVE MODE" if live).
- [ ] All data streams healthy (no stale indicators).
- [ ] No circuit breaker active.
- [ ] Kill switch inactive.
- [ ] All strategies enabled as expected.
- [ ] Review overnight P&L.
- [ ] Check for errors in logs.

### During Trading
- Monitor equity curve on dashboard.
- Watch for unusual drawdowns.
- Check opportunity count — zero opportunities may indicate data issues.
- Verify fill rates are as expected.

### End of Day
- Review daily metrics.
- Check fee totals.
- Verify strategy performance breakdown.
- Export daily report if needed.

---

## 4. Emergency Procedures

### Trip Kill Switch
```python
# Via dashboard or programmatically
risk_engine.trip_kill_switch("manual")
```

### Cancel All Orders
```python
for order in execution_engine.get_active_orders():
    await execution_engine.cancel_order(order.exchange, order.exchange_order_id, order.symbol)
```

### Circuit Breaker Reset
```python
risk_engine.reset_circuit_breaker()
# Verify reason for trip before resetting
```

---

## 5. Database Maintenance

```bash
# Run migrations
make migrate

# Backup database
pg_dump bot_db > backup_$(date +%Y%m%d).sql

# Vacuum (periodic)
psql bot_db -c "VACUUM ANALYZE;"
```

---

## 6. Logs

Logs are structured JSON by default. Use `jq` for filtering:

```bash
# View recent errors
cat logs/engine.log | jq 'select(.level == "ERROR")'

# View order placements
cat logs/engine.log | jq 'select(.event == "order_placed")'
```

---

## 7. Monitoring

The dashboard at `http://localhost:8080` shows:
- Real-time equity and P&L.
- Active positions.
- Current opportunities.
- Strategy performance.
- System health.

Health endpoint: `GET /health`
API state: `GET /api/state`

---

## 8. Configuration Changes

1. Edit `.env` or `config/default.yaml`.
2. Changes take effect on next restart.
3. Some values can be changed at runtime via the dashboard (not persisted across restarts).

---

## 9. Adding a New Strategy

1. Create a new Python module in `src/strategies/`.
2. Subclass `BaseStrategy` from `src/strategies/base.py`.
3. Implement `strategy_id`, `strategy_name`, and `analyze()`.
4. Register in the strategy registry at startup.
5. Add tests in `tests/test_strategies/`.
6. Backtest before enabling in paper mode.

---

## 10. Adding a New Exchange

1. Create a new adapter in `src/adapters/` (e.g., `src/adapters/crypto/ftx.py`).
2. Subclass `ExchangeAdapter` from `src/adapters/base.py`.
3. Implement all abstract methods.
4. Register the adapter in the engine initialization.
5. Add tests with mocked exchange responses.

---

## 11. Performance Tuning

- Monitor `avg_execution_latency_ms` and `avg_signal_to_order_latency_ms`.
- If backtesting, use `pyarrow` for large datasets.
- Consider Redis for caching frequently-queried data.
- Database connection pool size can be tuned in config.

---

## 12. Troubleshooting

| Symptom | Possible Cause | Action |
|---------|---------------|--------|
| Zero opportunities | Data feed down | Check data health on dashboard |
| All orders rejected | Risk engine trip | Check circuit breaker / kill switch |
| High latency | Network issue | Check exchange connectivity |
| Database errors | Connection lost | Check PostgreSQL is running |
| Dashboard not loading | Port conflict | Check DASHBOARD_PORT config |
