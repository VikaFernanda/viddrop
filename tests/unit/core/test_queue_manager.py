"""Unit tests for QueueManager."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from viddrop.core.queue_manager import DownloadEntry, QueueManager


@pytest.fixture
def home_dir(tmp_path, monkeypatch):
    """A temporary directory that ``Path.home()`` resolves to.

    ``delete()`` only removes files under the user's home directory, so
    delete tests must operate inside a sandboxed home.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _add(queue: QueueManager, dest: str = "/home/user/v.mp4") -> DownloadEntry:
    return queue.add_download("https://example.com/watch?v=abc", "Title", dest)


# ----------------------------------------------------------------------- #
# add_download
# ----------------------------------------------------------------------- #


def test_add_download_returns_queued_entry(queue):
    entry = queue.add_download("https://example.com/v", "T", "/home/user/v.mp4")
    assert isinstance(entry, DownloadEntry)
    # id is a valid UUID4 string
    uuid.UUID(entry.id)
    assert entry.progress_percent == 0
    # With a free slot it is immediately dispatched to in_progress.
    assert entry.status == "in_progress"


def test_add_download_appears_in_all_entries(queue):
    entry = _add(queue)
    assert entry in queue.all_entries()


def test_add_download_emits_download_ready_when_slot_free(queue, qtbot):
    with qtbot.waitSignal(queue.download_ready, timeout=200) as blocker:
        entry = _add(queue)
    assert blocker.args == [entry.id]
    assert queue.get_entry(entry.id).status == "in_progress"


def test_add_duplicate_url_creates_distinct_ids(queue):
    e1 = queue.add_download("https://dup/v", "A", "/home/user/a.mp4")
    e2 = queue.add_download("https://dup/v", "B", "/home/user/b.mp4")
    assert e1.id != e2.id
    ids = {e.id for e in queue.all_entries()}
    assert {e1.id, e2.id} <= ids


def test_add_download_with_none_title(queue):
    entry = queue.add_download("https://example.com/v", None, "/home/user/v.mp4")
    assert entry.title is None


# ----------------------------------------------------------------------- #
# pause
# ----------------------------------------------------------------------- #


def test_pause_in_progress_transitions_and_dispatches_next(queue, qtbot):
    # Fill all 3 slots, plus a 4th queued.
    actives = [_add(queue) for _ in range(3)]
    fourth = _add(queue)
    assert fourth.status == "queued"
    assert queue.active_count() == 3

    with qtbot.waitSignals(
        [queue.status_changed, queue.download_ready], timeout=200
    ):
        queue.pause(actives[0].id)

    assert queue.get_entry(actives[0].id).status == "paused"
    # The previously queued fourth item should now be active.
    assert queue.get_entry(fourth.id).status == "in_progress"
    assert queue.active_count() == 3


def test_pause_emits_status_changed_args(queue, qtbot):
    entry = _add(queue)
    with qtbot.waitSignal(queue.status_changed, timeout=200) as blocker:
        queue.pause(entry.id)
    assert blocker.args == [entry.id, "paused"]


def test_pause_noop_on_queued(queue, qtbot):
    # Fill slots so a 4th remains queued.
    for _ in range(3):
        _add(queue)
    queued = _add(queue)
    assert queued.status == "queued"
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.pause(queued.id)
    assert queue.get_entry(queued.id).status == "queued"


def test_pause_noop_on_paused(queue, qtbot):
    entry = _add(queue)
    queue.pause(entry.id)
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.pause(entry.id)


def test_pause_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.pause("nope")


# ----------------------------------------------------------------------- #
# resume
# ----------------------------------------------------------------------- #


def test_resume_paused_redispatches(queue, qtbot):
    entry = _add(queue)
    queue.pause(entry.id)
    assert queue.get_entry(entry.id).status == "paused"

    with qtbot.waitSignal(queue.download_ready, timeout=200) as blocker:
        queue.resume(entry.id)
    assert blocker.args == [entry.id]
    assert queue.get_entry(entry.id).status == "in_progress"


def test_resume_emits_status_changed_queued(queue, qtbot):
    # Keep all slots busy so resume lands on "queued" without re-dispatch.
    busy = [_add(queue) for _ in range(3)]
    extra = _add(queue)  # queued
    queue.pause(busy[0].id)  # frees a slot -> extra becomes in_progress
    # busy[0] is paused now; resume it (no free slot, stays queued).
    with qtbot.waitSignal(queue.status_changed, timeout=200) as blocker:
        queue.resume(busy[0].id)
    assert blocker.args == [busy[0].id, "queued"]
    assert queue.get_entry(busy[0].id).status == "queued"
    assert extra.status == "in_progress"


def test_resume_noop_on_queued(queue, qtbot):
    for _ in range(3):
        _add(queue)
    queued = _add(queue)
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.resume(queued.id)


def test_resume_noop_on_in_progress(queue, qtbot):
    entry = _add(queue)
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.resume(entry.id)


def test_resume_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.resume("nope")


# ----------------------------------------------------------------------- #
# stop
# ----------------------------------------------------------------------- #


def test_stop_in_progress_frees_slot_and_dispatches(queue, qtbot):
    actives = [_add(queue) for _ in range(3)]
    fourth = _add(queue)
    with qtbot.waitSignals(
        [queue.status_changed, queue.download_ready], timeout=200
    ):
        queue.stop(actives[0].id)
    assert queue.get_entry(actives[0].id).status == "cancelled"
    assert queue.get_entry(fourth.id).status == "in_progress"


def test_stop_queued_emits_status_changed(queue, qtbot):
    for _ in range(3):
        _add(queue)
    queued = _add(queue)
    with qtbot.waitSignal(queue.status_changed, timeout=200) as blocker:
        queue.stop(queued.id)
    assert blocker.args == [queued.id, "cancelled"]


def test_stop_paused_emits_status_changed(queue, qtbot):
    entry = _add(queue)
    queue.pause(entry.id)
    with qtbot.waitSignal(queue.status_changed, timeout=200) as blocker:
        queue.stop(entry.id)
    assert blocker.args == [entry.id, "cancelled"]


def test_stop_noop_on_complete(queue, qtbot):
    entry = _add(queue)
    queue.mark_complete(entry.id)
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.stop(entry.id)


def test_stop_noop_on_cancelled(queue, qtbot):
    entry = _add(queue)
    queue.stop(entry.id)
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.stop(entry.id)


def test_stop_noop_on_error(queue, qtbot):
    entry = _add(queue)
    queue.mark_error(entry.id, "boom")
    with qtbot.assertNotEmitted(queue.status_changed):
        queue.stop(entry.id)


def test_stop_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.stop("nope")


# ----------------------------------------------------------------------- #
# remove
# ----------------------------------------------------------------------- #


def test_remove_complete_entry(queue, tmp_db):
    entry = _add(queue)
    queue.mark_complete(entry.id)
    queue.remove(entry.id)
    assert entry not in queue.all_entries()
    assert all(e.id != entry.id for e in tmp_db.load_all())


def test_remove_cancelled_entry(queue):
    entry = _add(queue)
    queue.stop(entry.id)
    queue.remove(entry.id)
    assert entry.id not in {e.id for e in queue.all_entries()}


def test_remove_error_entry(queue):
    entry = _add(queue)
    queue.mark_error(entry.id, "boom")
    queue.remove(entry.id)
    assert entry.id not in {e.id for e in queue.all_entries()}


def test_remove_queued_raises(queue):
    for _ in range(3):
        _add(queue)
    queued = _add(queue)
    with pytest.raises(ValueError):
        queue.remove(queued.id)


def test_remove_in_progress_raises(queue):
    entry = _add(queue)
    with pytest.raises(ValueError):
        queue.remove(entry.id)


def test_remove_paused_raises(queue):
    entry = _add(queue)
    queue.pause(entry.id)
    with pytest.raises(ValueError):
        queue.remove(entry.id)


def test_remove_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.remove("nope")


# ----------------------------------------------------------------------- #
# delete
# ----------------------------------------------------------------------- #


def test_delete_complete_entry_removes_file(queue, home_dir):
    target = home_dir / "video.mp4"
    target.write_text("data")
    entry = queue.add_download("https://e/v", "T", str(target))
    queue.mark_complete(entry.id)
    queue.delete(entry.id)
    assert not target.exists()
    assert entry.id not in {e.id for e in queue.all_entries()}


def test_delete_when_file_already_gone(queue, home_dir):
    target = home_dir / "gone.mp4"  # never created
    entry = queue.add_download("https://e/v", "T", str(target))
    queue.mark_complete(entry.id)
    queue.delete(entry.id)
    assert entry.id not in {e.id for e in queue.all_entries()}


def test_delete_in_progress_raises(queue, home_dir):
    target = home_dir / "v.mp4"
    target.write_text("x")
    entry = queue.add_download("https://e/v", "T", str(target))
    with pytest.raises(ValueError):
        queue.delete(entry.id)
    assert target.exists()


def test_delete_symlink_raises(queue, home_dir):
    real = home_dir / "real.mp4"
    real.write_text("x")
    link = home_dir / "link.mp4"
    link.symlink_to(real)
    entry = queue.add_download("https://e/v", "T", str(link))
    queue.mark_complete(entry.id)
    with pytest.raises(ValueError):
        queue.delete(entry.id)
    assert real.exists()


def test_delete_directory_raises(queue, home_dir):
    d = home_dir / "adir"
    d.mkdir()
    entry = queue.add_download("https://e/v", "T", str(d))
    queue.mark_complete(entry.id)
    with pytest.raises(ValueError):
        queue.delete(entry.id)
    assert d.exists()


def test_delete_path_outside_home_raises(queue):
    entry = queue.add_download("https://e/v", "T", "/tmp/evil.mp4")
    queue.mark_complete(entry.id)
    with pytest.raises(ValueError):
        queue.delete(entry.id)


def test_delete_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.delete("nope")


# ----------------------------------------------------------------------- #
# update_progress
# ----------------------------------------------------------------------- #


def test_update_progress_no_signals(queue, qtbot, tmp_db):
    entry = _add(queue)
    with qtbot.assertNotEmitted(queue.status_changed), qtbot.assertNotEmitted(
        queue.error_occurred
    ), qtbot.assertNotEmitted(queue.download_completed), qtbot.assertNotEmitted(
        queue.download_ready
    ):
        queue.update_progress(entry.id, 33)
    assert queue.get_entry(entry.id).progress_percent == 33
    stored = next(e for e in tmp_db.load_all() if e.id == entry.id)
    assert stored.progress_percent == 33


def test_update_progress_back_to_zero(queue):
    entry = _add(queue)
    queue.update_progress(entry.id, 100)
    queue.update_progress(entry.id, 0)
    assert queue.get_entry(entry.id).progress_percent == 0


def test_update_progress_clamps_above_100(queue):
    entry = _add(queue)
    queue.update_progress(entry.id, 150)
    assert queue.get_entry(entry.id).progress_percent == 100


def test_update_progress_clamps_below_0(queue):
    entry = _add(queue)
    queue.update_progress(entry.id, -5)
    assert queue.get_entry(entry.id).progress_percent == 0


def test_update_progress_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.update_progress("nope", 10)


# ----------------------------------------------------------------------- #
# mark_complete
# ----------------------------------------------------------------------- #


def test_mark_complete_emits_and_dispatches(queue, qtbot):
    actives = [_add(queue) for _ in range(3)]
    fourth = _add(queue)
    with qtbot.waitSignals(
        [queue.download_completed, queue.status_changed, queue.download_ready],
        timeout=200,
    ):
        queue.mark_complete(actives[0].id)
    done = queue.get_entry(actives[0].id)
    assert done.status == "complete"
    assert done.completed_at is not None
    assert queue.get_entry(fourth.id).status == "in_progress"


def test_mark_complete_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.mark_complete("nope")


def test_mark_complete_only_active_no_redispatch(queue, qtbot):
    entry = _add(queue)
    assert queue.active_count() == 1
    with qtbot.assertNotEmitted(queue.download_ready):
        queue.mark_complete(entry.id)
    assert queue.active_count() == 0


# ----------------------------------------------------------------------- #
# mark_error
# ----------------------------------------------------------------------- #


def test_mark_error_emits_both_signals(queue, qtbot):
    entry = _add(queue)
    with qtbot.waitSignal(queue.error_occurred, timeout=200) as err_blocker:
        with qtbot.waitSignal(queue.status_changed, timeout=200) as st_blocker:
            queue.mark_error(entry.id, "plain failure")
    assert err_blocker.args == [entry.id, "plain failure"]
    assert st_blocker.args == [entry.id, "error"]


def test_mark_error_stores_sanitized(queue):
    entry = _add(queue)
    queue.mark_error(entry.id, "  spaced failure  ")
    assert queue.get_entry(entry.id).error_message == "spaced failure"


def test_mark_error_redacts_authorization(queue, qtbot):
    entry = _add(queue)
    raw = "Failed with header Authorization: Bearer secrettoken12345 here"
    with qtbot.waitSignal(queue.error_occurred, timeout=200) as blocker:
        queue.mark_error(entry.id, raw)
    emitted = blocker.args[1]
    # The full Authorization header value is collapsed into the redaction token.
    assert "<redacted>" in emitted
    assert "secrettoken12345" not in emitted
    assert "Bearer" not in emitted
    stored = queue.get_entry(entry.id).error_message
    assert "<redacted>" in stored
    assert "secrettoken12345" not in stored
    assert "Bearer" not in stored


def test_mark_error_redacts_token_query_param(queue):
    entry = _add(queue)
    queue.mark_error(entry.id, "url failed with token=AbCdEf12345678 in it")
    stored = queue.get_entry(entry.id).error_message
    assert "<redacted>" in stored
    assert "AbCdEf12345678" not in stored


def test_mark_error_truncates_to_500(queue):
    entry = _add(queue)
    queue.mark_error(entry.id, "x" * 1000)
    assert len(queue.get_entry(entry.id).error_message) == 500


def test_mark_error_empty_message(queue):
    entry = _add(queue)
    queue.mark_error(entry.id, "   ")
    assert queue.get_entry(entry.id).error_message == "Unknown error"


def test_mark_error_unknown_id_raises(queue):
    with pytest.raises(ValueError):
        queue.mark_error("nope", "msg")


# ----------------------------------------------------------------------- #
# Concurrency
# ----------------------------------------------------------------------- #


def test_four_adds_only_three_active(queue):
    entries = [_add(queue) for _ in range(4)]
    statuses = [queue.get_entry(e.id).status for e in entries]
    assert statuses[:3] == ["in_progress", "in_progress", "in_progress"]
    assert statuses[3] == "queued"
    assert queue.active_count() == 3


def test_complete_dispatches_fourth(queue):
    entries = [_add(queue) for _ in range(4)]
    queue.mark_complete(entries[0].id)
    assert queue.get_entry(entries[3].id).status == "in_progress"
    assert queue.active_count() == 3


def test_ten_adds_never_exceed_max(queue):
    for _ in range(10):
        _add(queue)
        assert queue.active_count() <= QueueManager.MAX_CONCURRENT
    assert queue.active_count() == QueueManager.MAX_CONCURRENT


# ----------------------------------------------------------------------- #
# Startup recovery
# ----------------------------------------------------------------------- #


def test_startup_requeues_in_progress(tmp_db, qtbot):
    e1 = DownloadEntry(
        id="r1",
        url="https://e/1",
        title="One",
        destination_path="/home/user/1.mp4",
        status="in_progress",
    )
    e2 = DownloadEntry(
        id="r2",
        url="https://e/2",
        title="Two",
        destination_path="/home/user/2.mp4",
        status="in_progress",
    )
    tmp_db.insert_download(e1)
    tmp_db.insert_download(e2)

    qm = QueueManager(db=tmp_db)
    # Both must be requeued and then (since slots are free) dispatched again.
    # After construction they end up in_progress, but their persisted DB row
    # passed through "queued" during recovery. Verify in-memory final state
    # is in_progress (re-dispatched) and that recovery happened by checking
    # active_count.
    assert qm.active_count() == 2
    assert {qm.get_entry("r1").status, qm.get_entry("r2").status} == {
        "in_progress"
    }
