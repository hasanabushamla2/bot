"""Order-Flow / Order-Book Imbalance Strategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core.logging_config import get_logger
from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal

logger = get_logger(__name__)


class OrderFlowStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "order_flow_v1"

    @property
    def strategy_name(self) -> str:
        return "Order-Flow / OBI v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < 10:
            return None
        score = 0.0
        if features.bid_ask_ratio > 1.2:
            score += min(1.0, (features.bid_ask_ratio - 1.0) / 1.0) * 0.35
        if features.trade_flow_ratio > 1.5:
            score += min(1.0, (features.trade_flow_ratio - 1.0) / 2.0) * 0.35
        if features.momentum_1m > 0:
            score += min(1.0, features.momentum_1m / 2.0) * 0.15
        if features.spread_bps < 20:
            score += 0.15
        if score < 0.25:
            return None
        direction = SignalDirection.LONG
        return StrategySignal(
            strategy_id=self.strategy_id,
            symbol=features.symbol,
            direction=direction,
            confidence=score,
            estimated_return=features.momentum_1m * 0.5,
            estimated_risk=features.volatility_5m_pct,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            entry_logic={
                "type": "order_flow",
                "bid_ask_ratio": features.bid_ask_ratio,
                "trade_flow": features.trade_flow_ratio,
            },
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.15,
                "activation_pct": 0.15,
                "no_fixed_take_profit": True,
            },
            metadata={
                "bid_ask_ratio": features.bid_ask_ratio,
                "trade_flow_ratio": features.trade_flow_ratio,
                "stop_loss_pct": 0.30,
            },
        )
