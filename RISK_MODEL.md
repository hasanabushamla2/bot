# Risk Model

## Quant Opportunity Engine — Risk Management Framework

---

## 1. Philosophy

The Risk Engine is a **mandatory, independent gate** between opportunity detection and execution. No component can bypass it. Strategies do not call the execution engine — they produce signals that flow through the risk gate.

---

## 2. Risk Evaluation Pipeline

```
Opportunity → [Kill Switch Check] → [Circuit Breaker] → [Drawdown]
            → [Total Exposure] → [Per-Market Exposure] → [Per-Strategy]
            → [Stale Data] → [Capital Check] → [Position Sizing]
            → [Stop Loss Calculation] → APPROVED
```

Any gate can reject the opportunity. Rejections are logged with a specific reason code.

---

## 3. Risk Gates (In Order)

| Gate | Description | Default |
|------|-------------|---------|
| **Kill Switch** | Manual emergency stop | Inactive |
| **Circuit Breaker** | Auto-trips on drawdown or consecutive losses | 15% drawdown or 5 consecutive losses |
| **Max Drawdown** | Peak-to-trough drawdown limit | 10% |
| **Total Exposure** | Maximum total capital at risk | $10,000 |
| **Per-Strategy** | Max concurrent positions per strategy | 10 |
| **Stale Data** | Reject if signal expired | Signal expiration time |
| **Capital** | Must have sufficient balance | Position-dependent |
| **Position Sizing** | Size based on available capital and limits | Min($1,000, available) |

---

## 4. Position Sizing

Position size is the **minimum** of:
- Strategy-requested capital
- `max_position_size_usd` config value
- Available capital buffer (remaining exposure headroom)

If the computed position size is less than 50% of the requested size, the opportunity is rejected — the system won't take a meaningfully smaller position than the strategy intended.

---

## 5. Stop Loss Model

**Configurable per-position and per-strategy.**

Default research configuration:
```
Stop Loss: -0.3% per position
```

But this is NOT hard-coded. Each strategy can specify its own stop loss in the signal metadata.

Stop loss price calculation:
- **Long**: `entry_price * (1 - stop_loss_pct/100)`
- **Short**: `entry_price * (1 + stop_loss_pct/100)`

### Important: Per-Position vs Account-Level

A -0.3% stop on one position does NOT mean the entire account can only lose 0.3% per day. The risk engine calculates account-level exposure correctly:
- If 10 positions at 1% each have 0.3% stops, max daily loss could be 3% (if all hit stops simultaneously).
- The drawdown and circuit breaker limits operate at the account level.

---

## 6. Circuit Breaker

The circuit breaker trips automatically when:

1. **Drawdown threshold exceeded**: Current drawdown ≥ 15% (configurable).
2. **Consecutive losses**: 5 losing trades in a row (configurable).

When tripped:
- All new position entry is halted.
- Existing positions are NOT automatically closed (to avoid panic-selling).
- An alert is fired.
- Manual reset is required.

---

## 7. Kill Switch

The kill switch is a **manual emergency mechanism**:
- Tripped by operator action or by automated extreme-event detection.
- Immediately halts ALL new order placement.
- Does NOT automatically cancel open orders (manual decision).
- Requires explicit reset.

---

## 8. Correlation Management

The risk engine tracks correlation between open positions. If a new opportunity is highly correlated (>0.7) with existing positions, it receives a penalty in the opportunity score. The `max_correlated_exposure_pct` limit prevents over-concentration.

---

## 9. Leverage Controls

- Default max leverage: **1.0x** (no leverage).
- Futures/margin trading is disabled by default.
- If futures are ever enabled, `max_leverage` must be explicitly increased.
- Leverage is per-exchange and per-position.

---

## 10. Risk Reporting

The risk engine reports:
- Current total exposure.
- Per-market exposure breakdown.
- Per-strategy exposure breakdown.
- Current drawdown.
- Consecutive losses counter.
- Circuit breaker status.
- Kill switch status.

These are visible on the dashboard and logged periodically.

---

## 11. Configuration

All risk parameters are in `config/default.yaml` under the `risk:` section and can be overridden via environment variables (prefix `RISK_`).

Example env overrides:
```
RISK_MAX_POSITION_SIZE_USD=2000.0
RISK_MAX_TOTAL_EXPOSURE_USD=20000.0
RISK_DEFAULT_STOP_LOSS_PCT=0.5
RISK_CIRCUIT_BREAKER_DRAWDOWN_PCT=20.0
RISK_CIRCUIT_BREAKER_CONSECUTIVE_LOSSES=7
```

---

## 12. Research Note on Daily Return Targets

The system has a research KPI of approximately 2%–2.5% average daily net return, but:

- **This is a research target, not a hard-coded requirement.**
- The bot must NEVER force trades to hit a target.
- The bot must NEVER increase risk irresponsibly.
- If real results are lower or higher, report real results.
- There is NO daily profit cap — if positive-EV opportunities exist, keep trading.
- There is NO fixed trade-count target.
