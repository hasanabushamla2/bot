"""Shared test fixtures and configuration."""

from __future__ import annotations

import pytest


@pytest.fixture
def sample_config_dict() -> dict:
    return {
        "enabled": True,
        "version": "1.0.0",
    }
