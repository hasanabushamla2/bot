"""Tests for the strategy base class and signal validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.strategies.base import BaseStrategy, SignalDirection, StrategySignal
from src.strategies.registry import StrategyRegistry


class TestStrategySignal:
    """Signal data validation."""

    def test_valid_signal(self) -> None:
        signal = StrategySignal(
            strategy_id="test",
            direction=SignalDirection.LONG,
            confidence=0.8,
            estimated_return=1.5,
        )
        assert signal.strategy_id == "test"
        assert signal.confidence == 0.8

    def test_confidence_range_enforced(self) -> None:
        with pytest.raises(ValueError, match="Confidence must be between"):
            StrategySignal(strategy_id="test", confidence=1.5)

        with pytest.raises(ValueError, match="Confidence must be between"):
            StrategySignal(strategy_id="test", confidence=-0.1)

    def test_signal_not_expired_when_no_expiry(self) -> None:
        signal = StrategySignal(strategy_id="test")
        assert not signal.is_expired

    def test_signal_expired_after_expiry_time(self) -> None:
        past = datetime.now(UTC) - timedelta(seconds=10)
        signal = StrategySignal(strategy_id="test", signal_expires_at=past)
        assert signal.is_expired

    def test_signal_not_expired_before_expiry(self) -> None:
        future = datetime.now(UTC) + timedelta(seconds=30)
        signal = StrategySignal(strategy_id="test", signal_expires_at=future)
        assert not signal.is_expired


class TestStrategyRegistry:
    """Strategy registration and lookup."""

    def test_register_and_retrieve(self) -> None:
        registry = StrategyRegistry()

        class TestStrat(BaseStrategy):
            @property
            def strategy_id(self) -> str:
                return "test_strat"

            @property
            def strategy_name(self) -> str:
                return "Test Strategy"

            async def analyze(self, **kwargs) -> StrategySignal | None:
                return None

        strat = TestStrat()
        registry.register(strat)
        assert registry.get("test_strat") is strat
        assert registry.count == 1

    def test_get_enabled_only(self) -> None:
        registry = StrategyRegistry()

        class EnabledStrat(BaseStrategy):
            strategy_id = "enabled"  # type: ignore[assignment]
            strategy_name = "Enabled"

            async def analyze(self, **kwargs) -> StrategySignal | None:
                return None

        class DisabledStrat(BaseStrategy):
            strategy_id = "disabled"  # type: ignore[assignment]
            strategy_name = "Disabled"

            def __init__(self) -> None:
                super().__init__({"enabled": False})

            async def analyze(self, **kwargs) -> StrategySignal | None:
                return None

        enabled = EnabledStrat()
        disabled = DisabledStrat()
        registry.register(enabled)
        registry.register(disabled)

        assert registry.enabled_count == 1
        assert registry.get_enabled() == [enabled]

    def test_describe_hides_secrets(self) -> None:
        class ConfigStrat(BaseStrategy):
            strategy_id = "config_test"  # type: ignore[assignment]
            strategy_name = "Config Test"

            async def analyze(self, **kwargs) -> StrategySignal | None:
                return None

        strat = ConfigStrat({"api_key": "secret123", "public_param": "visible"})
        desc = strat.describe()
        assert "api_key" not in desc["config"]
        assert "public_param" in desc["config"]
