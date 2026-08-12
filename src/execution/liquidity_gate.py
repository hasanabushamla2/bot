"""Centralized Liquidity Gate — pre-trade market quality & execution risk defense.

Evaluates:
- Bid/ask spread & spread_bps
- Top-of-book depth
- Cumulative depth across multiple levels
- Quote-volume / 24h volume
- Expected entry slippage
- Expected exit slippage
- Order book gaps & ordering anomalies
- Stale market data
- Executable notional & participation rate
- Number of book levels required to execute
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from src.core.logging_config import get_logger
from src.data.order_book import OrderBookState
from src.portfolio.market_quality import MarketQualityCalculator

logger = get_logger(__name__)


class LiquidityRejectionReason(str, Enum):
    LIQUIDITY_TOO_LOW = "LIQUIDITY_TOO_LOW"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    ENTRY_SLIPPAGE_TOO_HIGH = "ENTRY_SLIPPAGE_TOO_HIGH"
    EXIT_SLIPPAGE_TOO_HIGH = "EXIT_SLIPPAGE_TOO_HIGH"
    BOOK_TOO_SHALLOW = "BOOK_TOO_SHALLOW"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    PARTICIPATION_TOO_HIGH = "PARTICIPATION_TOO_HIGH"
    ABNORMAL_BOOK_GAP = "ABNORMAL_BOOK_GAP"
    BOOK_DATA_MISSING = "BOOK_DATA_MISSING"
    INVALID_BOOK_ORDERING = "INVALID_BOOK_ORDERING"
    EXPECTED_EDGE_TOO_LOW = "EXPECTED_EDGE_TOO_LOW"
    EFFECTIVE_STOP_LOSS_TOO_HIGH = "EFFECTIVE_STOP_LOSS_TOO_HIGH"
    MALFORMED_BOOK = "MALFORMED_BOOK"


@dataclass
class LiquidityGateConfig:
    """Configurable limits for Liquidity Gate with conservative PAPER defaults."""

    max_spread_bps: float = 35.0  # Max acceptable bid-ask spread in bps
    max_entry_slippage_bps: float = 25.0  # Target 20-30 bps max entry slippage
    max_expected_exit_slippage_bps: float = 35.0  # Max acceptable emergency exit slippage
    max_book_levels_consumed: int = 8  # Max levels of book to consume
    max_depth_participation_pct: float = 0.10  # Max 10% participation in visible book
    min_top_book_notional: float = 500.0  # Min $500 visible at top of book
    min_cumulative_book_notional: float = 5000.0  # Min $5,000 total visible depth
    min_quote_volume_24h: float = 100_000.0  # Min $100k 24h volume
    max_market_data_age_seconds: float = 45.0  # Max market data age
    max_gap_pct: float = 1.0  # Max 1.0% gap between adjacent book levels
    min_market_quality_score: float = 0.40  # Min Market Quality Score [0.0, 1.0]
    max_effective_stop_loss_pct: float = 0.80  # Max total effective loss on stop (0.3% stop + exit slippage <= 0.80%)


@dataclass
class LiquidityAssessment:
    passed: bool = False
    reason: LiquidityRejectionReason | None = None
    message: str = ""
    symbol: str = ""
    spread_bps: float = 0.0
    top_bid_notional: float = 0.0
    top_ask_notional: float = 0.0
    cumulative_bid_notional: float = 0.0
    cumulative_ask_notional: float = 0.0
    market_quality_score: float = 0.0
    data_age_seconds: float = 0.0
    assessed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class LiquidityGate:
    """Centralized gate verifying that a market is safe for trading before risk/order creation."""

    def __init__(self, config: LiquidityGateConfig | None = None) -> None:
        self.config = config or LiquidityGateConfig()
        self.rejection_counts: dict[str, int] = {r.value: 0 for r in LiquidityRejectionReason}
        self.total_checks: int = 0
        self.total_rejections: int = 0

    def assess_market(
        self,
        symbol: str,
        book: OrderBookState | None,
        volume_24h: float,
        data_age_seconds: float,
        expected_slippage_bps: float = 0.0,
    ) -> LiquidityAssessment:
        """Evaluate raw market condition before strategy or risk evaluation."""
        self.total_checks += 1
        cfg = self.config

        # 1. Freshness check
        if data_age_seconds > cfg.max_market_data_age_seconds:
            return self._reject(
                LiquidityRejectionReason.MARKET_DATA_STALE,
                f"Data age {data_age_seconds:.1f}s > {cfg.max_market_data_age_seconds:.1f}s",
                symbol=symbol, data_age_seconds=data_age_seconds,
            )

        # 2. Book presence check
        if book is None or not book.bids.levels or not book.asks.levels:
            return self._reject(
                LiquidityRejectionReason.BOOK_DATA_MISSING,
                "Order book data missing or empty",
                symbol=symbol, data_age_seconds=data_age_seconds,
            )

        best_bid, best_bid_qty = book.best_bid, book.best_bid_qty
        best_ask, best_ask_qty = book.best_ask, book.best_ask_qty

        # 3. Book validity check
        if (
            best_bid <= 0
            or best_ask <= 0
            or best_bid_qty <= 0
            or best_ask_qty <= 0
            or not math.isfinite(best_bid)
            or not math.isfinite(best_ask)
            or best_ask <= best_bid
        ):
            return self._reject(
                LiquidityRejectionReason.MALFORMED_BOOK,
                f"Invalid top of book: bid={best_bid} (qty={best_bid_qty}), ask={best_ask} (qty={best_ask_qty})",
                symbol=symbol, data_age_seconds=data_age_seconds,
            )

        # 4. Book ordering check
        bids = book.bids.levels
        asks = book.asks.levels
        for i in range(len(bids) - 1):
            if bids[i][0] < bids[i + 1][0]:
                return self._reject(
                    LiquidityRejectionReason.INVALID_BOOK_ORDERING,
                    f"Bids not descending at level {i}: {bids[i][0]} < {bids[i+1][0]}",
                    symbol=symbol,
                )
        for i in range(len(asks) - 1):
            if asks[i][0] > asks[i + 1][0]:
                return self._reject(
                    LiquidityRejectionReason.INVALID_BOOK_ORDERING,
                    f"Asks not ascending at level {i}: {asks[i][0]} > {asks[i+1][0]}",
                    symbol=symbol,
                )

        # 5. Spread check
        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10000.0
        if spread_bps > cfg.max_spread_bps:
            return self._reject(
                LiquidityRejectionReason.SPREAD_TOO_WIDE,
                f"Spread {spread_bps:.1f} bps > max {cfg.max_spread_bps:.1f} bps",
                symbol=symbol, spread_bps=spread_bps,
            )

        # 6. Top of book depth check
        top_bid_notional = best_bid * best_bid_qty
        top_ask_notional = best_ask * best_ask_qty
        if top_bid_notional < cfg.min_top_book_notional or top_ask_notional < cfg.min_top_book_notional:
            return self._reject(
                LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                f"Top notional too shallow: bid=${top_bid_notional:.0f}, ask=${top_ask_notional:.0f} < ${cfg.min_top_book_notional:.0f}",
                symbol=symbol, spread_bps=spread_bps,
                top_bid_notional=top_bid_notional, top_ask_notional=top_ask_notional,
            )

        # 7. Cumulative depth check
        cum_bid_notional = sum(p * q for p, q in bids)
        cum_ask_notional = sum(p * q for p, q in asks)
        if (
            cum_bid_notional < cfg.min_cumulative_book_notional
            or cum_ask_notional < cfg.min_cumulative_book_notional
        ):
            return self._reject(
                LiquidityRejectionReason.BOOK_TOO_SHALLOW,
                f"Cumulative depth too shallow: bids=${cum_bid_notional:.0f}, asks=${cum_ask_notional:.0f} < ${cfg.min_cumulative_book_notional:.0f}",
                symbol=symbol, spread_bps=spread_bps,
                cumulative_bid_notional=cum_bid_notional, cumulative_ask_notional=cum_ask_notional,
            )

        # 8. Order book gaps check
        for i in range(min(5, len(bids) - 1)):
            gap_pct = (bids[i][0] - bids[i + 1][0]) / bids[i][0] * 100.0
            if gap_pct > cfg.max_gap_pct:
                return self._reject(
                    LiquidityRejectionReason.ABNORMAL_BOOK_GAP,
                    f"Abnormal bid gap {gap_pct:.2f}% at level {i} > {cfg.max_gap_pct:.2f}%",
                    symbol=symbol, spread_bps=spread_bps,
                )
        for i in range(min(5, len(asks) - 1)):
            gap_pct = (asks[i + 1][0] - asks[i][0]) / asks[i][0] * 100.0
            if gap_pct > cfg.max_gap_pct:
                return self._reject(
                    LiquidityRejectionReason.ABNORMAL_BOOK_GAP,
                    f"Abnormal ask gap {gap_pct:.2f}% at level {i} > {cfg.max_gap_pct:.2f}%",
                    symbol=symbol, spread_bps=spread_bps,
                )

        # 9. 24h Volume check
        if volume_24h < cfg.min_quote_volume_24h:
            return self._reject(
                LiquidityRejectionReason.LIQUIDITY_TOO_LOW,
                f"Volume 24h ${volume_24h:,.0f} < min ${cfg.min_quote_volume_24h:,.0f}",
                symbol=symbol, spread_bps=spread_bps,
            )

        # 10. Depth within 10 bps
        depth_10bps = book.depth_within_bps(10)
        depth_10bps_usd = depth_10bps * mid

        # 11. Market Quality Score
        mq = MarketQualityCalculator.compute(
            spread_bps=spread_bps,
            depth_usd_10bps=depth_10bps_usd,
            volume_24h_usd=volume_24h,
            data_age_seconds=data_age_seconds,
            expected_slippage_bps=expected_slippage_bps,
            max_acceptable_spread_bps=cfg.max_spread_bps,
        )

        if mq.total_score < cfg.min_market_quality_score:
            return self._reject(
                LiquidityRejectionReason.LIQUIDITY_TOO_LOW,
                f"Market Quality Score {mq.total_score:.2f} < min {cfg.min_market_quality_score:.2f}",
                symbol=symbol, spread_bps=spread_bps, market_quality_score=mq.total_score,
            )

        # PASSED ALL CHECKS
        return LiquidityAssessment(
            passed=True,
            symbol=symbol,
            spread_bps=spread_bps,
            top_bid_notional=top_bid_notional,
            top_ask_notional=top_ask_notional,
            cumulative_bid_notional=cum_bid_notional,
            cumulative_ask_notional=cum_ask_notional,
            market_quality_score=mq.total_score,
            data_age_seconds=data_age_seconds,
        )

    def _reject(
        self,
        reason: LiquidityRejectionReason,
        message: str,
        symbol: str = "",
        spread_bps: float = 0.0,
        top_bid_notional: float = 0.0,
        top_ask_notional: float = 0.0,
        cumulative_bid_notional: float = 0.0,
        cumulative_ask_notional: float = 0.0,
        market_quality_score: float = 0.0,
        data_age_seconds: float = 0.0,
    ) -> LiquidityAssessment:
        self.rejection_counts[reason.value] = self.rejection_counts.get(reason.value, 0) + 1
        self.total_rejections += 1
        logger.debug("liquidity_gate_rejected", symbol=symbol, reason=reason.value, message=message)
        return LiquidityAssessment(
            passed=False,
            reason=reason,
            message=message,
            symbol=symbol,
            spread_bps=spread_bps,
            top_bid_notional=top_bid_notional,
            top_ask_notional=top_ask_notional,
            cumulative_bid_notional=cumulative_bid_notional,
            cumulative_ask_notional=cumulative_ask_notional,
            market_quality_score=market_quality_score,
            data_age_seconds=data_age_seconds,
        )
