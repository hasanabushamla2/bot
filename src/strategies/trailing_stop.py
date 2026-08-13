"""Trailing Profit Protection — dynamic exit management for winning positions.

The system uses trailing stops instead of fixed take-profit levels.
As price moves favorably, the trailing exit level follows at a
configurable distance, protecting progressively more profit while
allowing the position to capture extended moves.

SUPPORTED TRAILING ALGORITHMS (FINAL: trailing_delta=0.002=0.20%):
- percentage_trail: Fixed percentage distance from peak
- atr_trail: Distance based on Average True Range (volatility-aware)
- step_trail: Discrete steps triggered at profit milestones

KEY PRINCIPLE:
A winning position should continue as long as its trailing conditions
remain valid. There is NO fixed profit ceiling. The architecture
permits capturing +2%, +5%, +10%, +20%, or higher when the market
genuinely provides them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class TrailAlgorithm(str, Enum):
    PERCENTAGE = "percentage"
    ATR = "atr"
    STEP = "step"


class TrailDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class TrailConfig:
    """Configuration for one trailing stop instance."""

    algorithm: TrailAlgorithm = TrailAlgorithm.PERCENTAGE
    trail_pct: float = 0.20  # Distance from peak (0.20% trail floor)
    activation_pct: float = 0.20  # Profit needed before trail activates (floor)

    # ATR parameters
    atr_period: int = 14
    atr_multiplier: float = 1.5

    # Step parameters
    step_size_pct: float = 0.5  # Each step size
    step_count: int = 3  # Max number of steps

    # Never set a fixed ceiling
    enable_fixed_take_profit: bool = False  # MUST be False per policy
    trailing_delta: float = 0.002  # Retracement fraction (0.002 = 0.20%)


@dataclass
class TrailState:
    """Current state of a trailing stop for one position."""

    symbol: str = ""
    direction: TrailDirection = TrailDirection.LONG
    entry_price: float = 0.0
    current_price: float = 0.0
    peak_price: float = 0.0  # Best price achieved since entry
    peak_time: datetime | None = None
    trail_level: float = 0.0  # Current exit trigger price
    activated: bool = False  # Has trail been activated?
    activation_price: float = 0.0  # Price at which trail activates
    activation_pct: float = 0.0  # Effective activation, including the fee-aware floor
    trail_distance_pct: float = 0.0  # Effective per-position distance from peak

    # Metrics (recorded at exit)
    entry_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    exit_price: float | None = None
    exit_time: datetime | None = None
    exit_reason: str = ""  # "trail_hit", "stop_loss", "manual", "eod"
    captured_pct: float = 0.0  # Actual captured return %

    # Tracking
    current_unrealized_pct: float = 0.0
    protected_profit_pct: float = 0.0
    trail_updates: int = 0

    @property
    def is_active(self) -> bool:
        return self.exit_price is None


@dataclass(frozen=True)
class VolatilityAwareTrailParameters:
    """Effective per-position trail settings derived from live market noise."""

    trail_distance_pct: float
    activation_pct: float
    volatility_component_pct: float
    spread_component_pct: float


def compute_volatility_aware_trail(
    *,
    base_trail_distance_pct: float,
    base_activation_pct: float,
    volatility_pct: float,
    spread_bps: float,
    round_trip_cost_fraction: float,
    volatility_multiplier: float = 1.5,
    spread_multiplier: float = 2.0,
    activation_volatility_multiplier: float = 1.25,
    max_trail_distance_pct: float = 1.25,
) -> VolatilityAwareTrailParameters:
    """Make a trail wide enough for current noise while retaining a hard cap.

    ``volatility_pct`` and the result are percent units (``0.30`` means
    0.30%).  Costs use decimal-fraction units.  The function raises neither
    stops nor position sizing; it only prevents an otherwise healthy winner
    from being closed by a normal volatile retracement.
    """
    safe_base_distance = max(0.0, base_trail_distance_pct)
    safe_base_activation = max(0.0, base_activation_pct)
    vol_component = max(0.0, volatility_pct) * max(0.0, volatility_multiplier)
    spread_component = max(0.0, spread_bps) / 100.0 * max(0.0, spread_multiplier)
    raw_distance = max(safe_base_distance, vol_component, spread_component)
    effective_distance = min(max(0.0, max_trail_distance_pct), raw_distance)

    # The activation floor protects the economics of the trail.  The exact
    # post-cost breakeven price is calculated by the caller against its fill
    # model; this component ensures volatile markets do not activate too soon.
    cost_component = max(0.0, round_trip_cost_fraction) * 100.0
    effective_activation = max(
        safe_base_activation,
        cost_component,
        max(0.0, volatility_pct) * max(0.0, activation_volatility_multiplier),
        spread_component,
    )
    return VolatilityAwareTrailParameters(
        trail_distance_pct=effective_distance,
        activation_pct=effective_activation,
        volatility_component_pct=vol_component,
        spread_component_pct=spread_component,
    )


class TrailingStopManager:
    """Manages trailing stops for open positions.

    Update cycle:
    1. Feed new price → update peak, compute trail level.
    2. Check if trail_level is triggered → signal exit.
    3. If not triggered → position continues.
    """

    def __init__(self, config: TrailConfig | None = None) -> None:
        self.config = config or TrailConfig()

        # ATR tracking for ATR-based trails
        self._atr_values: dict[str, list[float]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(
        self,
        symbol: str,
        direction: TrailDirection,
        entry_price: float,
        entry_time: datetime | None = None,
        activation_pct: float | None = None,
        trail_distance_pct: float | None = None,
    ) -> TrailState:
        """Create a new trail state for an opening position.

        The trail does NOT activate immediately. It activates only
        after price reaches `activation_pct` beyond entry in the
        favorable direction.
        """
        cfg = self.config
        effective_activation_pct = (
            cfg.activation_pct if activation_pct is None else max(0.0, activation_pct)
        )
        effective_trail_distance_pct = (
            cfg.trail_pct if trail_distance_pct is None else max(0.0, trail_distance_pct)
        )
        state = TrailState(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            current_price=entry_price,
            peak_price=entry_price,
            trail_level=entry_price,  # No trail until activated
            entry_time=entry_time or datetime.now(UTC),
            activation_pct=effective_activation_pct,
            trail_distance_pct=effective_trail_distance_pct,
        )

        # Compute activation price.  PositionMonitor may provide a higher,
        # cost-derived floor so an activated trail protects net profit rather
        # than acting as a second loss stop.
        if direction == TrailDirection.LONG:
            state.activation_price = entry_price * (1.0 + effective_activation_pct / 100.0)
        else:
            state.activation_price = entry_price * (1.0 - effective_activation_pct / 100.0)

        return state

    # ------------------------------------------------------------------
    # Update — called on each new price tick
    # ------------------------------------------------------------------

    def update(self, state: TrailState, new_price: float) -> TrailState:
        """Update trail state with a new price. Returns updated state.

        The trail updates in-place and returns the same object.
        """
        state.current_price = new_price

        # --- Update peak ---
        is_new_peak = False
        if state.direction == TrailDirection.LONG:
            if new_price > state.peak_price:
                state.peak_price = new_price
                state.peak_time = datetime.now(UTC)
                is_new_peak = True
        else:
            if new_price < state.peak_price:
                state.peak_price = new_price
                state.peak_time = datetime.now(UTC)
                is_new_peak = True

        # --- Compute unrealized P&L ---
        if state.entry_price > 0:
            if state.direction == TrailDirection.LONG:
                state.current_unrealized_pct = (
                    (new_price - state.entry_price) / state.entry_price * 100.0
                )
            else:
                state.current_unrealized_pct = (
                    (state.entry_price - new_price) / state.entry_price * 100.0
                )

        # --- Activation check ---
        if not state.activated:
            if state.direction == TrailDirection.LONG:
                if new_price >= state.activation_price:
                    state.activated = True
            else:
                if new_price <= state.activation_price:
                    state.activated = True

        # --- Update trail level ---
        # The threshold ratchets only in the favorable direction.  Initializing
        # it at entry also prevents an activated trail from becoming a loss-side
        # stop; the independent hard stop owns that responsibility.
        if state.activated and (is_new_peak or state.trail_level == state.entry_price):
            candidate = self._compute_trail_level(state)
            if state.direction == TrailDirection.LONG:
                state.trail_level = max(state.trail_level, candidate)
            else:
                state.trail_level = min(state.trail_level, candidate)
            if is_new_peak:
                state.trail_updates += 1

        # --- Compute protected profit ---
        if state.entry_price > 0:
            if state.direction == TrailDirection.LONG:
                state.protected_profit_pct = (
                    (state.trail_level - state.entry_price) / state.entry_price * 100.0
                )
            else:
                state.protected_profit_pct = (
                    (state.entry_price - state.trail_level) / state.entry_price * 100.0
                )

        return state

    # ------------------------------------------------------------------
    # Check exit condition
    # ------------------------------------------------------------------

    def should_exit(self, state: TrailState) -> bool:
        """Check if the trailing stop has been triggered."""
        if not state.activated:
            return False

        if state.direction == TrailDirection.LONG:
            return state.current_price <= state.trail_level
        else:
            return state.current_price >= state.trail_level

    def exit(
        self,
        state: TrailState,
        exit_price: float,
        exit_time: datetime | None = None,
        reason: str = "trail_hit",
    ) -> TrailState:
        """Finalize a position exit. Records exit price and captured return."""
        state.exit_price = exit_price
        state.exit_time = exit_time or datetime.now(UTC)
        state.exit_reason = reason

        if state.entry_price > 0:
            if state.direction == TrailDirection.LONG:
                state.captured_pct = (exit_price - state.entry_price) / state.entry_price * 100.0
            else:
                state.captured_pct = (state.entry_price - exit_price) / state.entry_price * 100.0

        return state

    # ------------------------------------------------------------------
    # Trail level computation
    # ------------------------------------------------------------------

    def _compute_trail_level(self, state: TrailState) -> float:
        """Compute the current trail exit level based on the algorithm."""
        cfg = self.config

        if cfg.algorithm == TrailAlgorithm.ATR:
            return self._trail_atr(state)
        elif cfg.algorithm == TrailAlgorithm.STEP:
            return self._trail_step(state)
        else:
            return self._trail_percentage(state)

    def _trail_percentage(self, state: TrailState) -> float:
        """Percentage-based trail using the per-position effective distance."""
        pct = (
            state.trail_distance_pct / 100.0
            if state.trail_distance_pct > 0.0
            else self.config.trailing_delta
        )
        if state.direction == TrailDirection.LONG:
            return state.peak_price * (1.0 - pct)
        else:
            return state.peak_price * (1.0 + pct)

    def _trail_atr(self, state: TrailState) -> float:
        """ATR-based trail: peak - atr_multiplier * ATR."""
        # Simplified: use trail_pct as percentage of price if no ATR data
        return self._trail_percentage(state)

    def _trail_step(self, state: TrailState) -> float:
        """Step-based trail: locks profit in discrete increments."""
        cfg = self.config
        if state.entry_price <= 0:
            return state.peak_price

        profit_from_entry = abs(state.peak_price - state.entry_price) / state.entry_price
        steps_taken = int(profit_from_entry * 100.0 / cfg.step_size_pct)
        steps_taken = min(steps_taken, cfg.step_count)

        if steps_taken == 0:
            return state.entry_price

        locked_pct = steps_taken * cfg.step_size_pct / 100.0
        if state.direction == TrailDirection.LONG:
            return state.entry_price * (1.0 + locked_pct - cfg.trail_pct / 100.0)
        else:
            return state.entry_price * (1.0 - locked_pct + cfg.trail_pct / 100.0)

    # ------------------------------------------------------------------
    # ATR tracking for ATR-based trails
    # ------------------------------------------------------------------

    def record_atr(self, symbol: str, atr: float) -> None:
        """Record an ATR observation."""
        if symbol not in self._atr_values:
            self._atr_values[symbol] = []
        self._atr_values[symbol].append(atr)
        if len(self._atr_values[symbol]) > self.config.atr_period:
            self._atr_values[symbol] = self._atr_values[symbol][-self.config.atr_period :]

    def get_atr(self, symbol: str) -> float:
        """Get latest ATR for a symbol."""
        vals = self._atr_values.get(symbol, [])
        if not vals:
            return 0.0
        return sum(vals) / len(vals)


# ---------------------------------------------------------------------------
# Stop-loss / trail utility for the risk engine
# ---------------------------------------------------------------------------


def compute_hard_stop(
    entry_price: float,
    direction: str,  # "long" or "short"
    stop_loss_pct: float = 0.30,
) -> float:
    """Compute hard stop-loss price.

    FINAL CONFIGURATION: -0.30% per position.

    The system records TARGET STOP vs ACTUAL EXIT PRICE separately
    to account for fees, spread, slippage, and execution latency.
    """
    pct = stop_loss_pct / 100.0
    if direction == "long":
        return entry_price * (1.0 - pct)
    else:
        return entry_price * (1.0 + pct)


def compute_trail_config(
    trail_pct: float = 0.20,
    activation_pct: float = 0.20,
) -> TrailConfig:
    """Factory for the default trailing configuration.

    Trail distance of 0.20% means once profit exceeds 0.20%,
    the trailing stop follows at 0.20% below the peak.
    """
    return TrailConfig(
        algorithm=TrailAlgorithm.PERCENTAGE,
        trail_pct=trail_pct,
        activation_pct=activation_pct,
        trailing_delta=trail_pct / 100.0,
        enable_fixed_take_profit=False,  # PERMANENTLY DISABLED
    )
