"""Feed Health Monitor — detection of stale, unhealthy, or disconnected feeds.

Tracks every subscribed market-data stream and detects:
- No messages within expected period
- Clock drift between exchange and local time
- Sequence gaps
- Reconnection storms
- Unhealthy order books

When data becomes stale, notifies UniverseManager to suspend the asset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.core.logging_config import get_logger

logger = get_logger(__name__)


class FeedStatus(str):
    HEALTHY = "healthy"
    STALE = "stale"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


@dataclass
class FeedHealth:
    """Health status for a single data stream."""

    exchange: str
    symbol: str
    stream_type: str  # "ticker", "depth", "trades", "candles"

    status: str = FeedStatus.DISCONNECTED
    connected: bool = False

    # Timing
    last_message_at: datetime | None = None
    last_healthy_at: datetime | None = None
    stale_since: datetime | None = None
    stale_duration_seconds: float = 0.0

    # Counters
    messages_received: int = 0
    duplicates_detected: int = 0
    out_of_order_count: int = 0
    sequence_gaps: int = 0
    reconnect_count: int = 0

    # Clock
    clock_offset_ms: float = 0.0  # exchange_time - local_time
    max_observed_offset_ms: float = 0.0

    # Latency (ms)
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0
    latency_p99_ms: float = 0.0
    max_latency_ms: float = 0.0

    # Derived
    is_healthy: bool = False

    # Metadata
    last_event_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class FeedHealthMonitor:
    """Monitors all active market-data feeds.

    Checks are performed on every message receipt (passive) and
    periodically via the `check_all` method (active).
    """

    def __init__(
        self,
        stale_threshold_seconds: float = 10.0,
        max_clock_offset_ms: float = 5000.0,
        critical_stale_seconds: float = 60.0,
    ) -> None:
        self.stale_threshold_seconds = stale_threshold_seconds
        self.max_clock_offset_ms = max_clock_offset_ms
        self.critical_stale_seconds = critical_stale_seconds

        self._feeds: dict[str, FeedHealth] = {}  # key = "exchange:symbol:stream"
        self._latency_windows: dict[str, list[float]] = {}  # last 100 latency samples

    # --- Registration ---

    def register(self, exchange: str, symbol: str, stream_type: str) -> FeedHealth:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            return self._feeds[key]
        fh = FeedHealth(exchange=exchange, symbol=symbol, stream_type=stream_type)
        self._feeds[key] = fh
        self._latency_windows[key] = []
        return fh

    def get(self, exchange: str, symbol: str, stream_type: str) -> FeedHealth | None:
        return self._feeds.get(_feed_key(exchange, symbol, stream_type))

    def get_all(self) -> list[FeedHealth]:
        return list(self._feeds.values())

    def get_unhealthy(self) -> list[FeedHealth]:
        return [f for f in self._feeds.values() if not f.is_healthy]

    # --- Message receipt ---

    def record_message(
        self,
        exchange: str,
        symbol: str,
        stream_type: str,
        exchange_ts: datetime | None = None,
        local_ts: datetime | None = None,
        sequence_id: int | None = None,
        event_type: str = "",
    ) -> None:
        """Record a received message for one feed."""
        key = _feed_key(exchange, symbol, stream_type)
        fh = self._feeds.get(key)
        if fh is None:
            fh = self.register(exchange, symbol, stream_type)

        now = datetime.now(UTC)
        local_ts = local_ts or now

        fh.last_message_at = now
        fh.messages_received += 1
        fh.last_event_type = event_type

        if not fh.connected:
            fh.connected = True

        # --- Latency ---
        if exchange_ts is not None:
            latency_ms = (local_ts - exchange_ts).total_seconds() * 1000.0
            if latency_ms >= 0:
                window = self._latency_windows.setdefault(key, [])
                window.append(latency_ms)
                if len(window) > 100:
                    window = window[-100:]
                    self._latency_windows[key] = window
                if latency_ms > fh.max_latency_ms:
                    fh.max_latency_ms = latency_ms

                # Update percentiles periodically
                if fh.messages_received % 50 == 0 and len(window) >= 10:
                    sorted_w = sorted(window)
                    n = len(sorted_w)
                    fh.latency_p50_ms = sorted_w[n // 2]
                    fh.latency_p95_ms = sorted_w[int(n * 0.95) if int(n * 0.95) < n else n - 1]
                    fh.latency_p99_ms = sorted_w[int(n * 0.99)]

            # --- Clock offset ---
            fh.clock_offset_ms = (exchange_ts - now).total_seconds() * 1000.0
            if abs(fh.clock_offset_ms) > abs(fh.max_observed_offset_ms):
                fh.max_observed_offset_ms = fh.clock_offset_ms

        # --- Health assessment ---
        fh.is_healthy = self._assess(fh)

        if fh.is_healthy:
            fh.last_healthy_at = now
            if fh.status != FeedStatus.HEALTHY:
                fh.status = FeedStatus.HEALTHY
                fh.stale_since = None
                fh.stale_duration_seconds = 0.0
        elif fh.stale_since is None:
            fh.stale_since = now

        if fh.stale_since is not None:
            fh.stale_duration_seconds = (now - fh.stale_since).total_seconds()

    # --- Periodic check ---

    def check_all(self) -> list[FeedHealth]:
        """Check all feeds for staleness. Call periodically."""
        now = datetime.now(UTC)
        unhealthy = []
        for fh in self._feeds.values():
            if not self._assess(fh):
                unhealthy.append(fh)
            # Update stale duration
            if not fh.is_healthy and fh.last_healthy_at:
                fh.stale_duration_seconds = (now - fh.last_healthy_at).total_seconds()
            if fh.stale_duration_seconds > self.critical_stale_seconds:
                fh.status = FeedStatus.DISCONNECTED
        return unhealthy

    def _assess(self, fh: FeedHealth) -> bool:
        """Determine if a feed is healthy."""
        if not fh.connected:
            return False
        if fh.last_message_at is None:
            return False

        now = datetime.now(UTC)
        age = (now - fh.last_message_at).total_seconds()

        if age > self.stale_threshold_seconds:
            fh.status = FeedStatus.STALE
            return False

        if abs(fh.clock_offset_ms) > self.max_clock_offset_ms:
            fh.status = FeedStatus.DEGRADED
            return False

        return True

    # --- Record specific events ---

    def record_duplicate(self, exchange: str, symbol: str, stream_type: str) -> None:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            self._feeds[key].duplicates_detected += 1

    def record_out_of_order(self, exchange: str, symbol: str, stream_type: str) -> None:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            self._feeds[key].out_of_order_count += 1

    def record_sequence_gap(self, exchange: str, symbol: str, stream_type: str) -> None:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            self._feeds[key].sequence_gaps += 1

    def record_reconnect(self, exchange: str, symbol: str, stream_type: str) -> None:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            self._feeds[key].reconnect_count += 1
            self._feeds[key].status = FeedStatus.RECONNECTING

    def mark_connected(self, exchange: str, symbol: str, stream_type: str) -> None:
        key = _feed_key(exchange, symbol, stream_type)
        if key in self._feeds:
            self._feeds[key].connected = True
            self._feeds[key].status = FeedStatus.HEALTHY


def _feed_key(exchange: str, symbol: str, stream_type: str) -> str:
    return f"{exchange}:{symbol}:{stream_type}"
