# Backtesting

## Quant Opportunity Engine — Backtesting Framework

---

## 1. Core Principle

**Never trust a backtest that wasn't designed to fail.**

The backtesting engine is built with anti-bias protections as a first-class feature, not an afterthought.

---

## 2. Anti-Bias Protections

### 2.1 Look-Ahead Bias Prevention

The backtest engine iterates through data chronologically. At each time step `t`, the strategy function receives ONLY data up to and including `t`. The engine physically slices the DataFrame to time `t` before calling the strategy.

```python
# Only data up to current row — NO future information
market_snapshot = data.iloc[: i + 1].copy()
signal = strategy_fn(market_snapshot)
```

### 2.2 Period Separation

Backtests enforce strict separation of:
- **Training period**: Used for strategy development and parameter optimization.
- **Validation period**: Used for model selection and hyperparameter tuning.
- **Test period**: COMPLETELY UNTOUCHED until final evaluation.

The `BacktestConfig` validates that `train_end < validation_end < test_start`. Overlapping periods raise a `ValueError`.

### 2.3 Walk-Forward Analysis

Walk-forward testing uses rolling windows:
- Train on previous N days.
- Test on next M days.
- Roll forward.
- Never uses future data for training.

### 2.4 Survivorship Bias

- Historical data should include delisted instruments where possible.
- The database schema supports tracking inactive instruments.

### 2.5 Data Snooping

- Parameter optimization MUST occur only on the training period.
- The test period is a one-time evaluation.
- Multiple testing on the same test period constitutes data snooping.

---

## 3. Realistic Execution Simulation

### 3.1 Fees

Both maker and taker fees are applied:
- **Entry**: Taker fee on notional value.
- **Exit**: Taker fee on notional value.
- Default: 0.1% each way (0.2% round trip).

### 3.2 Slippage

Slippage is applied to every fill:
- Default: 5 basis points.
- Buy orders: price * (1 + slippage).
- Sell orders: price * (1 - slippage).

### 3.3 Spread

When bid/ask data is available:
- Buys fill at the ask.
- Sells fill at the bid.
- Adds the spread cost to every round-trip.

### 3.4 Latency

Configurable execution latency assumption (default: 100ms). The backtest does NOT assume instant execution at the signal price.

### 3.5 Partial Fills

When volume data supports it, orders may be partially filled if the available volume at a given price level is insufficient.

---

## 4. Metrics Computed

| Metric | Description |
|--------|-------------|
| Total Trades | Number of round-trip trades |
| Win Rate | Winning trades / total trades |
| Gross P&L | Sum of all gross profits |
| Net P&L | Gross P&L - fees - slippage |
| Total Return % | Net P&L / initial capital * 100 |
| Profit Factor | Gross profit / gross loss |
| Max Drawdown % | Peak-to-trough decline |
| Sharpe Ratio | Risk-adjusted return (annualized) |
| Sortino Ratio | Downside risk-adjusted return |
| Expectancy | (win_rate * avg_win) - (loss_rate * avg_loss) |
| Avg Trade Return % | Mean return per trade |
| Avg Win % | Mean return of winning trades |
| Avg Loss % | Mean return of losing trades |
| Equity Curve | List of equity values at each step |

---

## 5. Monte Carlo / Bootstrap Analysis

Future enhancement: The backtesting engine supports running multiple simulations with bootstrapped trade sequences to produce confidence intervals on performance metrics.

---

## 6. Usage

```python
from src.backtesting.engine import BacktestConfig, BacktestEngine, PeriodType

config = BacktestConfig(
    symbol="BTC-USD",
    start_date=datetime(2024, 1, 1),
    end_date=datetime(2024, 12, 31),
    initial_capital=10000.0,
    train_end=datetime(2024, 6, 1),
    validation_end=datetime(2024, 9, 1),
    test_start=datetime(2024, 9, 1),
)

engine = BacktestEngine(config)
result = engine.run(data, my_strategy_fn, period_type=PeriodType.TEST)

# Walk-forward
results = engine.walk_forward(data, my_strategy_fn, train_window_days=90, test_window_days=30)
```

---

## 7. Rules

- ❌ NEVER optimize on the test period.
- ❌ NEVER exclude losing trades from metrics.
- ❌ NEVER exclude fees to improve performance.
- ❌ NEVER use future information.
- ❌ NEVER silently modify historical data.
- ❌ NEVER hard-code profit results.
- ❌ NEVER claim a strategy works without evidence.

---

## 8. Capacity Testing & Strategy Capacity Reports

The backtesting system supports capacity analysis — estimating how strategy performance degrades as capital grows.

For each strategy, estimate:
- Expected return at different capital levels ($1k, $10k, $100k, $1M)
- Slippage at different order sizes
- Liquidity limitations
- Degradation of edge as capital grows

The `CapacityEstimator.capacity_report()` generates a `STRATEGY_CAPACITY` report:

```
Strategy A:  efficient at $1k, $10k;  degrades at $100k;  not viable at $1M
Strategy B:  efficient at $1k, $10k, $100k;  marginal at $1M
```

**Numbers must come from data/simulation, never fabricated.** Actual thresholds validated in paper trading before live use.

---

## 9. Validation Checklist

Before trusting any backtest result:

- [ ] Train/validation/test periods are strictly separated.
- [ ] Test period has NEVER been used for optimization.
- [ ] Fees are non-zero when trades exist.
- [ ] Net P&L < Gross P&L.
- [ ] Slippage is applied.
- [ ] Stop losses trigger correctly.
- [ ] Walk-forward results are consistent across windows.
- [ ] Parameter stability analysis has been performed.
