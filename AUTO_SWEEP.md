# Auto-Sweep

## Simulated Profit-Sweeping Policy Engine

---

## 1. Status

**AUTO-SWEEP EXECUTION = DISABLED**

This module generates `SweepRecommendation` objects only.
No real withdrawals occur. No exchange transfers.

---

## 2. Trigger Condition (Level 4 only)

When `total_balance` exceeds the active capital cap ($5,000,000 default):

```
excess_capital = max(0, total_balance - active_capital_cap)
positive_daily_profit = max(0, daily_realized_profit)
sweep_eligible = min(excess_capital, positive_daily_profit)

# Additional deductions:
# - Capital reserved for open positions
# - Capital reserved for open orders
# - Minimum sweep amount threshold
```

---

## 3. Rules

| Rule | Enforcement |
|------|------------|
| Never sweep unrealized profit | ✅ Enforced (only daily_realized_profit counts) |
| Never sweep borrowed funds | ✅ Enforced (no leverage, no borrow) |
| Never sweep capital for open positions | ✅ Enforced (reserved capital subtracted) |
| Never auto-execute | ✅ Enforced (approval_required = True) |
| Minimum sweep threshold | ✅ Enforced ($100 default) |

---

## 4. Destination Types

| Type | Description |
|------|-------------|
| CASH_RESERVE | Internal cash buffer |
| SECURE_WALLET | Cold/external secure wallet |
| EXTERNAL_TREASURY | External treasury account |
| GOLD_ALLOCATION | Gold/gold-linked allocation |
| FX_RESERVE | Forex reserve allocation |
| MANUAL_REVIEW | **Default** — human operator must decide |

---

## 5. SweepRecommendation

Generated per evaluation cycle. Contains:

- `sweep_id`: Unique identifier
- `eligible_amount`: USD amount eligible for sweep
- `reason`: Why the sweep was triggered
- `destination`: Where funds would go
- `status`: PENDING / APPROVED / REJECTED
- `approval_required`: Always True
- Portfolio context: balance, excess, profit, reserved capital

---

## 6. Lifecycle

```
SweepEngine.evaluate()
    → SweepRecommendation (status=PENDING)
    → Human reviews recommendation
    → Human calls mark_approved() or mark_rejected()
    → If approved: for future implementation — actual transfer
    → For now: END — no real action
```

---

## 7. Future Live Sweep Requirements

Before any real sweep can execute:

1. Explicit separate configuration (`SWEEP_EXECUTION_ENABLED=true`)
2. Whitelist destination addresses (on-exchange or external)
3. Additional approval gate (multi-sig or dual-approval)
4. Separate API permissions (transfer-only key, IP-restricted)
5. Full audit log persisted to database
6. Risk checks: balance verification, destination validation
7. Maximum sweep-per-transaction limit
8. Cooldown period between sweeps
