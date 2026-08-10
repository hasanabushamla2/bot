"""Replay Mode — deterministic replay of recorded market events through the SAME pipeline."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.core.logging_config import get_logger
from src.data.normalization import NormalizedEvent

logger = get_logger(__name__)


class ReplayFeed:
    """Replays recorded NormalizedEvent objects from a JSONL file."""

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
        logger.info("replay_loaded", file=str(self.file_path), events=len(self._events))
        return len(self._events)

    def record_event(self, event: NormalizedEvent, output_path: Path | str) -> None:
        """Record one normalized event to a JSONL file."""
        data = {
            "exchange": event.exchange,
            "symbol": event.symbol,
            "event_type": event.event_type.value,
            "exchange_timestamp": event.exchange_timestamp.isoformat(),
            "local_receive_timestamp": event.local_receive_timestamp.isoformat(),
            "sequence_id": event.sequence_id,
        }
        # Add type-specific fields
        if hasattr(event, "bid"):
            data["bid"] = event.bid
            data["ask"] = event.ask  # type: ignore[attr-defined]
            data["last"] = event.last  # type: ignore[attr-defined]
            data["volume_24h"] = getattr(event, "volume_24h", 0)
        if hasattr(event, "price"):
            data["price"] = event.price
            data["quantity"] = event.quantity  # type: ignore[attr-defined]
        if hasattr(event, "bids"):
            data["best_bid"] = event.bids  # type: ignore[attr-defined][0].price if event.bids  # type: ignore[attr-defined] else 0
            data["best_ask"] = event.ask  # type: ignore[attr-defined]s[0].price if event.ask  # type: ignore[attr-defined]s else 0
        with open(output_path, "a") as f:
            f.write(json.dumps(data) + "\n")

    async def replay(self, handler_func: Any) -> int:
        """Replay all events through handler_func(ticker_dict) in order with timing."""
        if not self._loaded:
            self.load()
        processed = 0
        prev_real_ts: float | None = None
        for evt in self._events:
            if prev_real_ts is not None:
                delay = (time.monotonic() - prev_real_ts) / self.speed
                if delay < 0.05:
                    pass  # Don't sleep for tiny gaps
                else:
                    await __import__("asyncio").sleep(min(delay, 1.0))
            # Build ticker-like dict for handler
            ticker = {
                "symbol": evt.get("symbol", ""),
                "last": evt.get("last", evt.get("price", 0)),
                "bid": evt.get("bid", evt.get("best_bid", 0)),
                "ask": evt.get("ask", evt.get("best_ask", 0)),
                "volume": evt.get("volume_24h", 0),
                "quantity": evt.get("quantity", 0),
            }
            try:
                await handler_func(ticker)
            except Exception:
                logger.exception("replay_handler_error", event_index=processed)
            processed += 1
            prev_real_ts = time.monotonic()
        return processed
