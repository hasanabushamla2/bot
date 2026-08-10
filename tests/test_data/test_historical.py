"""Tests for historical data ingestion."""

from __future__ import annotations

import pandas as pd

from src.data.historical import HistoricalDataLoader


class TestHistoricalDataLoader:
    def test_from_binance_klines(self) -> None:
        loader = HistoricalDataLoader()
        raw = [
            [
                1704067200000,
                42000.0,
                43000.0,
                41500.0,
                42500.0,
                100.0,
                1704153599999,
                4200000.0,
                500,
                50.0,
                2100000.0,
                "0",
            ],
            [
                1704153600000,
                42500.0,
                42800.0,
                42200.0,
                42600.0,
                80.0,
                1704240000000,
                3400000.0,
                400,
                40.0,
                1700000.0,
                "0",
            ],
        ]
        df = loader.from_binance_klines(raw, "BTC-USDT")
        assert len(df) == 2
        assert list(df.columns) == [
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "quote_volume",
            "trades_count",
            "symbol",
        ]
        assert df["close"].iloc[0] == 42500.0

    def test_validate_clean_data(self) -> None:
        loader = HistoricalDataLoader()
        dates = pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "timestamp": dates,
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50500.0,
                "volume": 100.0,
            }
        )
        report = loader.validate(df)
        assert report["valid"] is True
        assert report["rows"] == 100

    def test_validate_ohlc_inconsistency(self) -> None:
        loader = HistoricalDataLoader()
        dates = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "timestamp": dates,
                "open": [50000.0, 50000.0, 50000.0],
                "high": [51000.0, 49000.0, 51000.0],  # second: high < open
                "low": [49000.0, 48000.0, 49000.0],
                "close": [50500.0, 48500.0, 50500.0],
                "volume": [100.0, 100.0, 100.0],
            }
        )
        report = loader.validate(df)
        assert report["ohlc_inconsistent_rows"] >= 1

    def test_future_timestamps_filtered(self) -> None:
        loader = HistoricalDataLoader(max_future_tolerance_seconds=5.0)
        now = pd.Timestamp.now(tz="UTC")
        dates = [
            now - pd.Timedelta(hours=2),
            now - pd.Timedelta(hours=1),
            now + pd.Timedelta(hours=1),  # Future — should be dropped
        ]
        df = pd.DataFrame(
            {
                "timestamp": dates,
                "open": 50000.0,
                "high": 51000.0,
                "low": 49000.0,
                "close": 50500.0,
                "volume": 100.0,
            }
        )
        normalized = loader.from_dataframe(df, "TEST")
        assert len(normalized) == 2  # Future row dropped

    def test_empty_dataframe(self) -> None:
        loader = HistoricalDataLoader()
        df = loader.from_binance_klines([], "TEST")
        assert df.empty
