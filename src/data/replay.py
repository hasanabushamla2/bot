"""Replay Mode — deterministic replay of recorded market events."""

# mypy: ignore-errors
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class ReplayFeed:
    def __init__(self, file_path: Path | str, speed_multiplier: float = 1.0) -> None:
        self.file_path = Path(file_path)
        self.speed = speed_multiplier
        self._events: list[dict[str, Any]] = []
        self._loaded = False

    def load(self) -> int:
        if not self.file_path.exists():
            raise FileNotFoundError(f"Replay file not found: {self.file_path}")
        with open(self.file_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    self._events.append(json.loads(line))
        self._loaded = True
        return len(self._events)

    def record_ticker_event(self, event: Any, output_path: Path | str) -> None:
        data: dict[str, Any] = {
            "exchange": str(getattr(event, "exchange", "")),
            "symbol": str(getattr(event, "symbol", "")),
            "event_type": str(getattr(event, "event_type", "")),
            "bid": float(getattr(event, "bid", 0.0)),
            "ask": float(getattr(event, "ask", 0.0)),
            "last": float(getattr(event, "last", 0.0)),
            "volume_24h": float(getattr(event, "volume_24h", 0.0)),
        }
        with open(str(output_path), "a") as f:
            f.write(json.dumps(data) + "\n")

    async def replay(self, handler_func: Any) -> int:
        if not self._loaded:
            self.load()
        import asyncio

        processed = 0
        prev_real_ts: float | None = None
        for evt in self._events:
            if prev_real_ts is not None:
                delay = (time.monotonic() - prev_real_ts) / self.speed
                if delay > 0.05:
                    await asyncio.sleep(min(delay, 1.0))
            ticker = {
                "symbol": str(evt.get("symbol", "")),
                "last": float(evt.get("last", 0.0)),
                "bid": float(evt.get("bid", 0.0)),
                "ask": float(evt.get("ask", 0.0)),
                "volume": float(evt.get("volume_24h", 0.0)),
            }
            try:
                await handler_func(ticker)
            except Exception:
                logger.exception("replay_handler_error", event_index=processed)
            processed += 1
            prev_real_ts = time.monotonic()
        return processed
