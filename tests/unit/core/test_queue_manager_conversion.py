"""Unit tests for the conversion handoff added to :class:`QueueManager`.

Uses the ``queue`` fixture (real QueueManager on a temp DB) from conftest.py.
``QThreadPool`` is patched so no conversion worker actually runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from viddrop.core.conversion_worker import ConversionWorker


def _add(queue, dest="/home/user/v.mp4"):
    return queue.add_download("https://example.com/v", "T", dest)


def test_download_completed_enqueues_conversion_worker(
    qtbot, queue, monkeypatch
):
    pool = MagicMock()
    monkeypatch.setattr(
        "PyQt6.QtCore.QThreadPool.globalInstance", lambda: pool
    )

    entry = _add(queue)
    queue.set_output_path(entry.id, "/home/user/v.mp4")
    queue.mark_complete(entry.id)

    pool.start.assert_called_once()
    worker = pool.start.call_args.args[0]
    assert isinstance(worker, ConversionWorker)


def test_conversion_relay_signals(qtbot, queue):
    with qtbot.waitSignal(
        queue.conversion_finished, timeout=500
    ) as blocker:
        queue._on_conversion_finished("id", "/out.mp4")
    assert blocker.args == ["id", "/out.mp4"]


def test_unknown_id_in_on_download_completed_no_crash(queue, monkeypatch):
    pool = MagicMock()
    monkeypatch.setattr(
        "PyQt6.QtCore.QThreadPool.globalInstance", lambda: pool
    )
    # Should log a warning and return without raising.
    queue._on_download_completed("nonexistent")
    pool.start.assert_not_called()


def test_no_output_file_path_skips_conversion(qtbot, queue, monkeypatch):
    pool = MagicMock()
    monkeypatch.setattr(
        "PyQt6.QtCore.QThreadPool.globalInstance", lambda: pool
    )

    entry = _add(queue)
    # Intentionally do NOT call set_output_path.
    queue.mark_complete(entry.id)

    pool.start.assert_not_called()
