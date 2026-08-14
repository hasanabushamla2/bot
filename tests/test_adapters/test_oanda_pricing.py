"""Tests for the read-only OANDA pricing connector."""

from __future__ import annotations

import pytest

from src.adapters.fx.oanda import OandaPracticePricingAdapter, OandaPrice


def test_oanda_price_normalizes_gold_symbol() -> None:
    from datetime import UTC, datetime

    price = OandaPrice("XAU_USD", 2500.0, 2500.5, datetime.now(UTC), True)
    assert price.canonical_symbol == "XAU-USD"
    assert price.spread_bps > 0


@pytest.mark.asyncio
async def test_oanda_requires_local_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    adapter = OandaPracticePricingAdapter(token="", account_id="")
    with pytest.raises(RuntimeError, match="OANDA_API_TOKEN"):
        await adapter.connect()
