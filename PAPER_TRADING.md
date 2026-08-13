# Paper Trading

## Quant Opportunity Engine — Paper Trading System

---

## 1. Design Principle

**Paper trading uses the SAME code paths as live trading.**

There is no simplified "paper-only" fast path. The same:
- Strategy plugins
- Opportunity Engine
- Risk Engine
- Execution interface (with simulated fills)
- Order lifecycle state machine
- Analytics tracking

...operate in both paper and live mode. The ONLY difference is that paper mode simulates fills against live market data instead of sending orders to an exchange.

---

## 2. Why This Matters

Most trading systems create a separate paper trading implementation that:
- Fills every order instantly at the mid-price.
- Ignore slippage.
- Ignore partial fills.
- Have unrealistic latency.

This produces paper results that look great but have zero predictive value for live performance. **We reject this approach.**

---

## 3. Paper Execution Engine

### 3.1 Fill Simulation

- **Market orders**: Fill at current bid (sells) or ask (buys) with configurable slippage.
- **Limit orders**: Fill only if limit price crosses the spread. Otherwise remain OPEN.
- **Rejected orders**: Reject if price is stale, balance insufficient, or market data missing.

### 3.2 Cost Simulation

- **Fees**: Taker fee applied to every fill (default 0.1%).
- **Slippage**: Configurable slippage in basis points (default 5 bps).
- **Latency**: Configurable simulated latency (default 50ms).

### 3.3 Paper Account

The paper account starts with a configurable balance and tracks:
- Balance (available + reserved).
- Realized P&L.
- Total fees paid.
- Equity curve.

---

## 4. Running Paper Trading

```bash
# Start full paper trading system
make run-paper

# Or with Docker
docker compose up -d
```

The system will:
1. Connect to configured exchanges (WebSocket + REST).
2. Subscribe to configured symbols.
3. Run all enabled strategy plugins.
4. Score and rank opportunities.
5. Pass approved opportunities through the risk engine.
6. Simulate order execution.
7. Track all metrics.
8. Serve the dashboard at http://localhost:8080.

---

## 5. Paper Mode Safety

Paper mode:
- NEVER sends orders to any exchange.
- NEVER requires real API keys (can run with mock data).
- Is clearly labeled on the dashboard with a green "PAPER MODE" badge.
- Cannot be confused with live mode.

---

## 6. Transitioning to Live

Before enabling live trading:
1. Run paper trading for a statistically significant period (weeks, not hours).
2. Verify strategy performance aligns with backtest results.
3. Verify fill rates, slippage estimates, and fee calculations.
4. Validate `STRATEGY_CAPACITY` reports from the Capacity Estimator against observed paper fills.
5. Confirm the Capital Allocator is properly distributing across opportunities as equity grows.
6. Complete the security checklist in SECURITY.md.
7. Set `MODE=live` AND `LIVE_TRADING_ENABLED=true`.
8. Start with minimal position sizes.
9. Monitor closely for the first 24 hours.

---

## 7. One-hour execution/risk soak

Use a clean database and the public-market paper runner. `--fresh-db` removes
only the selected SQLite database (and its WAL/SHM sidecars) before startup.
It does not change fees, slippage, sizing, strategy logic, or risk rules.

```powershell
python .\scripts\run_live_paper.py --duration 3600 --experiment-id full_soak_1h --db-path data/full_soak_1h.db --fresh-db
```

Inspect a fact-only snapshot from another PowerShell window:

```powershell
python .\scripts\analyze_paper_run.py data\full_soak_1h.db
Get-Content .\logs\engine.log -Tail 100 -Wait
```

The report separates reference-price gross PnL, fees, modeled slippage, and
net PnL. A one-hour run is an execution-health gate, not evidence that the
strategy is profitable.

### Execution/risk configuration

| Environment variable | Default | Meaning |
|---|---:|---|
| `LOSS_COOLDOWN_SECONDS` | `300` | Minimum same-symbol delay after a net loss |
| `WIN_COOLDOWN_SECONDS` | `30` | Shorter delay after a net win |
| `TRAIL_ACTIVATION_PCT` | `0.20` | Configured activation floor; runtime raises it when costs require |
| `TRAIL_DISTANCE_PCT` | `0.20` | Minimum favorable high-water-mark trail distance |
| `TRAIL_VOLATILITY_MULTIPLIER` | `1.50` | Widens a per-position trail by realized volatility; never tightens below the floor |
| `TRAIL_SPREAD_MULTIPLIER` | `2.00` | Includes current bid/ask microstructure noise in the trail distance |
| `MAX_TRAIL_DISTANCE_PCT` | `1.25` | Safety cap for a volatility-expanded trail; no fixed profit ceiling is introduced |
| `MATERIAL_REENTRY_CONFIDENCE_IMPROVEMENT` | `0.10` | Required confidence improvement for an early re-entry after a profitable trail |
| `MIN_REENTRY_MARKET_STRUCTURE_SCORE` | `0.55` | Minimum trend/breakout/flow structure score for that early re-entry |
| `MAX_CONSECUTIVE_LOSSES_PER_SYMBOL` | `2` | Loss count that triggers temporary lockout |
| `SYMBOL_LOCKOUT_SECONDS` | `1800` | Temporary lockout after the threshold |
| `SYMBOL_LOSS_STREAK_RESET_SECONDS` | `21600` | Inactivity interval that decays a loss streak |
| `MIN_EXPECTED_EDGE_OVER_COST` | `0.001` | Required expected-return margin over estimated round-trip cost |
| `PAPER_TAKER_FEE` | `0.001` | Fee rate on each simulated market fill |
| `PAPER_MAKER_FEE` | `0.001` | Maker fee setting (market path currently uses taker rate) |
| `PAPER_SLIPPAGE_BPS` | `5.0` | Adverse modeled slippage applied on each side |
| `PAPER_SIMULATED_LATENCY_MS` | `50.0` | Simulated order latency |

A consumed signal remains blocked after cooldown until its strategy condition
has been observed false and subsequently becomes valid again. Explicit stable
`signal_id` values from event-driven strategy plugins are also supported.
Cooldown and signal-consumption state are persisted in the soak database.

### Adaptive re-entry and entry quality

A losing hard-stop is never bypassed: its cooldown is scaled from realized loss
severity, stop-out sequence, and live volatility. A profitable `trail_hit` can
re-enter before the short win cooldown expires only when all of the following
are true:

1. The guard observed a new signal sequence, rather than the prior continuous
   predicate.
2. The new confidence is materially stronger than the confidence used for the
   prior entry (with a volatility-aware uplift).
3. The new signal is directionally aligned and its normalized market-structure
   score (trend, momentum, breakout/flow confirmation) remains sufficient.

The pre-entry quality gate uses volatility-normalized momentum, trend,
breakout/range confirmation, live volume/liquidity, spread, visible-book
imbalance when available, signal persistence, and short-term reversal risk.
It does not inspect future candles. It does not alter hard stops, leverage, or
position-size limits.

### Session diagnostic report

Every paper-session result exposes these structured sections, and
`analyze_paper_run.py` renders them for durable SQLite runs:

- **SIGNAL FUNNEL:** raw, qualified, opportunities, approved, entries, and
  closed trades plus explicit counters for every filtering stage.
- **REJECTION BREAKDOWN:** primary reason counts and percentages; no silent
  capacity, open-symbol, scanner, or entry gate path.
- **TRADE PERFORMANCE:** gross PnL, fees, slippage, net PnL, profit factor,
  expectancy, winners/losers, MFE/MAE, and holding durations.
- **EXIT / STRATEGY / SYMBOL ANALYSIS:** hard-stop and trail performance,
  strategy telemetry and allocation evidence, and per-symbol outcomes.
- **THROUGHPUT / RISK HEALTH:** opportunities/hour, qualified/hour,
  entries/hour, net expectancy, profit factor, and drawdown.

## 8. Paper Trading Metrics

Same metrics as live trading:
- Win rate, P&L, Sharpe, Sortino, drawdown.
- Fill ratio, rejection rate.
- Latency measurements.
- Strategy/market/exchange breakdowns.

If paper results differ significantly from backtest results, investigate before going live.
