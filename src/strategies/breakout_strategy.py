"""Volume Spike / Breakout Strategy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core.logging_config import get_logger
from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal

logger = get_logger(__name__)


class BreakoutStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "breakout_v1"

    @property
    def strategy_name(self) -> str:
        return "Volume Spike / Breakout v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < 20:
            return None
        score = 0.0
        if features.breakout_position_pct > 80:
            score += min(1.0, (features.breakout_position_pct - 80) / 20) * 0.3
        if features.relative_volume > 2.0:
            score += min(1.0, (features.relative_volume - 1.0) / 4.0) * 0.3
        if features.momentum_1m > 0:
            score += min(1.0, features.momentum_1m / 3.0) * 0.2
        if features.trend_strength > 0:
            score += features.trend_strength * 0.2
        if score < 0.35:
            return None
        direction = SignalDirection.LONG if features.trend_strength > 0 else SignalDirection.NEUTRAL
        if direction == SignalDirection.NEUTRAL:
            return None
        return StrategySignal(
            strategy_id=self.strategy_id,
            symbol=features.symbol,
            direction=direction,
            confidence=score,
            estimated_return=features.momentum_1m,
            estimated_risk=features.volatility_5m_pct,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            entry_logic={
                "type": "breakout",
                "breakout_pos": features.breakout_position_pct,
                "rel_vol": features.relative_volume,
            },
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.15,
                "activation_pct": 0.15,
                "no_fixed_take_profit": True,
            },
            metadata={
                "breakout_pct": features.breakout_position_pct,
                "rel_volume": features.relative_volume,
                "stop_loss_pct": 0.30,
            },
        )
