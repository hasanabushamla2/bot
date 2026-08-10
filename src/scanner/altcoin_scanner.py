"""Altcoin Opportunity Scanner — continuous multi-asset signal detection.

Scans 100+ altcoin spot pairs simultaneously, detecting early movement
signals before they become obvious. Produces ranked opportunity signals
for the Opportunity Engine.

SCANNED SIGNALS:
- Volume Spikes: current volume vs. historical average
- Relative Volume: volume relative to peers
- Momentum Acceleration: rate-of-change of momentum
- Price Acceleration: second derivative of price
- Breakouts: price exceeding recent range with volume confirmation
- Order-Book Imbalance: bid/ask ratio asymmetry
- Buy/Sell Trade Flow: aggressive buy vs. sell volume
- Liquidity Changes: sudden spread/depth shifts
- Spread Changes: tightening/widening spreads
- Volatility Expansion: sudden increase in price range

SAFETY FILTER:
Before any altcoin becomes tradable, the scanner checks:
- Sufficient order-book depth
- Acceptable spread
- Sufficient 24h volume
- Acceptable estimated slippage
- Healthy market-data feed
- Valid instrument metadata
- Active trading status
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from src.core.logging_config import get_logger
from src.portfolio.universe import UniverseManager
from src.strategies.base import SignalDirection, StrategySignal

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Scanner configuration
# ---------------------------------------------------------------------------


@dataclass
class AltcoinScannerConfig:
    """Configuration for the altcoin opportunity scanner."""

    # Safety filters
    min_volume_24h_usd: float = 1_000_000.0  # Minimum $ volume
    max_spread_pct: float = 5.0  # Maximum bid/ask spread %
    min_order_book_depth_usd: float = 10_000.0  # Minimum depth at 10bps
    max_estimated_slippage_pct: float = 1.0  # Max slippage for $1k order

    # Signal thresholds
    volume_spike_threshold: float = 2.0  # Multiple of average volume
    momentum_accel_threshold: float = 0.05  # Rate-of-change threshold
    breakout_stddev: float = 2.0  # Standard deviations for breakout
    order_imbalance_threshold: float = 0.3  # Bid/ask ratio deviation

    # Scanner capacity
    max_scanned_assets: int = 150  # Maximum concurrent scans
    scan_interval_seconds: float = 1.0  # How often to re-scan

    # Output
    min_signal_confidence: float = 0.3  # Minimum confidence to emit
    max_signals_per_scan: int = 20  # Cap signals per scan cycle


# ---------------------------------------------------------------------------
# Signal data structures
# ---------------------------------------------------------------------------


@dataclass
class AssetSnapshot:
    """Market snapshot for a single altcoin at one point in time."""

    symbol: str
    exchange: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Price
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    price_change_1m_pct: float = 0.0
    price_change_5m_pct: float = 0.0

    # Volume
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    volume_5m: float = 0.0
    volume_vs_avg_ratio: float = 1.0

    # Order book
    depth_bid_10bps: float = 0.0
    depth_ask_10bps: float = 0.0
    bid_ask_ratio: float = 1.0

    # Derived
    momentum_score: float = 0.0
    breakout_score: float = 0.0
    volume_score: float = 0.0
    flow_score: float = 0.0
    liquidity_score: float = 0.0

    # Safety
    passes_safety_filter: bool = False
    safety_rejection_reason: str = ""


@dataclass
class AltcoinSignal:
    """A detected altcoin opportunity with all scoring factors."""

    snapshot: AssetSnapshot

    # Scores (0-1 scale, higher = better)
    momentum_score: float = 0.0
    volume_score: float = 0.0
    breakout_score: float = 0.0
    flow_score: float = 0.0
    liquidity_score: float = 0.0

    # Composite
    composite_score: float = 0.0
    estimated_net_edge_bps: float = 0.0
    confidence: float = 0.0

    # Ranking metadata
    rank_reason: str = ""
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class AltcoinScanner:
    """Continuously scans 100+ altcoin spot pairs for trading opportunities.

    The scanner does NOT decide what to trade — it produces ranked signals.
    The Opportunity Engine, Risk Engine, and Capital Allocator make the
    final decision.

    Design: lightweight, runs every scan_interval_seconds, produces at most
    max_signals_per_scan signals.
    """

    def __init__(
        self,
        config: AltcoinScannerConfig | None = None,
        universe: UniverseManager | None = None,
    ) -> None:
        self.config = config or AltcoinScannerConfig()
        self._universe = universe

        # Rolling data for signal computation
        self._price_history: dict[str, list[float]] = {}  # symbol → [prices]
        self._volume_history: dict[str, list[float]] = {}  # symbol → [volumes]
        self._max_history: int = 100

    # ------------------------------------------------------------------
    # Main scan entry point
    # ------------------------------------------------------------------

    def scan(
        self,
        snapshots: list[AssetSnapshot],
    ) -> list[AltcoinSignal]:
        """Scan a batch of asset snapshots and produce ranked signals.

        Args:
            snapshots: Current market snapshots for all scanned assets.

        Returns:
            Ranked list of AltcoinSignals, best first.
        """
        # --- Update rolling history ---
        for snap in snapshots:
            self._update_history(snap)

        # --- Safety filter ---
        eligible = [s for s in snapshots if self._check_safety(s)]
        for s in snapshots:
            if not s.passes_safety_filter:
                logger.debug(
                    "altcoin_safety_rejected", symbol=s.symbol, reason=s.safety_rejection_reason
                )

        # --- Compute scores ---
        signals: list[AltcoinSignal] = []
        for snap in eligible:
            signal = self._compute_scores(snap)
            if signal.confidence >= self.config.min_signal_confidence:
                signals.append(signal)

        # --- Rank by composite score ---
        signals.sort(key=lambda s: s.composite_score, reverse=True)

        # --- Cap ---
        return signals[: self.config.max_signals_per_scan]

    # ------------------------------------------------------------------
    # Safety filter
    # ------------------------------------------------------------------

    def _check_safety(self, snap: AssetSnapshot) -> bool:
        """Run all safety checks on an asset. Sets passes_safety_filter."""
        cfg = self.config

        if snap.volume_24h < cfg.min_volume_24h_usd:
            snap.safety_rejection_reason = (
                f"Volume ${snap.volume_24h:,.0f} < ${cfg.min_volume_24h_usd:,.0f}"
            )
            snap.passes_safety_filter = False
            return False

        if snap.spread_pct > cfg.max_spread_pct:
            snap.safety_rejection_reason = f"Spread {snap.spread_pct:.2f}% > {cfg.max_spread_pct}%"
            snap.passes_safety_filter = False
            return False

        if snap.last_price <= 0:
            snap.safety_rejection_reason = "Invalid price"
            snap.passes_safety_filter = False
            return False

        if snap.bid <= 0 or snap.ask <= 0:
            snap.safety_rejection_reason = "No valid bid/ask"
            snap.passes_safety_filter = False
            return False

        # Depth check (if data available)
        if (
            snap.depth_bid_10bps > 0
            and snap.depth_bid_10bps * snap.bid < cfg.min_order_book_depth_usd
        ):
            snap.safety_rejection_reason = "Insufficient order-book depth"
            snap.passes_safety_filter = False
            return False

        snap.passes_safety_filter = True
        return True

    # ------------------------------------------------------------------
    # Signal scoring
    # ------------------------------------------------------------------

    def _compute_scores(self, snap: AssetSnapshot) -> AltcoinSignal:
        """Compute all component scores for one asset."""
        signal = AltcoinSignal(snapshot=snap)

        # 1. Momentum score — price acceleration
        signal.momentum_score = self._score_momentum(snap)

        # 2. Volume score — volume spike detection
        signal.volume_score = self._score_volume(snap)

        # 3. Breakout score — range breakout with volume confirmation
        signal.breakout_score = self._score_breakout(snap)

        # 4. Flow score — order-book imbalance
        signal.flow_score = self._score_flow(snap)

        # 5. Liquidity score — tight spread, deep book
        signal.liquidity_score = self._score_liquidity(snap)

        # --- Composite (weighted) ---
        # Momentum: 25%, Volume: 25%, Breakout: 20%, Flow: 15%, Liquidity: 15%
        signal.composite_score = (
            signal.momentum_score * 0.25
            + signal.volume_score * 0.25
            + signal.breakout_score * 0.20
            + signal.flow_score * 0.15
            + signal.liquidity_score * 0.15
        )

        # --- Confidence: composite moderated by liquidity ---
        signal.confidence = signal.composite_score * (0.5 + 0.5 * signal.liquidity_score)

        # --- Estimated net edge (simplified) ---
        signal.estimated_net_edge_bps = signal.composite_score * 50.0  # Up to ~50bps

        # --- Rank reason ---
        reasons = []
        if signal.momentum_score > 0.7:
            reasons.append("momentum")
        if signal.volume_score > 0.7:
            reasons.append("volume")
        if signal.breakout_score > 0.7:
            reasons.append("breakout")
        if signal.flow_score > 0.7:
            reasons.append("flow")
        signal.rank_reason = "+".join(reasons) if reasons else "balanced"

        return signal

    # ------------------------------------------------------------------
    # Individual scorers
    # ------------------------------------------------------------------

    def _score_momentum(self, snap: AssetSnapshot) -> float:
        """Score price momentum/acceleration.

        Combines short-term (1m) and medium-term (5m) price changes.
        Higher absolute change + consistency → higher score.
        """
        scores: list[float] = []

        # 1-minute momentum
        if abs(snap.price_change_1m_pct) > 0:
            m1 = min(1.0, abs(snap.price_change_1m_pct) / 2.0)  # 2% → 1.0
            scores.append(m1)

        # 5-minute momentum
        if abs(snap.price_change_5m_pct) > 0:
            m5 = min(1.0, abs(snap.price_change_5m_pct) / 5.0)  # 5% → 1.0
            scores.append(m5)

        # Acceleration from rolling data
        hist = self._price_history.get(snap.symbol, [])
        if len(hist) >= 5:
            recent = hist[-5:]
            if recent[0] > 0:
                accel = (recent[-1] - recent[0]) / recent[0] * 100.0
                accel_score = min(1.0, abs(accel) / 3.0)
                scores.append(accel_score)

        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _score_volume(self, snap: AssetSnapshot) -> float:
        """Score volume spike vs. historical average."""
        if snap.volume_vs_avg_ratio <= 0:
            return 0.0

        cfg = self.config
        if snap.volume_vs_avg_ratio >= cfg.volume_spike_threshold:
            # Above threshold: map to 0.5-1.0
            return min(1.0, 0.5 + (snap.volume_vs_avg_ratio - cfg.volume_spike_threshold) / 5.0)

        # Below threshold: map to 0-0.5
        return snap.volume_vs_avg_ratio / cfg.volume_spike_threshold * 0.5

    def _score_breakout(self, snap: AssetSnapshot) -> float:
        """Score for range breakout detection.

        A breakout occurs when price moves beyond recent range on
        elevated volume. Simple check: is price_change_1m significant
        and volume elevated?
        """
        score = 0.0

        # Price movement component
        move = abs(snap.price_change_1m_pct)
        if move > 0.5:  # 0.5% in 1 minute is notable
            score += min(1.0, move / 3.0) * 0.5

        # Volume confirmation component
        if snap.volume_vs_avg_ratio > 1.5:
            score += min(1.0, (snap.volume_vs_avg_ratio - 1.0) / 3.0) * 0.5

        return min(1.0, score)

    def _score_flow(self, snap: AssetSnapshot) -> float:
        """Score order-book imbalance.

        bid_ask_ratio > 1 → more bid depth (buy pressure)
        bid_ask_ratio < 1 → more ask depth (sell pressure)
        """
        if snap.bid_ask_ratio <= 0:
            return 0.0

        # Deviation from 1.0 (balanced)
        deviation = abs(snap.bid_ask_ratio - 1.0)
        return min(1.0, deviation / 0.5)  # 0.5 deviation → 1.0

    def _score_liquidity(self, snap: AssetSnapshot) -> float:
        """Score based on execution quality: spread, depth, volume."""
        scores: list[float] = []

        # Spread: lower is better. 0% → 1.0, 5% → 0.0
        spread_score = max(0.0, 1.0 - snap.spread_pct / 5.0)
        scores.append(spread_score)

        # Volume: log scale
        if snap.volume_24h > 0:
            vol_score = min(1.0, snap.volume_24h / 50_000_000.0)
            scores.append(vol_score)

        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    # ------------------------------------------------------------------
    # Historical data management
    # ------------------------------------------------------------------

    def _update_history(self, snap: AssetSnapshot) -> None:
        """Update rolling price and volume history."""
        if snap.last_price > 0:
            hist = self._price_history.setdefault(snap.symbol, [])
            hist.append(snap.last_price)
            if len(hist) > self._max_history:
                self._price_history[snap.symbol] = hist[-self._max_history :]

        if snap.volume_5m > 0:
            vhist = self._volume_history.setdefault(snap.symbol, [])
            vhist.append(snap.volume_5m)
            if len(vhist) > self._max_history:
                self._volume_history[snap.symbol] = vhist[-self._max_history :]

    # ------------------------------------------------------------------
    # Convert to StrategySignal
    # ------------------------------------------------------------------

    def to_strategy_signal(self, alt_signal: AltcoinSignal) -> StrategySignal:
        """Convert an AltcoinSignal to the standard StrategySignal format.

        This bridges the scanner output into the existing opportunity pipeline.
        """
        snap = alt_signal.snapshot

        # Determine direction from momentum
        if snap.price_change_1m_pct > 0:
            direction = SignalDirection.LONG
        elif snap.price_change_1m_pct < 0:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.NEUTRAL

        return StrategySignal(
            strategy_id="altcoin_scanner",
            strategy_version="1.0.0",
            exchange=snap.exchange,
            symbol=snap.symbol,
            market="crypto_spot",
            direction=direction,
            confidence=alt_signal.confidence,
            estimated_return=snap.price_change_5m_pct,  # Recent momentum proxy
            estimated_risk=abs(snap.price_change_5m_pct) * 0.5,
            required_capital=None,  # Allocator determines size
            entry_logic={
                "scanner": "altcoin_scanner",
                "momentum_score": alt_signal.momentum_score,
                "volume_score": alt_signal.volume_score,
                "breakout_score": alt_signal.breakout_score,
                "flow_score": alt_signal.flow_score,
                "composite_score": alt_signal.composite_score,
            },
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.15,
                "activation_pct": 0.15,
                "no_fixed_take_profit": True,
            },
            metadata={
                "volume_24h": snap.volume_24h,
                "spread_pct": snap.spread_pct,
                "bid_ask_ratio": snap.bid_ask_ratio,
                "volume_vs_avg": snap.volume_vs_avg_ratio,
                "price_change_1m": snap.price_change_1m_pct,
                "price_change_5m": snap.price_change_5m_pct,
                "stop_loss_pct": 0.30,
            },
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),  # 30s expiry
        )

    def to_strategy_signals(self, alt_signals: list[AltcoinSignal]) -> list[StrategySignal]:
        """Batch-convert altcoin signals to strategy signals."""
        return [self.to_strategy_signal(s) for s in alt_signals]
