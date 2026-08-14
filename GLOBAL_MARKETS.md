# Global Market Mode

## Crypto venues

The global paper runner uses public CCXT data from:

- Binance
- KuCoin
- OKX
- Bybit
- Gate.io
- MEXC

It discovers active USDT spot markets, removes stablecoin/leveraged-token pairs,
filters current volume and spread, and assigns each base asset to one best venue.
This prevents duplicate directional exposure such as buying ACE on three venues.
If a venue is blocked or unavailable, the remaining venues continue.

```bash
python scripts/run_global_paper.py \
  --duration 43200 \
  --initial-balance 10000 \
  --max-global-symbols 500 \
  --fresh-db
```

The total paper balance is partitioned among venues in proportion to their
selected unique assets. Each venue writes a separate database:
`data/global_v1_<venue>.db`.

## OANDA Practice (FX and gold)

The read-only pricing adapter supports XAU/USD and major FX pairs. Configure
credentials locally; never commit or paste them into chat:

```bash
export OANDA_ENV=practice
export OANDA_ACCOUNT_ID='your-practice-account-id'
export OANDA_API_TOKEN='your-practice-token'
```

The OANDA connector is pricing-only in this version. It is deliberately not fed
into crypto order-book execution because OANDA pricing does not provide the same
visible-depth model. A separate FX/metals paper execution model is required
before those prices can generate simulated trades.

## Important limitations

- “All markets” means configured and accessible providers, not every market in existence.
- More venues and symbols increase opportunities, API load, and false signals.
- A profitable manual trade after a move does not prove the move was predictable beforehand.
- No live order methods are exposed by the global runner.
