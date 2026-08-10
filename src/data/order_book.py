"""Local Order Book Engine — deterministic, real-time order book maintenance.

Maintains a local copy of the exchange order book from snapshot + delta
updates. Handles sequence-number gaps, resynchronization, duplicate
updates, and out-of-order updates.

Queries: best_bid, best_ask, mid_price, spread_bps, depth_within_bps,
VWAP for candidate size.

Integrates with LiquidityAnalyzer and CapacityEstimator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.core.logging_config import get_logger
from src.data.normalization import BookLevel, CanonicalSymbol, OrderBookDelta, OrderBookSnapshot

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal order-book representation
# ---------------------------------------------------------------------------


class _SideBook:
    """Price-sorted book for one side (bids or asks).

    Bids: descending price (highest first)
    Asks: ascending price (lowest first)
    Uses list-of-lists for mutable price levels with O(log n) lookup.
    """

    def __init__(self, is_bids: bool) -> None:
        self._is_bids = is_bids
        # Sorted list of [price, quantity] — bids descending, asks ascending
        self._levels: list[list[float]] = []
        # Fast price → index lookup (approximate, bisect-based)

    @property
    def levels(self) -> list[list[float]]:
        return self._levels

    @property
    def count(self) -> int:
        return len(self._levels)

    def best(self) -> tuple[float, float]:
        """Best price + quantity."""
        if not self._levels:
            return (0.0, 0.0)
        level = self._levels[0]
        return (level[0], level[1])

    def apply_snapshot(self, levels: list[BookLevel]) -> None:
        """Replace entire side from snapshot."""
        sorted_levels = sorted(levels, key=lambda x: x.price, reverse=self._is_bids)
        self._levels = [[lv.price, lv.quantity] for lv in sorted_levels]

    def apply_delta(self, updates: tuple[BookLevel, ...]) -> None:
        """Apply incremental updates to this side.

        Updates with quantity == 0 are deletions.
        """
        for update in updates:
            self._upsert(update.price, update.quantity)

    def _upsert(self, price: float, quantity: float) -> None:
        """Insert or update a price level. Delete if quantity <= 0."""
        # Find insertion point
        if self._is_bids:
            # Bids descending: find where price <= existing, or insert before
            idx = self._find_idx_bids(price)
        else:
            # Asks ascending: find where price >= existing
            idx = self._find_idx_asks(price)

        if idx < len(self._levels) and self._levels[idx][0] == price:
            # Update existing
            if quantity <= 0:
                del self._levels[idx]
            else:
                self._levels[idx][1] = quantity
        elif quantity > 0:
            # Insert new
            self._levels.insert(idx, [price, quantity])

    def _find_idx_bids(self, price: float) -> int:
        """Find insertion index for bids (descending order)."""
        # Binary search for first element with price <= target
        lo, hi = 0, len(self._levels)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._levels[mid][0] > price:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def _find_idx_asks(self, price: float) -> int:
        """Find insertion index for asks (ascending order)."""
        lo, hi = 0, len(self._levels)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._levels[mid][0] < price:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def depth_up_to_price(self, target_price: float) -> float:
        """Cumulative quantity from best price to target_price."""
        total = 0.0
        for price, qty in self._levels:
            if self._is_bids:
                if price >= target_price:
                    total += qty
                else:
                    break
            else:
                if price <= target_price:
                    total += qty
                else:
                    break
        return total

    def depth_by_levels(self, n_levels: int) -> float:
        """Cumulative quantity for the first `n_levels` levels."""
        total = 0.0
        for i in range(min(n_levels, len(self._levels))):
            total += self._levels[i][1]
        return total

    def vwap_for_size(self, size: float) -> float | None:
        """Volume-weighted average price for executing `size` quantity."""
        if size <= 0 or not self._levels:
            return None
        remaining = size
        total_cost = 0.0
        for price, qty in self._levels:
            fill = min(remaining, qty)
            total_cost += fill * price
            remaining -= fill
            if remaining <= 0:
                break
        if remaining > 0:
            return None  # Not enough depth
        return total_cost / size


# ---------------------------------------------------------------------------
# Order-book state
# ---------------------------------------------------------------------------


@dataclass
class OrderBookState:
    """Complete local order book for one symbol."""

    symbol: str
    exchange: str
    bids: _SideBook = field(default_factory=lambda: _SideBook(is_bids=True))
    asks: _SideBook = field(default_factory=lambda: _SideBook(is_bids=False))
    last_update_id: int = 0
    last_sequence_id: int | None = None
    last_update_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    initialized: bool = False
    out_of_sync: bool = False
    snapshot_requested: bool = False

    # --- Queries ---

    @property
    def best_bid(self) -> float:
        return self.bids.best()[0]

    @property
    def best_ask(self) -> float:
        return self.asks.best()[0]

    @property
    def best_bid_qty(self) -> float:
        return self.bids.best()[1]

    @property
    def best_ask_qty(self) -> float:
        return self.asks.best()[1]

    @property
    def mid_price(self) -> float:
        bb, ba = self.best_bid, self.best_ask
        if bb <= 0 or ba <= 0:
            return 0.0
        return (bb + ba) / 2.0

    @property
    def spread_bps(self) -> float:
        """Spread in basis points."""
        bb, ba = self.best_bid, self.best_ask
        mid = (bb + ba) / 2.0
        if mid <= 0:
            return 0.0
        return (ba - bb) / mid * 10000.0

    def depth_within_bps(self, bps: int) -> float:
        """Total bid quantity available within `bps` of mid price."""
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        target = mid * (1.0 - bps / 10000.0)
        return self.bids.depth_up_to_price(target)

    def vwap_for_size(self, size: float, side: str = "buy") -> float | None:
        """Estimate VWAP for executing `size` on the given side."""
        if side == "buy":
            return self.asks.vwap_for_size(size)
        else:
            return self.bids.vwap_for_size(size)

    def to_snapshot(self) -> OrderBookSnapshot:
        """Export as immutable snapshot for downstream consumers."""
        bids = [BookLevel(price=p, quantity=q) for p, q in self.bids.levels]
        asks = [BookLevel(price=p, quantity=q) for p, q in self.asks.levels]
        return OrderBookSnapshot.create(
            exchange=self.exchange,
            canonical=self._canonical(),
            bids=bids,
            asks=asks,
            last_update_id=self.last_update_id,
        )

    def _canonical(self) -> CanonicalSymbol:
        return CanonicalSymbol.from_exchange_symbol(self.exchange, self.symbol)


# ---------------------------------------------------------------------------
# Order-book engine
# ---------------------------------------------------------------------------


class OrderBookEngine:
    """Manages local order books for all subscribed symbols.

    Binance depth-stream protocol:
    1. Fetch a REST snapshot (GET /api/v3/depth?symbol=BTCUSDT&limit=1000).
    2. Subscribe to the WebSocket diff stream (btcusdt@depth@100ms).
    3. Buffer diffs until first diff where `U <= lastUpdateId+1 <= u`.
    4. Apply snapshot, then replay buffered diffs from that point.
    5. Process subsequent diffs: discard if `u < lastUpdateId+1`;
       log warning if `U > lastUpdateId+1` (gap).
    """

    def __init__(self, max_books: int = 200) -> None:
        self._books: dict[str, OrderBookState] = {}  # key = "exchange:symbol"
        self.max_books = max_books
        # Buffered diffs waiting for initial snapshot
        self._pending_diffs: dict[str, list[OrderBookDelta]] = {}

    # --- Book access ---

    def get_book(self, exchange: str, symbol: str) -> OrderBookState | None:
        return self._books.get(_book_key(exchange, symbol))

    def get_or_create(self, exchange: str, symbol: str) -> OrderBookState:
        key = _book_key(exchange, symbol)
        if key not in self._books:
            if len(self._books) >= self.max_books:
                # Evict oldest
                oldest = min(
                    self._books.items(),
                    key=lambda kv: kv[1].last_update_time,
                )[0]
                del self._books[oldest]
            self._books[key] = OrderBookState(symbol=symbol, exchange=exchange)
            self._pending_diffs[key] = []
        return self._books[key]

    # --- Snapshot ---

    def apply_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        """Replace the local book with a full REST snapshot."""
        key = _book_key(snapshot.exchange, snapshot.symbol)
        book = self.get_or_create(snapshot.exchange, snapshot.symbol)

        book.bids.apply_snapshot(list(snapshot.bids))
        book.asks.apply_snapshot(list(snapshot.asks))
        book.last_update_id = snapshot.last_update_id
        book.initialized = True
        book.out_of_sync = False
        book.last_update_time = snapshot.local_receive_timestamp

        # Replay buffered diffs that are now applicable
        buffered = self._pending_diffs.get(key, [])
        to_apply = [d for d in buffered if d.final_update_id >= book.last_update_id + 1]
        for delta in to_apply:
            self._apply_delta_inner(book, delta)
        self._pending_diffs[key] = []

    # --- Delta ---

    def apply_delta(self, delta: OrderBookDelta) -> bool:
        """Apply an incremental depth update.

        Returns True if the delta was applied, False if buffered or dropped.
        """
        key = _book_key(delta.exchange, delta.symbol)
        book = self._books.get(key)

        if book is None:
            book = self.get_or_create(delta.exchange, delta.symbol)

        if not book.initialized:
            # Buffer until snapshot arrives
            self._pending_diffs.setdefault(key, []).append(delta)
            if len(self._pending_diffs[key]) > 5000:
                # Too many buffered — request resync
                book.out_of_sync = True
                book.snapshot_requested = True
                self._pending_diffs[key] = self._pending_diffs[key][-1000:]
            return False

        # --- Consistency checks ---
        if delta.final_update_id <= book.last_update_id:
            # Stale update — already reflected
            return False

        if delta.first_update_id > book.last_update_id + 1:
            # Gap detected — need resync
            logger.warning(
                "order_book_gap",
                symbol=delta.symbol,
                book_last=book.last_update_id,
                delta_first=delta.first_update_id,
            )
            book.out_of_sync = True
            book.snapshot_requested = True
            return False

        # --- Apply ---
        self._apply_delta_inner(book, delta)
        return True

    def _apply_delta_inner(self, book: OrderBookState, delta: OrderBookDelta) -> None:
        """Apply delta to an initialized book (no gap checks)."""
        if delta.bids:
            book.bids.apply_delta(delta.bids)
        if delta.asks:
            book.asks.apply_delta(delta.asks)
        book.last_update_id = delta.final_update_id
        book.last_sequence_id = delta.sequence_id
        book.last_update_time = datetime.now(UTC)

    # --- Status ---

    def needs_resync(self, exchange: str, symbol: str) -> bool:
        book = self.get_book(exchange, symbol)
        return book is not None and book.out_of_sync

    def mark_resync_requested(self, exchange: str, symbol: str) -> None:
        book = self.get_book(exchange, symbol)
        if book:
            book.snapshot_requested = True

    def is_healthy(self, exchange: str, symbol: str, max_age_seconds: float = 10.0) -> bool:
        book = self.get_book(exchange, symbol)
        if book is None or not book.initialized:
            return False
        age = (datetime.now(UTC) - book.last_update_time).total_seconds()
        return age < max_age_seconds and not book.out_of_sync


def _book_key(exchange: str, symbol: str) -> str:
    return f"{exchange}:{symbol}"
