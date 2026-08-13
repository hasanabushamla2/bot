"""Cost-aware range mean-reversion strategy for long-only spot markets.

Mean reversion is intentionally enabled only in a confirmed range and with
book/flow confirmation.  It is not a high-frequency scalp: the common fee-aware
opportunity gate still requires enough gross edge to pay a complete round trip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.features.engine import InstrumentFeatures
from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal
from src.strategies.regime import MarketRegime, MarketRegimeClassifier


class RangeMeanReversionStrategy(BaseStrategy):
    @property
    def strategy_id(self) -> str:
        return "range_mean_reversion_v1"

    @property
    def strategy_name(self) -> str:
        return "Liquid Range Mean Reversion v1"

    async def analyze(  # type: ignore[override]
        self, features: InstrumentFeatures | None = None, **kwargs: object
    ) -> StrategySignal | None:
        if features is None or features.sample_count < int(self.get_param("min_samples", 80)):
            return None

        regime = MarketRegimeClassifier().assess(features)
        if regime.regime != MarketRegime.RANGE:
            return None

        noise = max(regime.noise_pct, 0.01)
        downside_extension = -features.vwap_deviation_pct / noise
        if downside_extension < float(self.get_param("min_vwap_extension_multiple", 0.75)):
            return None
        if features.breakout_position_pct > float(self.get_param("max_range_position", 40.0)):
            return None
        if features.momentum_1m >= 0.0:
            return None
        if features.bid_ask_ratio <= 1.0 or features.trade_flow_ratio <= 1.0:
            return None

        extension_score = min(1.0, downside_extension / 2.0)
        book_score = min(1.0, (features.bid_ask_ratio - 1.0) / 0.5)
        flow_score = min(1.0, (features.trade_flow_ratio - 1.0) / 1.0)
        range_score = min(1.0, max(0.0, (50.0 - features.breakout_position_pct) / 30.0))
        score = min(1.0, 0.40 * extension_score + 0.25 * book_score + 0.20 * flow_score + 0.15 * range_score)
        if score < float(self.get_param("min_score", 0.68)):
            return None

        # Require a larger projected move than a trend continuation because
        # range reversion is more sensitive to churn and spread costs.
        estimated_return = min(
            0.025,
            max(0.005, (noise / 100.0) * (1.5 + 1.5 * score)),
        )
        return StrategySignal(
            strategy_id=self.strategy_id,
            symbol=features.symbol,
            direction=SignalDirection.LONG,
            confidence=score,
            estimated_return=estimated_return,
            estimated_risk=noise,
            timestamp=datetime.now(UTC),
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            entry_logic={
                "type": "range_mean_reversion",
                "regime": regime.regime.value,
                "vwap_extension_multiple": downside_extension,
                "range_position": features.breakout_position_pct,
                "bid_ask_ratio": features.bid_ask_ratio,
                "trade_flow_ratio": features.trade_flow_ratio,
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
                "vwap_extension_multiple": downside_extension,
            },
        )
