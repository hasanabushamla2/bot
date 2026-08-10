"""Data Engine Metrics — latency, throughput, and health metrics.

Tracks:
- Exchange-event → local-receipt latency
- Local-receipt → normalized-event latency
- Events per second
- Drops / gaps
- Reconnection counts
- Queue utilization

Exposed to analytics and dashboard.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class DataEngineMetrics:
    """Aggregated metrics for the entire data engine."""

    # Event counters
    total_events_received: int = 0
    total_events_normalized: int = 0
    total_events_dropped: int = 0
    events_per_second: float = 0.0

    # WebSocket
    ws_connections: int = 0
    ws_reconnects: int = 0
    ws_messages_received: int = 0

    # REST
    rest_requests: int = 0
    rest_429s: int = 0
    rest_errors: int = 0

    # Latency (ms)
    latency_exchange_to_local_p50_ms: float = 0.0
    latency_exchange_to_local_p95_ms: float = 0.0
    latency_exchange_to_local_p99_ms: float = 0.0
    latency_exchange_to_local_max_ms: float = 0.0

    # Order book
    books_maintained: int = 0
    books_healthy: int = 0
    books_out_of_sync: int = 0
    book_resyncs: int = 0

    # Rate limiting
    rate_limit_utilization_pct: float = 0.0  # Global avg

    # Clock
    clock_offset_max_ms: float = 0.0

    # Queue
    event_bus_queue_utilization_pct: float = 0.0

    # Feeds
    feeds_total: int = 0
    feeds_healthy: int = 0
    feeds_stale: int = 0
    feeds_disconnected: int = 0


class MetricsCollector:
    """Collects and computes data-engine metrics.

    Uses rolling windows for rate/latency computations.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds

        # Latency samples
        self._latency_samples: deque[float] = deque(maxlen=10000)
        self._event_timestamps: deque[float] = deque()  # For EPS calculation

        # Counters
        self.total_events_received = 0
        self.total_events_normalized = 0
        self.total_events_dropped = 0
        self.ws_connections = 0
        self.ws_reconnects = 0
        self.ws_messages = 0
        self.rest_requests = 0
        self.rest_429s = 0
        self.rest_errors = 0
        self.books_maintained = 0
        self.books_healthy = 0
        self.books_out_of_sync = 0
        self.book_resyncs = 0
        self.clock_offset_max = 0.0

    def record_event(self, latency_ms: float | None = None) -> None:
        """Record a received event."""
        self.total_events_received += 1
        now = time.monotonic()
        self._event_timestamps.append(now)

        # Prune old timestamps
        cutoff = now - self.window_seconds
        while self._event_timestamps and self._event_timestamps[0] < cutoff:
            self._event_timestamps.popleft()

        if latency_ms is not None and latency_ms >= 0:
            self._latency_samples.append(latency_ms)

    def record_drop(self) -> None:
        self.total_events_dropped += 1

    def record_normalized(self) -> None:
        self.total_events_normalized += 1

    def record_ws_connect(self) -> None:
        self.ws_connections += 1

    def record_ws_reconnect(self) -> None:
        self.ws_reconnects += 1

    def record_ws_message(self) -> None:
        self.ws_messages += 1

    def record_rest_request(self) -> None:
        self.rest_requests += 1

    def record_rest_429(self) -> None:
        self.rest_429s += 1

    def record_rest_error(self) -> None:
        self.rest_errors += 1

    def record_clock_offset(self, offset_ms: float) -> None:
        if abs(offset_ms) > abs(self.clock_offset_max):
            self.clock_offset_max = offset_ms

    def snapshot(self) -> DataEngineMetrics:
        """Return current metrics snapshot."""
        m = DataEngineMetrics()

        m.total_events_received = self.total_events_received
        m.total_events_normalized = self.total_events_normalized
        m.total_events_dropped = self.total_events_dropped

        # EPS
        window_count = len(self._event_timestamps)
        if window_count > 0 and len(self._event_timestamps) >= 2:
            span = self._event_timestamps[-1] - self._event_timestamps[0]
            m.events_per_second = window_count / max(span, 0.001)

        m.ws_connections = self.ws_connections
        m.ws_reconnects = self.ws_reconnects
        m.ws_messages_received = self.ws_messages
        m.rest_requests = self.rest_requests
        m.rest_429s = self.rest_429s
        m.rest_errors = self.rest_errors

        # Latency percentiles
        if self._latency_samples:
            sorted_lat = sorted(self._latency_samples)
            n = len(sorted_lat)
            m.latency_exchange_to_local_p50_ms = sorted_lat[n // 2]
            m.latency_exchange_to_local_p95_ms = sorted_lat[int(n * 0.95)]
            m.latency_exchange_to_local_p99_ms = sorted_lat[int(n * 0.99)]
            m.latency_exchange_to_local_max_ms = sorted_lat[-1]

        m.books_maintained = self.books_maintained
        m.books_healthy = self.books_healthy
        m.books_out_of_sync = self.books_out_of_sync
        m.book_resyncs = self.book_resyncs
        m.clock_offset_max_ms = self.clock_offset_max

        return m
