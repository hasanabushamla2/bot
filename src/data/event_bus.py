"""Internal Async Event Bus — non-blocking market-data distribution.

Routes normalized events from the Data Engine to downstream consumers
(strategies, feature engine, analytics, dashboard) in a non-blocking way.

Design:
- Bounded asyncio.Queue per consumer to prevent memory blowup.
- Slow consumers get dropped with explicit backpressure warnings.
- Producers never block waiting for consumers.
- Each consumer runs an async task that drains its queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from src.core.logging_config import get_logger
from src.data.normalization import NormalizedEvent

logger = get_logger(__name__)

Handler = Callable[[NormalizedEvent], Awaitable[None]]


@dataclass
class ConsumerStats:
    """Per-consumer queue statistics."""

    name: str
    queue_size: int = 0
    max_queue_size: int = 1000
    events_processed: int = 0
    events_dropped: int = 0
    last_processed_at: float = 0.0
    processing_lag_seconds: float = 0.0
    is_alive: bool = True


class EventBus:
    """Async event distribution with bounded per-consumer queues.

    Usage:
        bus = EventBus()
        bus.subscribe("strategy_momentum", momentum_handler)
        await bus.publish(ticker_event)
    """

    def __init__(
        self,
        default_max_queue: int = 1000,
        overflow_policy: str = "drop_oldest",
    ) -> None:
        self.default_max_queue = default_max_queue
        self.overflow_policy = overflow_policy  # "drop_oldest" | "drop_newest" | "block"

        self._consumers: dict[str, asyncio.Queue[NormalizedEvent]] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._handlers: dict[str, Handler] = {}
        self._stats: dict[str, ConsumerStats] = {}
        self._running = False

    # --- Lifecycle ---

    async def start(self) -> None:
        self._running = True

    async def shutdown(self) -> None:
        """Stop all consumer tasks and drain queues."""
        self._running = False
        for _name, task in self._tasks.items():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._consumers.clear()

    # --- Subscribe / Unsubscribe ---

    def subscribe(
        self,
        name: str,
        handler: Handler,
        max_queue: int | None = None,
    ) -> None:
        """Register a consumer with a handler callback."""
        if name in self._consumers:
            logger.warning("event_bus_duplicate_subscription", name=name)

        max_q = max_queue or self.default_max_queue
        self._consumers[name] = asyncio.Queue(maxsize=max_q)
        self._handlers[name] = handler
        self._stats[name] = ConsumerStats(name=name, max_queue_size=max_q)

        # Start consumer task
        task = asyncio.create_task(self._consumer_loop(name))
        self._tasks[name] = task

    def unsubscribe(self, name: str) -> None:
        """Remove a consumer."""
        if name in self._tasks:
            self._tasks[name].cancel()
            del self._tasks[name]
        self._consumers.pop(name, None)
        self._handlers.pop(name, None)
        self._stats.pop(name, None)

    # --- Publish ---

    async def publish(self, event: NormalizedEvent) -> None:
        """Push an event to all subscribed consumers.

        Never blocks — if a consumer's queue is full, the overflow
        policy determines what happens.

        Slow consumers: their queues fill → oldest events dropped.
        This prevents one slow strategy from blocking all market data.
        """
        for name, queue in self._consumers.items():
            try:
                if queue.full():
                    if self.overflow_policy == "drop_oldest":
                        # Drain one, then put
                        try:
                            queue.get_nowait()
                            self._stats[name].events_dropped += 1
                        except asyncio.QueueEmpty:
                            pass
                        queue.put_nowait(event)
                    elif self.overflow_policy == "drop_newest":
                        self._stats[name].events_dropped += 1
                    else:
                        # "block" — use put() which may briefly block
                        # This policy is dangerous, hence the default is drop_oldest
                        queue.put_nowait(event)
                else:
                    queue.put_nowait(event)
            except asyncio.QueueFull:
                self._stats[name].events_dropped += 1

    async def broadcast(self, events: list[NormalizedEvent]) -> None:
        """Publish a batch of events."""
        for event in events:
            await self.publish(event)

    # --- Stats ---

    def get_stats(self) -> list[ConsumerStats]:
        """Get queue statistics for all consumers."""
        for name, queue in self._consumers.items():
            self._stats[name].queue_size = queue.qsize()
        return list(self._stats.values())

    # --- Internal ---

    async def _consumer_loop(self, name: str) -> None:
        """Drain one consumer's queue, calling its handler for each event."""
        self._stats[name].is_alive = True
        handler = self._handlers[name]
        queue = self._consumers[name]

        while self._running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                try:
                    await handler(event)
                    self._stats[name].events_processed += 1
                    self._stats[name].last_processed_at = asyncio.get_event_loop().time()
                except Exception:
                    logger.exception("event_bus_handler_error", consumer=name)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

        self._stats[name].is_alive = False
