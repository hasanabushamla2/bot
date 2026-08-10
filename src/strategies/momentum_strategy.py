"""Momentum / Price Acceleration Strategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core.logging_config import get_logger
from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal

logger = get_logger(__name__)


class MomentumStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "momentum_v1"

    @property
    def strategy_name(self) -> str:
        return "Momentum / Price Acceleration v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < 20:
            return None
        momentum_score = 0.0
        if features.momentum_1m > 0:
            momentum_score += min(1.0, features.momentum_1m / 2.0) * 0.4
        if features.momentum_5m > 0:
            momentum_score += min(1.0, features.momentum_5m / 5.0) * 0.3
        if features.acceleration > 0:
            momentum_score += min(1.0, features.acceleration / 1.0) * 0.2
        if features.trend_strength > 0:
            momentum_score += features.trend_strength * 0.1
        if momentum_score < 0.3:
            return None
        direction = SignalDirection.LONG if features.trend_strength > 0 else SignalDirection.NEUTRAL
        if direction == SignalDirection.NEUTRAL:
            return None
        return StrategySignal(
            strategy_id=self.strategy_id,
            symbol=features.symbol,
            direction=direction,
            confidence=momentum_score,
            estimated_return=features.momentum_5m,
            estimated_risk=features.volatility_5m_pct,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            entry_logic={"type": "momentum", "score": momentum_score},
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.20,
                "activation_pct": 0.20,
                "no_fixed_take_profit": True,
            },
            metadata={
                "momentum_1m": features.momentum_1m,
                "momentum_5m": features.momentum_5m,
                "trend": features.trend_strength,
                "stop_loss_pct": 0.30,
            },
        )
