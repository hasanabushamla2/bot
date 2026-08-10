"""Real Fee Service — discovers actual exchange trading fees."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)

@dataclass
class RealFeeSchedule:
    symbol: str = ""
    maker_fee: float = 0.0010
    taker_fee: float = 0.0010
    source: str = "fallback"
    buy_fee_estimated: float = 0.0010
    sell_fee_estimated: float = 0.0010
    round_trip_pct: float = 0.0020

@dataclass
class RealFeeResult:
    symbol: str = ""
    buy_fee_paid: float = 0.0
    sell_fee_paid: float = 0.0
    total_fees: float = 0.0
    fee_currency: str = ""
    buy_fee_pct: float = 0.0
    sell_fee_pct: float = 0.0
    from_actual_fill: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

class RealFeeService:
    def __init__(self, fallback_taker: float = 0.001, fallback_maker: float = 0.001) -> None:
        self.fallback_taker = fallback_taker
        self.fallback_maker = fallback_maker
        self._schedule_cache: dict[str, RealFeeSchedule] = {}

    def get_schedule(self, symbol: str = "default") -> RealFeeSchedule:
        if symbol not in self._schedule_cache:
            self._schedule_cache[symbol] = RealFeeSchedule(
                symbol=symbol, maker_fee=self.fallback_taker,
                taker_fee=self.fallback_taker, source="fallback",
                buy_fee_estimated=self.fallback_taker,
                sell_fee_estimated=self.fallback_taker,
                round_trip_pct=self.fallback_taker * 2,
            )
        return self._schedule_cache[symbol]

    def update_from_exchange(self, symbol: str, maker: float, taker: float, source: str = "api") -> None:
        sched = RealFeeSchedule(
            symbol=symbol, maker_fee=maker, taker_fee=taker, source=source,
            buy_fee_estimated=taker, sell_fee_estimated=taker,
            round_trip_pct=taker * 2,
        )
        self._schedule_cache[symbol] = sched
        logger.info("fee_schedule_updated", symbol=symbol, maker=maker, taker=taker, source=source)

    def compute_from_fills(self, symbol: str, buy_notional: float, sell_notional: float, buy_fee: float, sell_fee: float, fee_currency: str = "USD") -> RealFeeResult:
        result = RealFeeResult(
            symbol=symbol, buy_fee_paid=buy_fee, sell_fee_paid=sell_fee,
            total_fees=buy_fee + sell_fee, fee_currency=fee_currency,
            from_actual_fill=True,
        )
        if buy_notional > 0:
            result.buy_fee_pct = buy_fee / buy_notional * 100
        if sell_notional > 0:
            result.sell_fee_pct = sell_fee / sell_notional * 100
        return result
