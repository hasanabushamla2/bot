"""Tests for configuration management."""

from __future__ import annotations

import pytest

from src.core.config import ModeSettings, Settings


class TestModeSettings:
    """Verify the live trading safety gate."""

    def test_paper_mode_default(self) -> None:
        ms = ModeSettings(mode="paper", live_trading_enabled=False)
        assert ms.is_live is False

    def test_live_mode_without_enabled_flag(self) -> None:
        ms = ModeSettings(mode="live", live_trading_enabled=False)
        assert ms.is_live is False

    def test_live_mode_with_enabled_flag(self) -> None:
        ms = ModeSettings(mode="live", live_trading_enabled=True)
        assert ms.is_live is True

    def test_paper_mode_even_with_flag(self) -> None:
        ms = ModeSettings(mode="paper", live_trading_enabled=True)
        assert ms.is_live is False

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="MODE must be 'paper' or 'live'"):
            ModeSettings(mode="production")

    def test_both_conditions_required(self) -> None:
        """Live trading requires BOTH mode='live' AND live_trading_enabled=True."""
        assert ModeSettings(mode="live", live_trading_enabled=False).is_live is False
        assert ModeSettings(mode="paper", live_trading_enabled=True).is_live is False
        assert ModeSettings(mode="live", live_trading_enabled=True).is_live is True


class TestRiskSettings:
    """Verify risk configuration defaults are safe."""

    def test_defaults_are_conservative(self) -> None:
        settings = Settings()
        assert settings.risk.max_position_size_usd == 1000.0
        assert settings.risk.max_total_exposure_usd == 10000.0
        assert settings.risk.max_leverage == 1.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RISK_MAX_POSITION_SIZE_USD", "500.0")
        settings = Settings()
        assert settings.risk.max_position_size_usd == 500.0
