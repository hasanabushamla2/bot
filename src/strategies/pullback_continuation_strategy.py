"""Trend-pullback continuation strategy for liquid spot altcoins.

It enters only after a modest counter-move inside an established uptrend and
requires order-flow support before trying to rejoin the trend.  This supplies a
different entry timing from momentum/breakout without relaxing stops or costs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal
from src.strategies.regime import MarketRegime, MarketRegimeClassifier


class PullbackContinuationStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "pullback_continuation_v1"

    @property
    def strategy_name(self) -> str:
        return "Liquid Alt Trend Pullback Continuation v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < int(self.get_param("min_samples", 60)):
            return None

        regime = MarketRegimeClassifier().assess(features)
        if regime.regime != MarketRegime.UPTREND:
            return None

        noise = max(regime.noise_pct, 0.01)
        pullback_multiple = -features.momentum_1m / noise
        # A pullback is useful only when it is modest.  A large negative move
        # is a possible regime break and remains for the risk/quality gates.
        if not (0.0 <= pullback_multiple <= float(self.get_param("max_pullback_multiple", 1.5))):
            return None
        if features.vwap_deviation_pct > 0.0:
            return None
        if features.momentum_5m <= 0.0 or features.return_15m_pct <= 0.0:
            return None

        continuation_multiple = features.momentum_5m / noise
        flow_score = min(1.0, max(0.0, (features.trade_flow_ratio - 0.8) / 1.2))
        book_score = min(1.0, max(0.0, (features.bid_ask_ratio - 0.9) / 0.6))
        vwap_score = min(1.0, max(0.0, -features.vwap_deviation_pct / noise))
        trend_score = min(1.0, max(0.0, features.trend_strength))
        score = min(
            1.0,
            0.30 * min(1.0, continuation_multiple)
            + 0.25 * trend_score
            + 0.20 * flow_score
            + 0.15 * book_score
            + 0.10 * vwap_score,
        )
        if score < float(self.get_param("min_score", 0.60)):
            return None

        estimated_return = min(
            0.03,
            max(0.004, (noise / 100.0) * (1.0 + 1.5 * score)),
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
                "type": "trend_pullback_continuation",
                "regime": regime.regime.value,
                "pullback_multiple": pullback_multiple,
                "continuation_multiple": continuation_multiple,
                "vwap_deviation_pct": features.vwap_deviation_pct,
                "trade_flow_ratio": features.trade_flow_ratio,
                "bid_ask_ratio": features.bid_ask_ratio,
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
                "pullback_multiple": pullback_multiple,
                "trend_strength": features.trend_strength,
            },
        )
