"""Micro-Live Configuration — safety-gated mode for real-money validation.

THREE safety gates must ALL be tripped before any real order:
  MODE=micro_live
  MICRO_LIVE_ENABLED=true
  MICRO_LIVE_ACKNOWLEDGED=true

MAXIMUM CAPITAL: $50 USD. SPOT ONLY. No withdrawals.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass
class MicroLivePolicy:
    capital_cap_usd: float = 50.0
    default_slot_size_usd: float = 5.0
    max_slots: int = 10
    min_slot_size_usd: float = 1.0
    spot_only: bool = True
    allow_leverage: bool = False
    allow_margin: bool = False
    allow_futures: bool = False
    allow_shorts: bool = False
    max_daily_validation_loss_usd: float = 10.0
    stop_slippage_warn_pct: float = 0.20
    stop_slippage_critical_pct: float = 0.50
    latency_warn_ms: float = 500.0
    latency_critical_ms: float = 2000.0
    max_consecutive_rejections: int = 5
    balance_mismatch_action: str = "halt"

class MicroLiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MICRO_LIVE_", extra="ignore")
    enabled: bool = Field(default=False, alias="ENABLED")
    acknowledged: bool = Field(default=False, alias="ACKNOWLEDGED")
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    mode: str = Field(default="micro_live", alias="MODE")

    @property
    def is_fully_armed(self) -> bool:
        return (
            self.enabled
            and self.acknowledged
            and not self.dry_run
        )

    @property
    def can_place_real_orders(self) -> bool:
        return self.is_fully_armed

    @property
    def is_dry_run(self) -> bool:
        return self.enabled and self.dry_run
