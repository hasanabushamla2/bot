"""Liquid-alt, multi-horizon trend-continuation strategy.

This is a long-only spot strategy.  It deliberately requires a current-liquid
book and agreement between short/intermediate momentum rather than reacting to
a single tick.  The strategy is designed to provide an additional *distinct*
entry pathway; portfolio selection still decides whether it is the best signal
for a symbol.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal
from src.strategies.regime import MarketRegime, MarketRegimeClassifier


class LiquidAltTrendStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "liquid_alt_trend_v1"

    @property
    def strategy_name(self) -> str:
        return "Liquid Alt Multi-Horizon Trend v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < int(self.get_param("min_samples", 60)):
            return None

        regime = MarketRegimeClassifier().assess(features)
        if regime.regime != MarketRegime.UPTREND:
            return None

        noise = max(regime.noise_pct, 0.01)
        m1_multiple = max(0.0, features.momentum_1m / noise)
        m5_multiple = max(0.0, features.momentum_5m / noise)
        m15_multiple = max(0.0, features.return_15m_pct / noise)
        volume_score = min(1.0, max(0.0, features.relative_volume) / 1.5)
        flow_score = min(1.0, max(0.0, (features.bid_ask_ratio - 0.8) / 0.8))
        trend_score = min(1.0, max(0.0, features.trend_strength))
        score = min(
            1.0,
            0.25 * min(1.0, m1_multiple)
            + 0.30 * min(1.0, m5_multiple)
            + 0.20 * min(1.0, m15_multiple)
            + 0.15 * trend_score
            + 0.05 * volume_score
            + 0.05 * flow_score,
        )
        if score < float(self.get_param("min_score", 0.58)):
            return None

        # This is an intentionally conservative gross-edge estimate for the
        # existing fee-aware opportunity gate, not a claim of a future return.
        estimated_return = min(
            0.04,
            max(0.004, (noise / 100.0) * (1.2 + 1.8 * score)),
        )
        return StrategySignal(
            strategy_id=self.strategy_id,
            symbol=features.symbol,
            direction=SignalDirection.LONG,
            confidence=score,
            estimated_return=estimated_return,
            estimated_risk=noise,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=45),
            entry_logic={
                "type": "liquid_alt_trend",
                "regime": regime.regime.value,
                "momentum_1m_multiple": m1_multiple,
                "momentum_5m_multiple": m5_multiple,
                "momentum_15m_multiple": m15_multiple,
                "relative_volume": features.relative_volume,
                "book_imbalance": features.bid_ask_ratio,
            },
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.20,
                "activation_pct": 0.20,
                "no_fixed_take_profit": True,
            },
            metadata={
                "entry_price": features.last_price,
                "stop_loss_pct": 0.30,
                "regime": regime.regime.value,
                "regime_reason": regime.reason,
                "noise_pct": noise,
                "trend_strength": features.trend_strength,
                "relative_volume": features.relative_volume,
                "bid_ask_ratio": features.bid_ask_ratio,
                "trade_flow_ratio": features.trade_flow_ratio,
            },
        )
