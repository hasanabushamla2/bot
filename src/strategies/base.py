"""Base strategy interface.

Every strategy MUST implement this abstract base class.

A strategy:
1. Receives normalized market data.
2. Analyzes data according to its algorithm.
3. Produces a structured Signal when an opportunity is detected.

Strategies MUST NOT:
- Place orders directly (that's the Execution Engine's job).
- Bypass the Risk Engine.
- Access exchange APIs directly.
- Log API keys or secrets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from src.adapters.base import NormalizedOrderBook, NormalizedTicker, NormalizedTrade


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


@dataclass
class StrategySignal:
    """Structured signal produced by a strategy.

    This is the ONLY output a strategy may produce. The Opportunity
    Engine consumes signals and decides whether to act on them.
    """

    # Identity.  ``signal_id`` is optional for strategy plugins; the paper
    # lifecycle assigns a durable regime id when a plugin does not provide an
    # event id of its own.
    strategy_id: str
    signal_id: str = ""
    strategy_version: str = "1.0.0"

    # Market targeting
    exchange: str | None = None
    symbol: str | None = None
    instrument_id: str | None = None
    market: str | None = None  # "crypto", "forex", etc.

    # Direction
    direction: SignalDirection = SignalDirection.NEUTRAL

    # Logic (documented, auditable)
    entry_logic: dict[str, Any] = field(default_factory=dict)
    exit_logic: dict[str, Any] = field(default_factory=dict)

    # Quantitative estimates
    confidence: float = 0.0  # 0.0 to 1.0
    estimated_return: float | None = None  # Expected % return
    estimated_risk: float | None = None  # Expected % risk
    required_capital: float | None = None  # Capital needed

    # Timing
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    signal_expires_at: datetime | None = None  # Signal becomes stale after this

    # Extras
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.confidence < 0.0 or self.confidence > 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {self.confidence}")

    @property
    def is_expired(self, now: datetime | None = None) -> bool:
        """Check if the signal has expired."""
        if self.signal_expires_at is None:
            return False
        now = now or datetime.now(UTC)
        return now > self.signal_expires_at


class BaseStrategy(ABC):
    """Abstract base for all trading strategies.

    Subclass this and implement:
    - analyze(): core signal generation logic.
    - strategy_id: unique identifier.
    - strategy_name: human-readable name.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self._enabled = self.config.get("enabled", True)

    # --- Identity ---

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        """Unique strategy identifier, e.g. 'mean_reversion_v1'."""
        ...

    @property
    @abstractmethod
    def strategy_name(self) -> str:
        """Human-readable name, e.g. 'Mean Reversion v1'."""
        ...

    @property
    def strategy_version(self) -> str:
        """Semantic version of the strategy."""
        v = self.config.get("version", "1.0.0")
        return str(v)

    @property
    def is_enabled(self) -> bool:
        return bool(self._enabled)

    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    # --- Lifecycle ---

    async def initialize(self) -> None:
        """Called once when the strategy is loaded. Override for setup."""
        pass

    async def shutdown(self) -> None:
        """Called on graceful shutdown. Override for cleanup."""
        pass

    # --- Core ---

    @abstractmethod
    async def analyze(
        self,
        ticker: NormalizedTicker | None = None,
        order_book: NormalizedOrderBook | None = None,
        trades: list[NormalizedTrade] | None = None,
    ) -> StrategySignal | None:
        """Analyze market data and produce a signal if an opportunity exists.

        Args:
            ticker: Latest ticker data (may be None if not subscribed).
            order_book: Latest order book data (may be None).
            trades: Recent trades buffer (may be None).

        Returns:
            A StrategySignal if an opportunity is detected, None otherwise.
        """
        ...

    # --- Parameter Management ---

    def get_param(self, key: str, default: Any = None) -> Any:
        """Get a strategy parameter from config."""
        return self.config.get(key, default)

    def set_param(self, key: str, value: Any) -> None:
        """Update a strategy parameter at runtime."""
        self.config[key] = value

    # --- Audit ---

    def describe(self) -> dict[str, Any]:
        """Return strategy metadata for the dashboard / audit trail."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "version": self.strategy_version,
            "enabled": self.is_enabled,
            "config": {k: v for k, v in self.config.items() if k not in ("api_key", "secret")},
        }
