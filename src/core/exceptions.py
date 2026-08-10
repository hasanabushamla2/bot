"""Core domain exceptions.

Every exception in the system inherits from QuantEngineError so
error handling can identify internal errors vs external ones.
"""

from __future__ import annotations


class QuantEngineError(Exception):
    """Base exception for all engine errors."""


# --- Data Errors ---
class DataError(QuantEngineError):
    """Base for data-related errors."""


class ConnectionError(DataError):
    """WebSocket or REST connection failure."""


class StaleDataError(DataError):
    """Market data is too old to act on."""


class DuplicateEventError(DataError):
    """Duplicate event received (idempotency check)."""


class OutOfOrderEventError(DataError):
    """Event timestamp is older than last processed."""


class NormalizationError(DataError):
    """Failed to normalize exchange data to internal format."""


# --- Strategy Errors ---
class StrategyError(QuantEngineError):
    """Base for strategy errors."""


class InvalidSignalError(StrategyError):
    """A strategy produced a malformed or invalid signal."""


# --- Risk Errors ---
class RiskError(QuantEngineError):
    """Base for risk management errors."""


class RiskLimitExceededError(RiskError):
    """A trade would exceed a risk limit."""


class CircuitBreakerError(RiskError):
    """Circuit breaker has tripped — no new positions allowed."""


class KillSwitchError(RiskError):
    """Emergency kill switch is active."""


# --- Execution Errors ---
class ExecutionError(QuantEngineError):
    """Base for execution errors."""


class OrderRejectedError(ExecutionError):
    """Order was rejected by the exchange."""


class OrderTimeoutError(ExecutionError):
    """Order did not receive acknowledgement in time."""


class InsufficientBalanceError(ExecutionError):
    """Not enough balance to place the order."""


class DuplicateOrderError(ExecutionError):
    """An identical order was already placed (idempotency)."""


class ReconciliationError(ExecutionError):
    """Exchange state does not match local state after reconciliation."""


class RateLimitError(ExecutionError):
    """Exchange rate limit reached."""


# --- Configuration Errors ---
class ConfigError(QuantEngineError):
    """Invalid configuration."""


class LiveTradingNotEnabledError(ConfigError):
    """Attempted live trade without the safety gate being tripped."""


# --- Backtesting Errors ---
class BacktestError(QuantEngineError):
    """Base for backtesting errors."""


class LookAheadBiasError(BacktestError):
    """Potential look-ahead bias detected in backtesting data."""


class InsufficientDataError(BacktestError):
    """Not enough data to run a valid backtest."""
