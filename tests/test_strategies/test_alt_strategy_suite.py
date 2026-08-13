"""Tests for the liquid-alt strategy suite and regime-aware selector."""

from __future__ import annotations

import asyncio

from src.features.engine import InstrumentFeatures
from src.strategies.base import SignalDirection, StrategySignal
from src.strategies.ensemble import StrategyEnsembleSelector
from src.strategies.liquid_alt_trend_strategy import LiquidAltTrendStrategy
from src.strategies.pullback_continuation_strategy import PullbackContinuationStrategy
from src.strategies.range_mean_reversion_strategy import RangeMeanReversionStrategy
from src.strategies.regime import MarketRegime, MarketRegimeClassifier


def _uptrend_features() -> InstrumentFeatures:
    return InstrumentFeatures(
        symbol="ALT-USDT",
        last_price=100.0,
        sample_count=100,
        momentum_1m=0.40,
        momentum_5m=0.60,
        return_15m_pct=0.80,
        atr_pct=0.20,
        volatility_5m_pct=0.20,
        relative_volume=1.5,
        bid_ask_ratio=1.30,
        trade_flow_ratio=1.30,
        trend_strength=0.80,
        spread_bps=3.0,
    )


def test_liquid_alt_trend_emits_only_in_aligned_uptrend() -> None:
    features = _uptrend_features()
    signal = asyncio.run(LiquidAltTrendStrategy().analyze(features=features))
    assert signal is not None
    assert signal.strategy_id == "liquid_alt_trend_v1"
    assert signal.direction == SignalDirection.LONG
    assert signal.confidence >= 0.58
    assert signal.estimated_return is not None and signal.estimated_return > 0.003


def test_pullback_continuation_uses_modest_pullback_inside_uptrend() -> None:
    features = _uptrend_features()
    features.momentum_1m = -0.10
    features.vwap_deviation_pct = -0.10
    features.trade_flow_ratio = 1.50
    features.bid_ask_ratio = 1.25
    signal = asyncio.run(PullbackContinuationStrategy().analyze(features=features))
    assert signal is not None
    assert signal.strategy_id == "pullback_continuation_v1"
    assert signal.entry_logic["type"] == "trend_pullback_continuation"


def test_range_mean_reversion_only_emits_in_range_with_bid_flow_confirmation() -> None:
    features = _uptrend_features()
    features.trend_strength = 0.05
    features.momentum_1m = -0.10
    features.momentum_5m = 0.05
    features.return_15m_pct = 0.0
    features.vwap_deviation_pct = -0.30
    features.breakout_position_pct = 20.0
    features.bid_ask_ratio = 1.40
    features.trade_flow_ratio = 1.50
    assert MarketRegimeClassifier().assess(features).regime == MarketRegime.RANGE
    signal = asyncio.run(RangeMeanReversionStrategy().analyze(features=features))
    assert signal is not None
    assert signal.strategy_id == "range_mean_reversion_v1"


def test_selector_keeps_one_regime_compatible_signal_per_symbol() -> None:
    features = _uptrend_features()
    trend = StrategySignal(
        strategy_id="liquid_alt_trend_v1",
        symbol="ALT-USDT",
        direction=SignalDirection.LONG,
        confidence=0.82,
        estimated_return=0.01,
    )
    breakout = StrategySignal(
        strategy_id="breakout_v1",
        symbol="ALT-USDT",
        direction=SignalDirection.LONG,
        confidence=0.70,
        estimated_return=0.01,
    )
    range_signal = StrategySignal(
        strategy_id="range_mean_reversion_v1",
        symbol="ALT-USDT",
        direction=SignalDirection.LONG,
        confidence=0.95,
        estimated_return=0.01,
    )

    selection = StrategyEnsembleSelector().select(
        [trend, breakout, range_signal], lambda _symbol: features
    )
    assert selection.selected == [trend]
    assert {item.reason for item in selection.rejected} == {
        "ensemble_regime_uptrend",
        "ensemble_symbol_competition",
    }
