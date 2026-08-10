#!/usr/bin/env python3
"""Initialize database tables from ORM models.

Usage:
    python scripts/init_db.py

Requires DATABASE_URL_SYNC to be set (env or .env).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sqlalchemy import create_engine

from src.core.config import get_settings
from src.db.models import Base


def init_db() -> None:
    """Create all tables defined in the ORM."""
    settings = get_settings()
    url = settings.database.url_sync
    print(f"Initializing database at: {url.split('@')[-1] if '@' in url else url}")

    engine = create_engine(url, echo=False)
    Base.metadata.create_all(engine)
    engine.dispose()
    print("Database tables created successfully.")


if __name__ == "__main__":
    init_db()
