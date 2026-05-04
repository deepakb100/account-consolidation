"""Shared pytest fixtures.

Each test gets a fresh on-disk SQLite at a temp path with the schema applied.
On-disk (not :memory:) because we test WAL-mode concurrent access.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make project root importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db import init_schema  # noqa: E402  (after sys.path manipulation)


@pytest.fixture
def tmp_db(tmp_path) -> Path:
    """Fresh DB per test. Returns the path."""
    db = tmp_path / "test.db"
    init_schema(db)
    return db


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
