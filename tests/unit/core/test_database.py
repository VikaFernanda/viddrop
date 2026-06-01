"""Unit tests for DatabaseManager."""

from __future__ import annotations

import sqlite3

import pytest

from viddrop.core.queue_manager import DownloadEntry


def _make_entry(**overrides) -> DownloadEntry:
    defaults: dict = {
        "id": "id-1",
        "url": "https://example.com/v",
        "title": "Title",
        "destination_path": "/home/user/v.mp4",
        "status": "queued",
        "progress_percent": 0,
        "error_message": None,
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": None,
        "completed_at": None,
    }
    defaults.update(overrides)
    return DownloadEntry(**defaults)


# ----------------------------------------------------------------------- #
# open()
# ----------------------------------------------------------------------- #


def test_open_creates_db_file(tmp_db):
    assert tmp_db.DB_PATH.exists()


def test_open_creates_downloads_table(tmp_db):
    row = tmp_db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='downloads'"
    ).fetchone()
    assert row is not None
    assert row["name"] == "downloads"


def test_open_twice_does_not_raise(tmp_db):
    # Re-opening on the same instance must not blow up (IF NOT EXISTS DDL).
    tmp_db.open()
    assert tmp_db.DB_PATH.exists()


# ----------------------------------------------------------------------- #
# insert_download + load_all
# ----------------------------------------------------------------------- #


def test_insert_and_load_single_entry_fields_match(tmp_db):
    entry = _make_entry(
        id="abc",
        url="https://host/x",
        title="My Title",
        destination_path="/home/user/x.mp4",
        status="queued",
        progress_percent=42,
        error_message=None,
        created_at="2026-02-02T10:00:00Z",
        started_at="2026-02-02T10:01:00Z",
        completed_at=None,
    )
    tmp_db.insert_download(entry)

    loaded = tmp_db.load_all()
    assert len(loaded) == 1
    got = loaded[0]
    assert got.id == "abc"
    assert got.url == "https://host/x"
    assert got.title == "My Title"
    assert got.destination_path == "/home/user/x.mp4"
    assert got.status == "queued"
    assert got.progress_percent == 42
    assert got.error_message is None
    assert got.created_at == "2026-02-02T10:00:00Z"
    assert got.started_at == "2026-02-02T10:01:00Z"
    assert got.completed_at is None


def test_load_all_orders_by_created_at_asc(tmp_db):
    later = _make_entry(id="later", created_at="2026-03-02T00:00:00Z")
    earlier = _make_entry(id="earlier", created_at="2026-03-01T00:00:00Z")
    # Insert out of order to prove ORDER BY does the work.
    tmp_db.insert_download(later)
    tmp_db.insert_download(earlier)

    loaded = tmp_db.load_all()
    assert [e.id for e in loaded] == ["earlier", "later"]


# ----------------------------------------------------------------------- #
# update_status
# ----------------------------------------------------------------------- #


def test_update_status_changes_status(tmp_db):
    tmp_db.insert_download(_make_entry(id="s", status="queued"))
    tmp_db.update_status("s", "in_progress")
    assert tmp_db.load_all()[0].status == "in_progress"


def test_update_status_leaves_timestamps_null_when_not_provided(tmp_db):
    tmp_db.insert_download(_make_entry(id="s", status="queued"))
    tmp_db.update_status("s", "paused")
    got = tmp_db.load_all()[0]
    assert got.started_at is None
    assert got.completed_at is None


def test_update_status_sets_started_at(tmp_db):
    tmp_db.insert_download(_make_entry(id="s", status="queued"))
    tmp_db.update_status("s", "in_progress", started_at="2026-04-01T00:00:00Z")
    got = tmp_db.load_all()[0]
    assert got.status == "in_progress"
    assert got.started_at == "2026-04-01T00:00:00Z"
    assert got.completed_at is None


def test_update_status_sets_completed_at(tmp_db):
    tmp_db.insert_download(_make_entry(id="s", status="in_progress"))
    tmp_db.update_status("s", "complete", completed_at="2026-04-02T00:00:00Z")
    got = tmp_db.load_all()[0]
    assert got.status == "complete"
    assert got.completed_at == "2026-04-02T00:00:00Z"


# ----------------------------------------------------------------------- #
# update_progress
# ----------------------------------------------------------------------- #


def test_update_progress_sets_value(tmp_db):
    tmp_db.insert_download(_make_entry(id="p", progress_percent=0))
    tmp_db.update_progress("p", 75)
    assert tmp_db.load_all()[0].progress_percent == 75


def test_update_progress_back_to_zero(tmp_db):
    tmp_db.insert_download(_make_entry(id="p", progress_percent=0))
    tmp_db.update_progress("p", 50)
    tmp_db.update_progress("p", 0)
    assert tmp_db.load_all()[0].progress_percent == 0


# ----------------------------------------------------------------------- #
# update_error
# ----------------------------------------------------------------------- #


def test_update_error_sets_status_and_message(tmp_db):
    tmp_db.insert_download(_make_entry(id="e", status="in_progress"))
    tmp_db.update_error("e", "error", "Something failed")
    got = tmp_db.load_all()[0]
    assert got.status == "error"
    assert got.error_message == "Something failed"


def test_update_error_with_single_quote_is_parameterized(tmp_db):
    tmp_db.insert_download(_make_entry(id="e", status="in_progress"))
    nasty = "can't connect'; DROP TABLE downloads; --"
    tmp_db.update_error("e", "error", nasty)
    got = tmp_db.load_all()[0]
    assert got.error_message == nasty
    # Table must still exist and hold the row.
    assert len(tmp_db.load_all()) == 1


# ----------------------------------------------------------------------- #
# delete_download
# ----------------------------------------------------------------------- #


def test_delete_removes_row(tmp_db):
    tmp_db.insert_download(_make_entry(id="d"))
    tmp_db.delete_download("d")
    assert tmp_db.load_all() == []


def test_delete_nonexistent_id_does_not_raise(tmp_db):
    tmp_db.delete_download("ghost")
    assert tmp_db.load_all() == []


# ----------------------------------------------------------------------- #
# guard: operations require an open connection
# ----------------------------------------------------------------------- #


def test_operations_require_open_connection(tmp_db):
    tmp_db.close()
    with pytest.raises(RuntimeError):
        tmp_db.load_all()


def test_row_factory_is_row(tmp_db):
    assert tmp_db._conn.row_factory is sqlite3.Row
