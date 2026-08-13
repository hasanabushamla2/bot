"""Core configuration management.

All configuration flows through this module. Secrets come from environment
variables only — never hard-coded. Configuration is layered:

1. Default values (config/default.yaml)
2. Environment variables
3. Runtime overrides (via API/dashboard, not persisted)

Live trading requires an explicit safety gate that must be tripped
both in config AND environment before any real-money order can execute.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection settings."""

    model_config = SettingsConfigDict(env_prefix="DB_", extra="ignore")

    url: str = Field(
        default="postgresql+asyncpg://bot:bot_password@localhost:5432/bot_db",
        alias="DATABASE_URL",
    )
    url_sync: str = Field(
        default="postgresql://bot:bot_password@localhost:5432/bot_db",
        alias="DATABASE_URL_SYNC",
    )
    pool_size: int = Field(default=20, ge=1, le=100)
    pool_overflow: int = Field(default=10, ge=0, le=50)
    echo: bool = Field(default=False)


class RedisSettings(BaseSettings):
    """Redis connection settings."""

    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")

    url: str = Field(default="redis://localhost:6379/0", alias="URL")


class RiskSettings(BaseSettings):
    """Configurable risk limits. Overridable per-strategy in strategy config."""

    model_config = SettingsConfigDict(env_prefix="RISK_", extra="ignore")

    max_position_size_usd: float = Field(default=1000.0, gt=0)
    max_total_exposure_usd: float = Field(default=10000.0, gt=0)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    default_stop_loss_pct: float = Field(default=0.3, gt=0, le=100)
    max_leverage: float = Field(default=1.0, ge=1.0, le=10.0)
    max_positions_per_strategy: int = Field(default=10, ge=1)
    max_correlated_exposure_pct: float = Field(default=25.0, gt=0, le=100)
    circuit_breaker_drawdown_pct: float = Field(default=15.0, gt=0, le=100)
    circuit_breaker_consecutive_losses: int = Field(default=5, ge=1)


class PaperTradingSettings(BaseSettings):
    """Execution/risk protections used by the paper-trading lifecycle.

    Percentage-like edge fields use decimal fractions unless their name ends
    in ``_pct``.  For example, 0.001 means 0.10% expected edge.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    loss_cooldown_seconds: float = Field(
        default=300.0, ge=0.0, validation_alias="LOSS_COOLDOWN_SECONDS"
    )
    win_cooldown_seconds: float = Field(
        default=30.0, ge=0.0, validation_alias="WIN_COOLDOWN_SECONDS"
    )
    trail_activation_pct: float = Field(
        default=0.20, ge=0.0, le=100.0, validation_alias="TRAIL_ACTIVATION_PCT"
    )
    trail_distance_pct: float = Field(
        default=0.20, gt=0.0, le=100.0, validation_alias="TRAIL_DISTANCE_PCT"
    )
    trail_volatility_multiplier: float = Field(
        default=1.5, ge=0.0, le=10.0, validation_alias="TRAIL_VOLATILITY_MULTIPLIER"
    )
    trail_spread_multiplier: float = Field(
        default=2.0, ge=0.0, le=10.0, validation_alias="TRAIL_SPREAD_MULTIPLIER"
    )
    trail_activation_volatility_multiplier: float = Field(
        default=1.25,
        ge=0.0,
        le=10.0,
        validation_alias="TRAIL_ACTIVATION_VOLATILITY_MULTIPLIER",
    )
    max_trail_distance_pct: float = Field(
        default=1.25, gt=0.0, le=100.0, validation_alias="MAX_TRAIL_DISTANCE_PCT"
    )
    material_reentry_confidence_improvement: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        validation_alias="MATERIAL_REENTRY_CONFIDENCE_IMPROVEMENT",
    )
    min_reentry_market_structure_score: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        validation_alias="MIN_REENTRY_MARKET_STRUCTURE_SCORE",
    )
    reentry_reference_volatility_pct: float = Field(
        default=0.30,
        gt=0.0,
        le=100.0,
        validation_alias="REENTRY_REFERENCE_VOLATILITY_PCT",
    )
    max_consecutive_losses_per_symbol: int = Field(
        default=2, ge=1, validation_alias="MAX_CONSECUTIVE_LOSSES_PER_SYMBOL"
    )
    symbol_lockout_seconds: float = Field(
        default=1800.0, ge=0.0, validation_alias="SYMBOL_LOCKOUT_SECONDS"
    )
    symbol_loss_streak_reset_seconds: float = Field(
        default=21600.0, gt=0.0, validation_alias="SYMBOL_LOSS_STREAK_RESET_SECONDS"
    )
    min_expected_edge_over_cost: float = Field(
        default=0.001, ge=0.0, le=1.0, validation_alias="MIN_EXPECTED_EDGE_OVER_COST"
    )
    # Bounded sizing enhancement.  It is evaluated only after opportunity,
    # risk, liquidity, and expected-net-edge gates; it cannot exceed the
    # existing per-position risk cap.
    high_conviction_sizing_enabled: bool = Field(
        default=True, validation_alias="HIGH_CONVICTION_SIZING_ENABLED"
    )
    high_conviction_min_confidence: float = Field(
        default=0.72,
        ge=0.0,
        le=1.0,
        validation_alias="HIGH_CONVICTION_MIN_CONFIDENCE",
    )
    high_conviction_min_quality_score: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
        validation_alias="HIGH_CONVICTION_MIN_QUALITY_SCORE",
    )
    high_conviction_min_net_edge_fraction: float = Field(
        default=0.0015,
        ge=0.0,
        le=1.0,
        validation_alias="HIGH_CONVICTION_MIN_NET_EDGE_FRACTION",
    )
    high_conviction_max_multiplier: float = Field(
        default=4.0,
        ge=1.0,
        le=4.0,
        validation_alias="HIGH_CONVICTION_MAX_MULTIPLIER",
    )

    # The paper fill model.  These remain realistic rather than being relaxed
    # for a short soak test.
    taker_fee: float = Field(default=0.001, ge=0.0, le=0.1, validation_alias="PAPER_TAKER_FEE")
    maker_fee: float = Field(default=0.001, ge=0.0, le=0.1, validation_alias="PAPER_MAKER_FEE")
    slippage_bps: float = Field(
        default=5.0, ge=0.0, le=1000.0, validation_alias="PAPER_SLIPPAGE_BPS"
    )
    simulated_latency_ms: float = Field(
        default=50.0, ge=0.0, validation_alias="PAPER_SIMULATED_LATENCY_MS"
    )


class ExchangeSettings(BaseSettings):
    """Exchange-specific configuration."""

    model_config = SettingsConfigDict(env_prefix="EXCHANGE_", extra="ignore")

    coinbase_api_key: str = Field(default="", alias="COINBASE_API_KEY")
    coinbase_api_secret: str = Field(default="", alias="COINBASE_API_SECRET")
    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
    kraken_api_key: str = Field(default="", alias="KRAKEN_API_KEY")
    kraken_api_secret: str = Field(default="", alias="KRAKEN_API_SECRET")
    bybit_api_key: str = Field(default="", alias="BYBIT_API_KEY")
    bybit_api_secret: str = Field(default="", alias="BYBIT_API_SECRET")


class ModeSettings(BaseSettings):
    """Operational mode — the safety gate for live trading."""

    model_config = SettingsConfigDict(env_prefix="MODE_", extra="ignore")

    mode: str = Field(default="paper", alias="MODE")
    live_trading_enabled: bool = Field(default=False, alias="LIVE_TRADING_ENABLED")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("paper", "live"):
            raise ValueError("MODE must be 'paper' or 'live'")
        return v

    @property
    def is_live(self) -> bool:
        """Both conditions must be true for live trading."""
        return self.mode == "live" and self.live_trading_enabled


class LoggingSettings(BaseSettings):
    """Logging configuration."""

    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore")

    level: str = Field(default="INFO", alias="LEVEL")
    format: str = Field(default="json", alias="FORMAT")  # "json" or "text"


class DashboardSettings(BaseSettings):
    """Dashboard server settings."""

    model_config = SettingsConfigDict(env_prefix="DASHBOARD_", extra="ignore")

    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8080, alias="PORT", ge=1, le=65535)


class Settings(BaseSettings):
    """Root settings aggregating all subsystem configs."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    paper: PaperTradingSettings = Field(default_factory=PaperTradingSettings)
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    mode: ModeSettings = Field(default_factory=ModeSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)

    # Alert webhooks
    alert_webhook_url: str = Field(default="")
    alert_email: str = Field(default="")

    @classmethod
    def from_yaml(cls, yaml_path: Path | None = None) -> Settings:
        """Layer YAML file config under environment variables."""
        instance = cls()
        if yaml_path and yaml_path.exists():
            with open(yaml_path) as f:
                yaml_config: dict[str, Any] = yaml.safe_load(f) or {}
            # Merge YAML values only where env var is not explicitly set
            for key, value in yaml_config.items():
                env_val = os.environ.get(key.upper())
                if env_val is None and hasattr(instance, key):
                    setattr(instance, key, value)
        return instance


# Singleton — initialized once at startup
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the global settings instance, initializing if needed."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Force re-read of settings (useful after config changes in dashboard)."""
    global _settings
    _settings = Settings()
    return _settings
