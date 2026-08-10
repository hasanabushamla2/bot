"""N-04/F-06: Mode must be part of arming. MODE=micro_live required."""

from __future__ import annotations

from dataclasses import dataclass

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


class MicroLiveSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MICRO_LIVE_", extra="ignore")
    enabled: bool = False
    acknowledged: bool = False
    dry_run: bool = True
    mode: str = "paper"

    @property
    def is_fully_armed(self) -> bool:
        """N-04: mode must be 'micro_live' plus all 3 gates."""
        return self.mode == "micro_live" and self.enabled and self.acknowledged and not self.dry_run

    @property
    def can_place_real_orders(self) -> bool:
        return self.is_fully_armed

    @property
    def is_dry_run(self) -> bool:
        return self.enabled and self.dry_run

    def gate_status(self) -> dict:
        return {
            "MODE": self.mode,
            "MICRO_LIVE_ENABLED": self.enabled,
            "MICRO_LIVE_ACKNOWLEDGED": self.acknowledged,
            "MICRO_LIVE_DRY_RUN": self.dry_run,
            "REAL_ORDER_ALLOWED": self.can_place_real_orders,
        }
