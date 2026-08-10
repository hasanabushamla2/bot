# Asset Universe

## Dynamic, Data-Driven Tradable Universe

---

## 1. Philosophy

The system has **NO loyalty** to any asset, asset class, or exchange.
An instrument becomes eligible purely on measurable data — volume,
liquidity, spread, depth, execution quality, and data integrity.

**No coin-name lists. No social-popularity filters. No market-cap biases.**

---

## 2. Quality Filter Pipeline

```
Exchange Instruments → UniverseManager → AssetQualityFilter.assess()
    → Quality Tier (A/B/C/D)
    → select_for_tier() → Capital Allocator
```

Every instrument is **re-evaluated continuously** as market data arrives.

---

## 3. Quality Tiers

| Tier | Description | Example (subject to data) |
|------|-------------|--------------------------|
| TIER_A | Deep liquidity, tight spreads | BTC, ETH, top-tier assets |
| TIER_B | Highly liquid established | SOL, XRP, BNB, MATIC, etc. |
| TIER_C | Qualified medium-liquidity | Active altcoins passing all checks |
| TIER_D | Rejected | Illiquid, wide spread, stale data |

### Tier Reclassification

- if liquidity improves → TIER_C → TIER_B
- if liquidity deteriorates → TIER_B → TIER_C or SUSPENDED
- if data becomes stale → SUSPENDED regardless of tier

---

## 4. Universe Size

The system does not target a fixed number of instruments.

```
UniverseManager discovers all eligible Spot markets
    → filters by AssetQualityFilter
    → result may be 20, 50, 100, 200+ instruments
```

Report the **actual count**, not a target.

---

## 5. Capital-Tier Universe Rules

| Capital Level | Allowed Tiers | Rationale |
|---------------|--------------|-----------|
| LEVEL_1 (≤ $5K) | A, B, C | Full flexibility, small account |
| LEVEL_2 (≤ $100K) | A, B, C | Diversification via many instruments |
| LEVEL_3 (≤ $5M) | A, B only | Focus on deep liquidity |
| LEVEL_4 (> $5M) | A, B only | Max liquidity, minimum impact |

---

## 6. Future Asset Classes

The `AssetClass` enum supports expanding to:
- GOLD (XAU/USD via supported APIs)
- FX_SPOT (major/minor pairs)
- Additional instruments

Each new class requires: adapter, normalization, quality filter thresholds.
