"""Position Monitor — real-time hard stop + trailing stop monitoring for paper positions."""

from __future__ import annotations

from typing import Any

from src.core.logging_config import get_logger
from src.paper.account import PaperAccount, PaperPosition
from src.strategies.trailing_stop import (
    TrailConfig,
    TrailDirection,
    TrailingStopManager,
    TrailState,
)

logger = get_logger(__name__)


class PositionMonitor:
    def __init__(
        self,
        account: PaperAccount,
        trail_config: TrailConfig | None = None,
        hard_stop_pct: float = 0.30,
    ) -> None:
        self.account = account
        self.trail_manager = TrailingStopManager(
            trail_config
            or TrailConfig(trail_pct=0.20, activation_pct=0.20, enable_fixed_take_profit=False)
        )
        self.hard_stop_pct = hard_stop_pct
        self._trail_states: dict[str, TrailState] = {}
        self._exit_requests: list[dict[str, Any]] = []

    def register_position(self, pos: PaperPosition) -> None:
        direction = TrailDirection.LONG if pos.direction == "long" else TrailDirection.SHORT
        ts = self.trail_manager.initialize(pos.symbol, direction, pos.entry_price, pos.entry_time)
        self._trail_states[pos.symbol] = ts

    def check_position(self, pos: PaperPosition) -> dict[str, Any] | None:
        """Check one position for hard stop or trailing stop. Returns exit dict or None."""
        price = pos.current_price
        if price <= 0:
            return None
        # Hard stop
        if pos.stop_loss_price > 0 and pos.direction == "long" and price <= pos.stop_loss_price:
            return {
                "symbol": pos.symbol,
                "reason": "hard_stop",
                "price": price,
                "trail_peak": pos.trail_peak,
                "trail_level": 0.0,
            }
        if pos.stop_loss_price > 0 and pos.direction == "short" and price >= pos.stop_loss_price:
            return {
                "symbol": pos.symbol,
                "reason": "hard_stop",
                "price": price,
                "trail_peak": pos.trail_peak,
                "trail_level": 0.0,
            }
        # Trailing stop
        ts = self._trail_states.get(pos.symbol)
        if ts is None:
            direction = TrailDirection.LONG if pos.direction == "long" else TrailDirection.SHORT
            ts = self.trail_manager.initialize(
                pos.symbol, direction, pos.entry_price, pos.entry_time
            )
            self._trail_states[pos.symbol] = ts
        self.trail_manager.update(ts, price)
        pos.trail_peak = ts.peak_price
        pos.trail_activated = ts.activated
        pos.trail_level = ts.trail_level
        if self.trail_manager.should_exit(ts):
            return {
                "symbol": pos.symbol,
                "reason": "trail_hit",
                "price": price,
                "trail_peak": ts.peak_price,
                "trail_level": ts.trail_level,
            }
        return None

    def check_all(self) -> list[dict[str, Any]]:
        exits: list[dict[str, Any]] = []
        for _sym, pos in list(self.account.state.open_positions.items()):
            exit_req = self.check_position(pos)
            if exit_req:
                exits.append(exit_req)
        return exits

    def unregister_position(self, symbol: str) -> None:
        self._trail_states.pop(symbol, None)
