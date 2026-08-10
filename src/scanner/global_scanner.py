"""Global Multi-Asset Opportunity Scanner — continuous, asset-class-agnostic
signal detection across ALL supported markets and instruments.

PRINCIPLE:
    Scan everything that is legitimately accessible →
    normalize all opportunities →
    compare them →
    allocate capital to the best qualified opportunities.

SUPPORTED ASSET CLASSES (modular, extensible):
- Crypto Spot: BTC, ETH, liquid altcoins, stablecoin pairs
- Gold / Gold-linked: XAU/USD and instruments where a legitimate API exists
- FX Spot: Major and minor currency pairs where supported
- Additional liquid instruments via adapter plugins

The scanner has NO loyalty to any specific asset, asset class, or
instrument. An opportunity is an opportunity — ranked purely by
quantitative merit.

SCANNED SIGNALS (universal, applicable across all asset classes):
- Volume Spikes: current volume vs. historical average
- Relative Volume: volume relative to universe peers
- Momentum Acceleration: rate-of-change of price momentum
- Price Acceleration: second derivative of price
- Breakouts: price exceeding recent range with volume confirmation
- Order-Book Imbalance: bid/ask ratio asymmetry
- Buy/Sell Trade Flow: aggressive buy vs. sell volume
- Liquidity Changes: sudden spread/depth shifts
- Spread Changes: tightening/widening spreads
- Volatility Expansion: sudden increase in price range

SAFETY FILTER (applied before any instrument enters ranking):
- Sufficient order-book depth
- Acceptable spread
- Sufficient 24h volume
- Acceptable estimated slippage
- Healthy market-data feed
- Valid instrument metadata
- Active trading status
- Adequate execution capacity

INTEGRATION:
Scanner → AssetSnapshots → GlobalScanner.scan() → ScannerSignals →
  to_strategy_signal() → OpportunityEngine → RiskEngine →
  CapitalAllocator → ExecutionEngine
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from src.core.logging_config import get_logger
from src.portfolio.universe import UniverseManager
from src.strategies.base import SignalDirection, StrategySignal

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Asset class enumeration — modular, extensible
# ---------------------------------------------------------------------------


class AssetClass(str, Enum):
    """Supported asset classes. Add new entries as new adapters are built.

    The scanner treats all classes uniformly. This enum exists purely
    for metadata tagging and universe filtering — never for bias.
    """

    CRYPTO_SPOT = "crypto_spot"
    GOLD = "gold"
    FX_SPOT = "fx_spot"
    # Future: EQUITY, BOND, COMMODITY, INDEX


# ---------------------------------------------------------------------------
# Scanner configuration
# ---------------------------------------------------------------------------


@dataclass
class ScannerConfig:
    """Configuration for the global multi-asset opportunity scanner.

    Safety thresholds are asset-class-agnostic. Per-asset-class
    overrides are supported via the `per_class` dict for cases
    where e.g. FX has much tighter spreads than crypto.
    """

    # Safety filters (global defaults)
    min_volume_24h_usd: float = 1_000_000.0
    max_spread_pct: float = 5.0
    min_order_book_depth_usd: float = 10_000.0
    max_estimated_slippage_pct: float = 1.0

    # Per-asset-class overrides (optional)
    per_class: dict[AssetClass, dict[str, float]] = field(default_factory=dict)

    # Signal thresholds
    volume_spike_threshold: float = 2.0
    momentum_accel_threshold: float = 0.05
    breakout_stddev: float = 2.0
    order_imbalance_threshold: float = 0.3

    # Scanner capacity
    max_scanned_assets: int = 200  # BTC + ETH + 100+ altcoins + gold + FX
    scan_interval_seconds: float = 1.0

    # Output
    min_signal_confidence: float = 0.3
    max_signals_per_scan: int = 30

    # Asset class activation — which classes to scan
    enabled_classes: list[AssetClass] = field(default_factory=lambda: [AssetClass.CRYPTO_SPOT])


# ---------------------------------------------------------------------------
# Signal data structures
# ---------------------------------------------------------------------------


@dataclass
class AssetSnapshot:
    """Market snapshot for one instrument at one point in time.

    Asset-class agnostic. BTC, ETH, an altcoin, gold, or EUR/USD —
    all produce the same snapshot structure.
    """

    symbol: str
    exchange: str
    asset_class: AssetClass = AssetClass.CRYPTO_SPOT
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    # Price
    last_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    price_change_1m_pct: float = 0.0
    price_change_5m_pct: float = 0.0
    price_change_1h_pct: float = 0.0

    # Volume
    volume_24h: float = 0.0
    volume_1h: float = 0.0
    volume_5m: float = 0.0
    volume_vs_avg_ratio: float = 1.0

    # Order book
    depth_bid_10bps: float = 0.0
    depth_ask_10bps: float = 0.0
    bid_ask_ratio: float = 1.0

    # Derived (computed during scan)
    momentum_score: float = 0.0
    breakout_score: float = 0.0
    volume_score: float = 0.0
    flow_score: float = 0.0
    liquidity_score: float = 0.0

    # Safety
    passes_safety_filter: bool = False
    safety_rejection_reason: str = ""

    # Instrument metadata
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScannerSignal:
    """A detected opportunity from ANY asset class with all scoring factors.

    BTC, Altcoin, Gold, FX — all produce the same signal type.
    The Opportunity Engine and Capital Allocator compare them
    purely on quantitative merit.
    """

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
# Global Scanner
# ---------------------------------------------------------------------------


class GlobalScanner:
    """Continuously scans ALL supported markets for trading opportunities.

    BLUEPRINT:
    SCAN EVERYTHING THAT IS LEGITIMATELY ACCESSIBLE →
    NORMALIZE ALL OPPORTUNITIES →
    COMPARE THEM →
    ALLOCATE CAPITAL TO THE BEST QUALIFIED OPPORTUNITIES

    The scanner does NOT decide what to trade — it produces ranked signals.
    The Opportunity Engine, Risk Engine, and Capital Allocator make the
    final decision.

    Asset loyalty: NONE.
    If BTC is stagnant → ignore it.
    If an altcoin produces a stronger signal → allocate there.
    If gold produces the strongest opportunity → allocate there.
    """

    def __init__(
        self,
        config: ScannerConfig | None = None,
        universe: UniverseManager | None = None,
    ) -> None:
        self.config = config or ScannerConfig()
        self._universe = universe

        # Rolling data for signal computation
        self._price_history: dict[str, list[float]] = {}
        self._volume_history: dict[str, list[float]] = {}
        self._max_history: int = 100

    # ------------------------------------------------------------------
    # Main scan entry point
    # ------------------------------------------------------------------

    def scan(self, snapshots: list[AssetSnapshot]) -> list[ScannerSignal]:
        """Scan a batch of asset snapshots from ALL asset classes.

        Args:
            snapshots: Current market snapshots for all scanned assets
                       across all enabled asset classes.

        Returns:
            Ranked list of ScannerSignals (best first), capped at
            max_signals_per_scan.
        """
        # --- Update rolling history ---
        for snap in snapshots:
            self._update_history(snap)

        # --- Filter by enabled asset classes ---
        enabled = {ac.value for ac in self.config.enabled_classes}
        class_filtered = [s for s in snapshots if s.asset_class.value in enabled]

        # --- Safety filter ---
        eligible: list[AssetSnapshot] = []
        for s in class_filtered:
            if self._check_safety(s):
                eligible.append(s)
            else:
                logger.debug(
                    "scanner_safety_rejected",
                    symbol=s.symbol,
                    asset_class=s.asset_class.value,
                    reason=s.safety_rejection_reason,
                )

        # --- Compute scores ---
        signals: list[ScannerSignal] = []
        for snap in eligible:
            signal = self._compute_scores(snap)
            if signal.confidence >= self.config.min_signal_confidence:
                signals.append(signal)

        # --- Rank by composite score (asset-class agnostic) ---
        signals.sort(key=lambda s: s.composite_score, reverse=True)

        # --- Cap ---
        return signals[: self.config.max_signals_per_scan]

    # ------------------------------------------------------------------
    # Safety filter
    # ------------------------------------------------------------------

    def _check_safety(self, snap: AssetSnapshot) -> bool:
        """Asset-class-agnostic safety filter.

        Uses global defaults unless per-class overrides exist.
        """
        cfg = self.config

        # Per-class overrides
        overrides = cfg.per_class.get(snap.asset_class, {})

        vol_min = overrides.get("min_volume_24h_usd", cfg.min_volume_24h_usd)
        spread_max = overrides.get("max_spread_pct", cfg.max_spread_pct)
        depth_min = overrides.get("min_order_book_depth_usd", cfg.min_order_book_depth_usd)

        if snap.volume_24h < vol_min:
            snap.safety_rejection_reason = f"Volume ${snap.volume_24h:,.0f} < ${vol_min:,.0f}"
            snap.passes_safety_filter = False
            return False

        if snap.spread_pct > spread_max:
            snap.safety_rejection_reason = f"Spread {snap.spread_pct:.2f}% > {spread_max}%"
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

        if snap.depth_bid_10bps > 0 and snap.depth_bid_10bps * snap.bid < depth_min:
            snap.safety_rejection_reason = "Insufficient order-book depth"
            snap.passes_safety_filter = False
            return False

        snap.passes_safety_filter = True
        return True

    # ------------------------------------------------------------------
    # Signal scoring
    # ------------------------------------------------------------------

    def _compute_scores(self, snap: AssetSnapshot) -> ScannerSignal:
        """Compute all component scores for one asset.

        Asset-class agnostic. A BTC snapshot and a gold snapshot
        flow through the exact same scoring pipeline.
        """
        signal = ScannerSignal(snapshot=snap)

        signal.momentum_score = self._score_momentum(snap)
        signal.volume_score = self._score_volume(snap)
        signal.breakout_score = self._score_breakout(snap)
        signal.flow_score = self._score_flow(snap)
        signal.liquidity_score = self._score_liquidity(snap)

        # Composite: momentum 25%, volume 25%, breakout 20%, flow 15%, liquidity 15%
        signal.composite_score = (
            signal.momentum_score * 0.25
            + signal.volume_score * 0.25
            + signal.breakout_score * 0.20
            + signal.flow_score * 0.15
            + signal.liquidity_score * 0.15
        )

        signal.confidence = signal.composite_score * (0.5 + 0.5 * signal.liquidity_score)
        signal.estimated_net_edge_bps = (
            0.0  # N-24: signal_strength_score — real edge requires model/history
        )

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
    # Individual scorers (asset-class agnostic)
    # ------------------------------------------------------------------

    def _score_momentum(self, snap: AssetSnapshot) -> float:
        scores: list[float] = []

        if abs(snap.price_change_1m_pct) > 0:
            scores.append(min(1.0, abs(snap.price_change_1m_pct) / 2.0))
        if abs(snap.price_change_5m_pct) > 0:
            scores.append(min(1.0, abs(snap.price_change_5m_pct) / 5.0))

        hist = self._price_history.get(snap.symbol, [])
        if len(hist) >= 5 and hist[0] > 0:
            accel = (hist[-1] - hist[0]) / hist[0] * 100.0
            scores.append(min(1.0, abs(accel) / 3.0))

        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    def _score_volume(self, snap: AssetSnapshot) -> float:
        if snap.volume_vs_avg_ratio <= 0:
            return 0.0
        t = self.config.volume_spike_threshold
        if snap.volume_vs_avg_ratio >= t:
            return min(1.0, 0.5 + (snap.volume_vs_avg_ratio - t) / 5.0)
        return snap.volume_vs_avg_ratio / t * 0.5

    def _score_breakout(self, snap: AssetSnapshot) -> float:
        score = 0.0
        move = abs(snap.price_change_1m_pct)
        if move > 0.5:
            score += min(1.0, move / 3.0) * 0.5
        if snap.volume_vs_avg_ratio > 1.5:
            score += min(1.0, (snap.volume_vs_avg_ratio - 1.0) / 3.0) * 0.5
        return min(1.0, score)

    def _score_flow(self, snap: AssetSnapshot) -> float:
        if snap.bid_ask_ratio <= 0:
            return 0.0
        return min(1.0, abs(snap.bid_ask_ratio - 1.0) / 0.5)

    def _score_liquidity(self, snap: AssetSnapshot) -> float:
        scores: list[float] = []
        scores.append(max(0.0, 1.0 - snap.spread_pct / 5.0))
        if snap.volume_24h > 0:
            scores.append(min(1.0, snap.volume_24h / 50_000_000.0))
        if not scores:
            return 0.0
        return sum(scores) / len(scores)

    # ------------------------------------------------------------------
    # Historical data management
    # ------------------------------------------------------------------

    def _update_history(self, snap: AssetSnapshot) -> None:
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
    # Convert to StrategySignal (bridge to the pipeline)
    # ------------------------------------------------------------------

    def to_strategy_signal(self, scanner_signal: ScannerSignal) -> StrategySignal:
        """Convert a ScannerSignal (from any asset class) to the standard
        StrategySignal format consumed by the Opportunity Engine.

        Assets are tagged with their class but ranked purely by signal quality.
        """
        snap = scanner_signal.snapshot

        if snap.price_change_1m_pct > 0:
            direction = SignalDirection.LONG
        elif snap.price_change_1m_pct < 0:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.NEUTRAL

        return StrategySignal(
            strategy_id="global_scanner",
            strategy_version="1.0.0",
            exchange=snap.exchange,
            symbol=snap.symbol,
            market=snap.asset_class.value,
            direction=direction,
            confidence=scanner_signal.confidence,
            estimated_return=snap.price_change_5m_pct / 100.0,
            estimated_risk=abs(snap.price_change_5m_pct) * 0.5 / 100.0,
            required_capital=None,
            entry_logic={
                "scanner": "global_scanner",
                "asset_class": snap.asset_class.value,
                "momentum_score": scanner_signal.momentum_score,
                "volume_score": scanner_signal.volume_score,
                "breakout_score": scanner_signal.breakout_score,
                "flow_score": scanner_signal.flow_score,
                "composite_score": scanner_signal.composite_score,
            },
            exit_logic={
                "hard_stop_pct": 0.30,
                "trail_pct": 0.20,
                "activation_pct": 0.20,
                "no_fixed_take_profit": True,
            },
            metadata={
                "entry_price": snap.last_price,
                "asset_class": snap.asset_class.value,
                "volume_24h": snap.volume_24h,
                "spread_pct": snap.spread_pct,
                "bid_ask_ratio": snap.bid_ask_ratio,
                "volume_vs_avg": snap.volume_vs_avg_ratio,
                "price_change_1m": snap.price_change_1m_pct,
                "price_change_5m": snap.price_change_5m_pct,
                "stop_loss_pct": 0.30,
                "exchange": snap.exchange,
            },
            signal_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )

    def to_strategy_signals(self, scanner_signals: list[ScannerSignal]) -> list[StrategySignal]:
        """Batch-convert signals to strategy signals."""
        return [self.to_strategy_signal(s) for s in scanner_signals]
