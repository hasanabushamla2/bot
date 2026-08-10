"""Historical Market Data Ingestion — foundation for backtesting data pipeline.

Supports:
- Exchange historical klines/candles via REST (Binance public endpoint)
- CSV file import
- Normalization into pandas DataFrames for backtesting
- NEVER mixes future values into current timestamps
- Outputs data in standardized format for BacktestEngine
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from src.core.logging_config import get_logger

logger = get_logger(__name__)

# Columns we require for backtesting
REQUIRED_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

# Optional columns that improve realism
OPTIONAL_COLUMNS = [
    "bid",
    "ask",
    "quote_volume",
    "trades_count",
]


class HistoricalDataLoader:
    """Loads and normalizes historical market data from multiple sources.

    All data is normalized to:
    - UTC timestamps
    - Consistent column names
    - Sorted chronologically
    - No NaN in required columns
    - No future timestamps (checked against load time)
    """

    def __init__(self, max_future_tolerance_seconds: float = 5.0) -> None:
        self.max_future_tolerance = max_future_tolerance_seconds

    # ------------------------------------------------------------------
    # CSV import
    # ------------------------------------------------------------------

    def from_csv(
        self,
        path: Path | str,
        symbol: str | None = None,
        timestamp_col: str = "timestamp",
        timestamp_unit: str = "ms",  # "ms" or "s"
    ) -> pd.DataFrame:
        """Load OHLCV data from a CSV file.

        Expected columns: timestamp, open, high, low, close, volume
        Optional: bid, ask, quote_volume, trades_count
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Historical data file not found: {path}")

        df = pd.read_csv(path)
        return self._normalize(df, symbol or path.stem, timestamp_col, timestamp_unit)

    def from_dataframe(
        self,
        df: pd.DataFrame,
        symbol: str,
        timestamp_col: str = "timestamp",
        timestamp_unit: str = "ms",
    ) -> pd.DataFrame:
        """Normalize an existing DataFrame."""
        return self._normalize(df.copy(), symbol, timestamp_col, timestamp_unit)

    def from_binance_klines(
        self,
        raw_klines: list[list[Any]],
        symbol: str = "UNKNOWN",
    ) -> pd.DataFrame:
        """Convert raw Binance kline data to normalized DataFrame.

        Binance kline format:
        [open_time, open, high, low, close, volume, close_time,
         quote_volume, trades, taker_buy_base, taker_buy_quote, ignore]
        """
        if not raw_klines:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        rows = []
        for k in raw_klines:
            rows.append(
                {
                    "timestamp": int(k[0]),
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "quote_volume": float(k[7]),
                    "trades_count": int(k[8]),
                }
            )

        df = pd.DataFrame(rows)
        return self._normalize(df, symbol, "timestamp", "ms")

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def _normalize(
        self,
        df: pd.DataFrame,
        symbol: str,
        timestamp_col: str,
        timestamp_unit: str,
    ) -> pd.DataFrame:
        """Apply all normalization steps."""
        if df.empty:
            return pd.DataFrame(columns=REQUIRED_COLUMNS)

        # --- Verify required columns ---
        missing = set(REQUIRED_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(
                f"Missing required columns in {symbol} data: {missing}. Found: {list(df.columns)}"
            )

        # --- Timestamp normalization ---
        if timestamp_col in df.columns:
            if timestamp_unit == "ms":
                df["timestamp"] = pd.to_datetime(df[timestamp_col], unit="ms", utc=True)
            else:
                df["timestamp"] = pd.to_datetime(df[timestamp_col], unit="s", utc=True)

        # --- Sort chronologically ---
        df = df.sort_values("timestamp").reset_index(drop=True)

        # --- Future-data check ---
        now = pd.Timestamp.now(tz="UTC")
        future_mask = df["timestamp"] > now + pd.Timedelta(seconds=self.max_future_tolerance)
        if future_mask.any():
            future_count = future_mask.sum()
            logger.warning(
                "historical_data_future_timestamps",
                symbol=symbol,
                count=int(future_count),
            )
            df = df[~future_mask].copy()

        # --- Drop NaN in required columns ---
        df = df.dropna(subset=REQUIRED_COLUMNS)

        # --- Numeric type coercion ---
        for col in REQUIRED_COLUMNS:
            if col != "timestamp" and col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=REQUIRED_COLUMNS)

        # --- Add symbol if not present ---
        if "symbol" not in df.columns:
            df["symbol"] = symbol

        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Data quality checks
    # ------------------------------------------------------------------

    def validate(self, df: pd.DataFrame) -> dict[str, Any]:
        """Run quality checks on a DataFrame and return a report."""
        report: dict[str, Any] = {
            "rows": len(df),
            "columns": list(df.columns),
        }

        if df.empty:
            report["valid"] = False
            report["error"] = "Empty DataFrame"
            return report

        # Date range
        report["start"] = df["timestamp"].min().isoformat()
        report["end"] = df["timestamp"].max().isoformat()

        # Gaps (assume uniform frequency based on median diff)
        if len(df) >= 2:
            diffs = df["timestamp"].diff().dropna()
            median_diff = diffs.median()
            report["median_interval"] = str(median_diff)
            # Count gaps > 3x median interval
            large_gaps = (diffs > median_diff * 3).sum()
            report["large_gaps"] = int(large_gaps)

        # Price sanity
        for col in ["open", "high", "low", "close"]:
            if col in df.columns:
                report[f"{col}_min"] = float(df[col].min())
                report[f"{col}_max"] = float(df[col].max())

        # OHLC consistency
        bad_rows = (
            (df["high"] < df["low"])
            | (df["high"] < df["open"])
            | (df["high"] < df["close"])
            | (df["low"] > df["open"])
            | (df["low"] > df["close"])
        ).sum()
        report["ohlc_inconsistent_rows"] = int(bad_rows)

        report["valid"] = bad_rows == 0 and df["timestamp"].is_monotonic_increasing
        return report
