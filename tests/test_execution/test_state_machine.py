"""Tests for the Order State Machine — must be deterministic."""

from __future__ import annotations

import pytest

from src.core.exceptions import ExecutionError
from src.execution.state_machine import OrderState, OrderStateMachine


class TestValidTransitions:
    """All legal state transitions."""

    def test_pending_to_open(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.PENDING, OrderState.OPEN)

    def test_pending_to_rejected(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.PENDING, OrderState.REJECTED)

    def test_open_to_partially_filled(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.OPEN, OrderState.PARTIALLY_FILLED)

    def test_open_to_filled(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.OPEN, OrderState.FILLED)

    def test_open_to_canceled(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.OPEN, OrderState.CANCELED)

    def test_open_to_expired(self) -> None:
        assert OrderStateMachine.can_transition(OrderState.OPEN, OrderState.EXPIRED)

    def test_partially_filled_to_filled(self) -> None:
        assert OrderStateMachine.can_transition(
            OrderState.PARTIALLY_FILLED, OrderState.FILLED
        )

    def test_partially_filled_to_canceled(self) -> None:
        assert OrderStateMachine.can_transition(
            OrderState.PARTIALLY_FILLED, OrderState.CANCELED
        )

    def test_partially_filled_to_more_partial(self) -> None:
        """Partial fill → partial fill is valid (more quantity fills)."""
        assert OrderStateMachine.can_transition(
            OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED
        )


class TestInvalidTransitions:
    """All illegal state transitions must be rejected."""

    def test_filled_is_terminal(self) -> None:
        for target in OrderState:
            if target != OrderState.FILLED:
                assert not OrderStateMachine.can_transition(OrderState.FILLED, target)

    def test_canceled_is_terminal(self) -> None:
        for target in OrderState:
            if target != OrderState.CANCELED:
                assert not OrderStateMachine.can_transition(OrderState.CANCELED, target)

    def test_rejected_is_terminal(self) -> None:
        for target in OrderState:
            if target != OrderState.REJECTED:
                assert not OrderStateMachine.can_transition(OrderState.REJECTED, target)

    def test_pending_cannot_go_to_filled(self) -> None:
        assert not OrderStateMachine.can_transition(OrderState.PENDING, OrderState.FILLED)

    def test_open_cannot_go_to_pending(self) -> None:
        assert not OrderStateMachine.can_transition(OrderState.OPEN, OrderState.PENDING)

    def test_invalid_transition_raises(self) -> None:
        with pytest.raises(ExecutionError, match="Invalid order state transition"):
            OrderStateMachine.transition(OrderState.FILLED, OrderState.OPEN)


class TestTerminalStates:
    def test_terminal_states(self) -> None:
        assert OrderStateMachine.is_terminal(OrderState.FILLED)
        assert OrderStateMachine.is_terminal(OrderState.CANCELED)
        assert OrderStateMachine.is_terminal(OrderState.REJECTED)
        assert OrderStateMachine.is_terminal(OrderState.EXPIRED)
        assert not OrderStateMachine.is_terminal(OrderState.PENDING)
        assert not OrderStateMachine.is_terminal(OrderState.OPEN)
        assert not OrderStateMachine.is_terminal(OrderState.PARTIALLY_FILLED)

    def test_active_states(self) -> None:
        assert OrderStateMachine.is_active(OrderState.OPEN)
        assert OrderStateMachine.is_active(OrderState.PARTIALLY_FILLED)
        assert not OrderStateMachine.is_active(OrderState.PENDING)
        assert not OrderStateMachine.is_active(OrderState.FILLED)


class TestFillApplication:
    def test_full_fill(self) -> None:
        result = OrderStateMachine.apply_fill(OrderState.OPEN, 10.0, 10.0)
        assert result == OrderState.FILLED

    def test_partial_fill(self) -> None:
        result = OrderStateMachine.apply_fill(OrderState.OPEN, 10.0, 3.0)
        assert result == OrderState.PARTIALLY_FILLED

    def test_zero_fill_no_change(self) -> None:
        result = OrderStateMachine.apply_fill(OrderState.OPEN, 10.0, 0.0)
        assert result == OrderState.OPEN

    def test_overfill_still_filled(self) -> None:
        """Defensive: overfill still results in FILLED."""
        result = OrderStateMachine.apply_fill(OrderState.PARTIALLY_FILLED, 10.0, 12.0)
        assert result == OrderState.FILLED
