"""Order State Machine — deterministic finite state machine for order lifecycle.

States: PENDING → OPEN → PARTIALLY_FILLED → FILLED
                                    ↓
                              CANCELED / REJECTED / EXPIRED

Every state transition is validated. Invalid transitions raise errors.
This is the single source of truth for what transitions are legal.
"""

from __future__ import annotations

from typing import ClassVar

from src.adapters.base import OrderState
from src.core.exceptions import ExecutionError


class OrderStateMachine:
    """Deterministic FSM for order lifecycle.

    Valid transitions:
    - PENDING → OPEN, REJECTED
    - OPEN → PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
    - PARTIALLY_FILLED → PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED
    - FILLED → (terminal)
    - CANCELED → (terminal)
    - REJECTED → (terminal)
    - EXPIRED → (terminal)
    """

    VALID_TRANSITIONS: ClassVar[dict[OrderState, set[OrderState]]] = {
        OrderState.PENDING: {OrderState.OPEN, OrderState.REJECTED},
        OrderState.OPEN: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        },
        OrderState.PARTIALLY_FILLED: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        },
        OrderState.FILLED: set(),
        OrderState.CANCELED: set(),
        OrderState.REJECTED: set(),
        OrderState.EXPIRED: set(),
    }

    TERMINAL_STATES: ClassVar[set[OrderState]] = {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }

    @classmethod
    def can_transition(cls, from_state: OrderState, to_state: OrderState) -> bool:
        """Check if a transition is legal."""
        return to_state in cls.VALID_TRANSITIONS.get(from_state, set())

    @classmethod
    def is_terminal(cls, state: OrderState) -> bool:
        """Check if a state is terminal (no further transitions)."""
        return state in cls.TERMINAL_STATES

    @classmethod
    def is_active(cls, state: OrderState) -> bool:
        """Check if an order is still active on the book."""
        return state in {OrderState.OPEN, OrderState.PARTIALLY_FILLED}

    @classmethod
    def transition(cls, from_state: OrderState, to_state: OrderState, order_id: str = "") -> OrderState:
        """Validate and execute a state transition.

        Raises:
            ExecutionError: If the transition is invalid.
        """
        if not cls.can_transition(from_state, to_state):
            raise ExecutionError(
                f"Invalid order state transition: {from_state.value} → {to_state.value} "
                f"(order_id={order_id})"
            )
        return to_state

    @classmethod
    def apply_fill(cls, state: OrderState, total_qty: float, filled_qty: float) -> OrderState:
        """Determine new state after a fill event."""
        if filled_qty >= total_qty:
            return OrderState.FILLED
        if filled_qty > 0:
            return OrderState.PARTIALLY_FILLED
        return state
