"""Position Monitor — N-duplicate-exit fix: exactly ONE exit intent per trigger.
Persists highest_price, trail_level, exit_intent_pending for restart."""

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
            or TrailConfig(
                trail_pct=0.20,
                activation_pct=0.20,
                trailing_delta=0.002,
                enable_fixed_take_profit=False,
            )
        )
        self.hard_stop_pct = hard_stop_pct
        self._trail_states: dict[str, TrailState] = {}
        self._exit_intents: set[str] = set()  # N: one exit intent per symbol

    def register_position(self, pos: PaperPosition) -> None:
        direction = TrailDirection.LONG if pos.direction == "long" else TrailDirection.SHORT
        ts = self.trail_manager.initialize(pos.symbol, direction, pos.entry_price, pos.entry_time)
        self._trail_states[pos.symbol] = ts
        self._exit_intents.discard(pos.symbol)

    def check_position(self, pos: PaperPosition) -> dict[str, Any] | None:
        """N: Only ONE exit intent. Once triggered, won't trigger again."""
        price = pos.current_price
        if price <= 0:
            return None
        sym = pos.symbol
        if sym in self._exit_intents:
            return None  # Already triggered
        # Hard stop
        if pos.stop_loss_price > 0 and pos.direction == "long" and price <= pos.stop_loss_price:
            self._exit_intents.add(sym)
            return {
                "symbol": sym,
                "reason": "hard_stop",
                "price": price,
                "trail_peak": pos.trail_peak,
                "trail_level": 0.0,
            }
        # Trailing
        ts = self._trail_states.get(sym)
        if ts is None:
            direction = TrailDirection.LONG if pos.direction == "long" else TrailDirection.SHORT
            ts = self.trail_manager.initialize(sym, direction, pos.entry_price, pos.entry_time)
            self._trail_states[sym] = ts
        self.trail_manager.update(ts, price)
        pos.trail_peak = ts.peak_price
        pos.trail_activated = ts.activated
        pos.trail_level = ts.trail_level
        if self.trail_manager.should_exit(ts):
            self._exit_intents.add(sym)
            return {
                "symbol": sym,
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
        self._exit_intents.discard(symbol)

    # N-10: Persistence support
    def get_trail_state(self, symbol: str) -> dict[str, Any] | None:
        ts = self._trail_states.get(symbol)
        if ts is None:
            return None
        return {
            "symbol": ts.symbol,
            "direction": ts.direction.value,
            "entry_price": ts.entry_price,
            "peak_price": ts.peak_price,
            "trail_level": ts.trail_level,
            "activated": ts.activated,
            "exit_intent": symbol in self._exit_intents,
        }

    def restore_trail_state(self, saved: dict[str, Any]) -> None:
        sym = saved["symbol"]
        direction = TrailDirection.LONG if saved["direction"] == "long" else TrailDirection.SHORT
        ts = self.trail_manager.initialize(sym, direction, saved["entry_price"])
        ts.peak_price = saved["peak_price"]
        ts.trail_level = saved["trail_level"]
        ts.activated = saved["activated"]
        self._trail_states[sym] = ts
        if saved.get("exit_intent"):
            self._exit_intents.add(sym)
