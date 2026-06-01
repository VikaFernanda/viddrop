"""Shared pytest fixtures for Viddrop tests."""

from __future__ import annotations

import pytest

from viddrop.core.database import DatabaseManager
from viddrop.core.queue_manager import QueueManager


@pytest.fixture
def tmp_db(tmp_path):
    """An open DatabaseManager backed by a temporary SQLite file."""
    db = DatabaseManager()
    db.DB_DIR = tmp_path
    db.DB_PATH = tmp_path / "test.db"
    db.open()
    yield db
    db.close()


@pytest.fixture
def queue(qtbot, tmp_db):
    """A QueueManager wired to the temporary database."""
    return QueueManager(db=tmp_db)
