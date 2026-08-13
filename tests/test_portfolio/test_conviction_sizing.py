"""Tests for bounded high-conviction sizing."""

from __future__ import annotations

import pytest

from src.portfolio.conviction_sizing import ConvictionSizer, ConvictionSizingConfig


def test_base_size_remains_for_any_marginal_component() -> None:
    sizer = ConvictionSizer()
    decision = sizer.assess(
        confidence=0.90,
        entry_quality_score=0.86,
        expected_net_edge_fraction=0.0014,
    )
    assert decision.multiplier == 1.0
    assert not decision.is_high_conviction
    assert decision.reason == "base_size_insufficient_independent_conviction"


def test_high_conviction_can_use_existing_four_times_base_slice() -> None:
    sizer = ConvictionSizer()
    decision = sizer.assess(
        confidence=0.92,
        entry_quality_score=0.90,
        expected_net_edge_fraction=0.005,
    )
    assert decision.is_high_conviction
    assert decision.multiplier == pytest.approx(4.0)
    assert decision.confidence_component == 1.0
    assert decision.quality_component == 1.0
    assert decision.edge_component == 1.0


def test_sizing_interpolates_and_never_exceeds_configured_multiplier() -> None:
    sizer = ConvictionSizer(ConvictionSizingConfig(max_multiplier=3.0))
    decision = sizer.assess(
        confidence=0.81,
        entry_quality_score=0.775,
        expected_net_edge_fraction=0.00275,
    )
    assert 1.0 < decision.multiplier < 3.0


def test_disabled_sizing_never_changes_base_allocation() -> None:
    sizer = ConvictionSizer(ConvictionSizingConfig(enabled=False))
    decision = sizer.assess(
        confidence=1.0,
        entry_quality_score=1.0,
        expected_net_edge_fraction=1.0,
    )
    assert decision.multiplier == 1.0
    assert decision.reason == "disabled"
