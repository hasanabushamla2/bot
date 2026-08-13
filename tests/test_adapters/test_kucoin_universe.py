"""Pure selection tests for the feed-budget-aware KuCoin universe."""

from __future__ import annotations

from src.adapters.crypto.kucoin import KuCoinPublicAdapter


def _symbol(symbol: str, *, base: str, enabled: bool = True) -> dict[str, object]:
    return {
        "symbol": symbol,
        "baseCurrency": base,
        "quoteCurrency": "USDT",
        "enableTrading": enabled,
    }


def test_ranked_liquid_universe_enforces_current_volume_spread_and_cap() -> None:
    adapter = KuCoinPublicAdapter()
    symbols = [
        _symbol("BTC-USDT", base="BTC"),
        _symbol("HIGHVOL-USDT", base="HIGHVOL"),
        _symbol("LOWVOL-USDT", base="LOWVOL"),
        _symbol("WIDESPREAD-USDT", base="WIDESPREAD"),
        _symbol("USDC-USDT", base="USDC"),
        _symbol("DISABLED-USDT", base="DISABLED", enabled=False),
    ]
    tickers = {
        "BTC-USDT": {"bid": 100.0, "ask": 100.1, "volume_24h_usd": 1_000_000.0},
        "HIGHVOL-USDT": {"bid": 10.0, "ask": 10.01, "volume_24h_usd": 2_000_000.0},
        "LOWVOL-USDT": {"bid": 5.0, "ask": 5.01, "volume_24h_usd": 10_000.0},
        "WIDESPREAD-USDT": {"bid": 5.0, "ask": 5.20, "volume_24h_usd": 3_000_000.0},
    }

    selected = adapter.rank_liquid_usdt_pairs(
        symbols,
        tickers,
        min_volume_usd=100_000.0,
        max_symbols=2,
        max_spread_bps=35.0,
    )

    # Priority core BTC remains available, while the cap prevents a book feed
    # from accepting every metadata-listed pair.
    assert selected == ["BTC-USDT", "HIGHVOL-USDT"]


def test_ranked_liquid_universe_returns_empty_when_no_live_candidate_is_safe() -> None:
    adapter = KuCoinPublicAdapter()
    selected = adapter.rank_liquid_usdt_pairs(
        [_symbol("AAA-USDT", base="AAA")],
        {"AAA-USDT": {"bid": 0.0, "ask": 0.0, "volume_24h_usd": 1_000_000.0}},
        max_symbols=100,
    )
    assert selected == []
