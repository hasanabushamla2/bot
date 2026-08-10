"""Strategy registry — discovers and manages strategy plugins.

Strategies are registered here and the engine iterates over them.
Adding a new strategy means creating a new module and registering it.
"""

from __future__ import annotations

from typing import Any

from src.core.logging_config import get_logger
from src.strategies.base import BaseStrategy

logger = get_logger(__name__)


class StrategyRegistry:
    """Holds all loaded strategy instances and provides lookup.

    Strategies are loaded once at startup. The registry is thread-safe
    for reads (strategies are not hot-reloaded at runtime in v0.1).
    """

    def __init__(self) -> None:
        self._strategies: dict[str, BaseStrategy] = {}

    def register(self, strategy: BaseStrategy) -> None:
        """Register a strategy instance."""
        sid = strategy.strategy_id
        if sid in self._strategies:
            logger.warning("strategy_already_registered", strategy_id=sid)
        self._strategies[sid] = strategy
        logger.info("strategy_registered", strategy_id=sid, name=strategy.strategy_name)

    def unregister(self, strategy_id: str) -> None:
        """Remove a strategy from the registry."""
        self._strategies.pop(strategy_id, None)
        logger.info("strategy_unregistered", strategy_id=strategy_id)

    def get(self, strategy_id: str) -> BaseStrategy | None:
        """Get a strategy by ID."""
        return self._strategies.get(strategy_id)

    def get_all(self) -> list[BaseStrategy]:
        """Return all registered strategies."""
        return list(self._strategies.values())

    def get_enabled(self) -> list[BaseStrategy]:
        """Return only enabled strategies."""
        return [s for s in self._strategies.values() if s.is_enabled]

    def get_ids(self) -> list[str]:
        """Return all strategy IDs."""
        return list(self._strategies.keys())

    @property
    def count(self) -> int:
        return len(self._strategies)

    @property
    def enabled_count(self) -> int:
        return len(self.get_enabled())

    async def initialize_all(self) -> None:
        """Initialize all registered strategies."""
        for strategy in self._strategies.values():
            try:
                await strategy.initialize()
            except Exception:
                logger.exception("strategy_init_failed", strategy_id=strategy.strategy_id)

    async def shutdown_all(self) -> None:
        """Shutdown all registered strategies."""
        for strategy in self._strategies.values():
            try:
                await strategy.shutdown()
            except Exception:
                logger.exception("strategy_shutdown_failed", strategy_id=strategy.strategy_id)

    def describe_all(self) -> list[dict[str, Any]]:
        """Return metadata for all strategies (dashboard)."""
        return [s.describe() for s in self._strategies.values()]


# Global singleton
_strategy_registry: StrategyRegistry | None = None


def get_strategy_registry() -> StrategyRegistry:
    """Return the global strategy registry singleton."""
    global _strategy_registry
    if _strategy_registry is None:
        _strategy_registry = StrategyRegistry()
    return _strategy_registry
