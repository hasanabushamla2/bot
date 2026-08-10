"""Correlation-Aware Allocation — portfolio correlation matrix and diversification scoring.

Do NOT treat BTC, ETH, SOL, and other highly correlated crypto assets as
fully independent diversification. Measure and estimate pairwise correlations
and reduce allocations when portfolio exposures are strongly correlated.

The correlation matrix is built from rolling price returns and updated
periodically. It feeds into the capital allocator for correlation penalties.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from src.core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class CorrelationMatrix:
    """Pairwise correlation matrix for all tracked instruments."""

    symbols: list[str] = field(default_factory=list)
    matrix: np.ndarray = field(default_factory=lambda: np.array([[]]))
    lookback_days: int = 30
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sample_count: int = 0

    def get_correlation(self, sym_a: str, sym_b: str) -> float:
        """Get correlation between two symbols. Returns 0.0 if unknown."""
        if sym_a == sym_b:
            return 1.0
        try:
            i = self.symbols.index(sym_a)
            j = self.symbols.index(sym_b)
            if i < self.matrix.shape[0] and j < self.matrix.shape[1]:
                val = float(self.matrix[i, j])
                return val if not np.isnan(val) else 0.0
        except (ValueError, IndexError):
            pass
        return 0.0

    def max_correlation_with_set(self, symbol: str, existing_symbols: set[str]) -> float:
        """Maximum absolute correlation between `symbol` and any in `existing_symbols`."""
        if not existing_symbols:
            return 0.0
        max_corr = 0.0
        for existing in existing_symbols:
            corr = abs(self.get_correlation(symbol, existing))
            if corr > max_corr:
                max_corr = corr
        return max_corr

    def to_dict(self) -> dict[str, Any]:
        """Serialize for logging/storage."""
        return {
            "symbols": self.symbols,
            "lookback_days": self.lookback_days,
            "sample_count": self.sample_count,
            "updated_at": self.updated_at.isoformat(),
            "matrix_shape": list(self.matrix.shape) if self.matrix.size > 0 else [0, 0],
        }


class CorrelationTracker:
    """Builds and maintains a correlation matrix from price return data.

    The tracker ingests price updates and periodically recomputes
    pairwise Pearson correlations on log returns.
    """

    def __init__(
        self,
        lookback_days: int = 30,
        update_interval_minutes: int = 60,
        min_samples: int = 20,
        max_correlation_threshold: float = 0.7,
    ) -> None:
        self.lookback_days = lookback_days
        self.update_interval_minutes = update_interval_minutes
        self.min_samples = min_samples
        self.max_correlation_threshold = max_correlation_threshold

        # Price history: symbol → list of (timestamp, price)
        self._price_history: dict[str, list[tuple[datetime, float]]] = {}
        self._last_update: datetime | None = None
        self._matrix = CorrelationMatrix()

    def record_price(self, symbol: str, price: float, timestamp: datetime | None = None) -> None:
        """Record a price observation for a symbol."""
        ts = timestamp or datetime.now(UTC)
        if symbol not in self._price_history:
            self._price_history[symbol] = []
        self._price_history[symbol].append((ts, price))

        # Prune old entries
        cutoff = ts.timestamp() - self.lookback_days * 86400
        self._price_history[symbol] = [
            (t, p) for t, p in self._price_history[symbol] if t.timestamp() >= cutoff
        ]

    def should_update(self) -> bool:
        """Check if enough time has passed to recompute the matrix."""
        if self._last_update is None:
            return True
        elapsed = (datetime.now(UTC) - self._last_update).total_seconds()
        return elapsed >= self.update_interval_minutes * 60

    def recompute(self) -> CorrelationMatrix:
        """Recompute the correlation matrix from price history.

        Uses log returns: r_t = ln(p_t / p_{t-1}).
        Requires at least `min_samples` observations per symbol.
        """
        symbols = list(self._price_history.keys())

        if len(symbols) < 2:
            matrix_arr = np.array([[1.0]]) if symbols else np.array([[]])
            self._matrix = CorrelationMatrix(
                symbols=symbols,
                matrix=matrix_arr,
                lookback_days=self.lookback_days,
            )
            self._last_update = datetime.now(UTC)
            return self._matrix

        # Build return series
        returns: dict[str, np.ndarray] = {}
        for sym in symbols:
            prices = [p for _, p in self._price_history[sym]]
            if len(prices) < self.min_samples + 1:
                continue
            log_returns = np.diff(np.log(np.array(prices)))
            returns[sym] = log_returns

        active_symbols = list(returns.keys())
        if len(active_symbols) < 2:
            matrix_arr = np.eye(len(active_symbols)) if active_symbols else np.array([[]])
            self._matrix = CorrelationMatrix(
                symbols=active_symbols,
                matrix=matrix_arr,
                lookback_days=self.lookback_days,
                sample_count=len(active_symbols),
            )
            self._last_update = datetime.now(UTC)
            return self._matrix

        # Align series lengths (use minimum length)
        min_len = min(len(r) for r in returns.values())
        aligned = np.column_stack([returns[s][-min_len:] for s in active_symbols])

        # Compute correlation
        with np.errstate(invalid="ignore"):
            corr = np.corrcoef(aligned.T)
            corr = np.nan_to_num(corr, nan=0.0)

        self._matrix = CorrelationMatrix(
            symbols=active_symbols,
            matrix=corr,
            lookback_days=self.lookback_days,
            sample_count=min_len,
        )
        self._last_update = datetime.now(UTC)

        logger.debug(
            "correlation_matrix_updated",
            symbols=active_symbols,
            samples=min_len,
            mean_correlation=float(np.mean(np.abs(corr[np.triu_indices_from(corr, k=1)])))
            if len(active_symbols) > 1
            else 0.0,
        )

        return self._matrix

    def get_matrix(self) -> CorrelationMatrix:
        """Get current correlation matrix (recomputes if needed)."""
        if self.should_update():
            self.recompute()
        return self._matrix

    def diversification_score(self, current_symbols: set[str], new_symbol: str) -> float:
        """Score how much diversification `new_symbol` adds to existing set.

        Returns 1.0 for perfectly uncorrelated (good diversification),
        0.0 for perfectly correlated (no diversification benefit).
        """
        matrix = self.get_matrix()
        if not current_symbols:
            return 1.0
        max_corr = matrix.max_correlation_with_set(new_symbol, current_symbols)
        # Transform: 0 correlation → 1.0 score, 1.0 correlation → 0.0 score
        return max(0.0, 1.0 - max_corr)

    def correlation_penalty(self, symbol: str, existing_positions: set[str]) -> float:
        """Compute allocation penalty for correlated positions.

        Higher penalty means more correlated → less capital should be allocated.
        Returns 0.0 (no penalty) to 1.0 (maximum penalty).
        """
        if not existing_positions:
            return 0.0
        matrix = self.get_matrix()
        max_corr = matrix.max_correlation_with_set(symbol, existing_positions)
        if max_corr <= self.max_correlation_threshold:
            return 0.0
        # Linear penalty above threshold
        penalty = (max_corr - self.max_correlation_threshold) / (
            1.0 - self.max_correlation_threshold
        )
        return min(1.0, max(0.0, penalty))
