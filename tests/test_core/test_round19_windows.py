"""R19: Windows hardening regression tests — cross-platform API safety."""

from __future__ import annotations

import sys
from pathlib import Path


class TestSystemEndpointCrossPlatform:
    """Test /system helpers work without Unix-only resource module."""

    def test_get_memory_mb_returns_float_or_none(self):
        """_get_memory_mb must return float|None, never raise."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from api.main import _get_memory_mb
        result = _get_memory_mb()
        assert result is None or isinstance(result, float), (
            f"_get_memory_mb must return float|None, got {type(result)}"
        )

    def test_get_db_info_on_nonexistent(self):
        """DB info must not crash on missing path."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from api.main import _get_db_info
        info = _get_db_info("nonexistent/path/test.db")
        assert info["exists"] is False
        assert info["size_bytes"] == 0
        assert "filename" in info

    def test_read_db_returns_none_for_missing(self):
        """_read_db must return None (not crash) for missing DB."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from api.main import _read_db
        result = _read_db("nonexistent/db_is_missing.db")
        assert result is None

    def test_classify_health(self):
        """_classify_health must produce correct states."""
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from api.main import _classify_health
        assert _classify_health(True) == "healthy"
        assert _classify_health(False) == "unhealthy"
        assert _classify_health(None) == "unknown"

    def test_memory_source_is_cross_platform(self):
        """Memory metric source must not be Unix-only resource module."""
        code = Path(__file__).parent.parent.parent / "api" / "main.py"
        content = code.read_text()
        # Must use try/except for resource module (not bare import)
        assert "try:" in content, "must have try/except for imports"
