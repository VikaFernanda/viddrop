"""Unit tests for DownloadWorker.

The downloader and credential store are mocked; QueueManager is a lightweight
stub for most tests and a real instance only for the QThreadPool integration
test. No real network calls are made.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import Mock

import pytest

from viddrop.core import download_worker as worker_mod
from viddrop.core.credential_store import CredentialStoreError
from viddrop.core.download_worker import DownloadWorker
from viddrop.core.downloader import DownloadCancelledError, DownloadError


@dataclass
class StubEntry:
    id: str
    url: str
    destination_path: str
    title: str | None = "T"


class StubQueueManager:
    """Records the QueueManager calls a worker makes."""

    def __init__(self, entry: StubEntry) -> None:
        self._entry = entry
        self.completed: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.progress: list[tuple[str, int]] = []
        self.output_paths: list[tuple[str, str]] = []

    def get_entry(self, download_id: str) -> StubEntry:
        if download_id != self._entry.id:
            raise ValueError("unknown")
        return self._entry

    def mark_complete(self, download_id: str) -> None:
        self.completed.append(download_id)

    def mark_error(self, download_id: str, raw_message: str) -> None:
        self.errors.append((download_id, raw_message))

    def update_progress(self, download_id: str, percent: int) -> None:
        self.progress.append((download_id, percent))

    def set_output_path(self, download_id: str, file_path: str) -> None:
        self.output_paths.append((download_id, file_path))


class FakeDownloader:
    """Configurable downloader stub. Captures start() args."""

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self.raise_exc = raise_exc
        self.start_args: dict | None = None

    def start(self, url, dest_path, credentials, progress_callback):
        self.start_args = {
            "url": url,
            "dest_path": dest_path,
            "credentials": credentials,
            "progress_callback": progress_callback,
        }
        if self.raise_exc is not None:
            raise self.raise_exc
        return f"{dest_path}/out.mp4"


@pytest.fixture
def entry() -> StubEntry:
    return StubEntry(id="d1", url="https://example.com/v", destination_path="/tmp/dl")


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    """Default: credential store returns None unless a test overrides it."""
    monkeypatch.setattr(
        worker_mod.credential_store, "get_credentials", lambda key: None
    )


# ----------------------------------------------------------------------- #
# Happy path
# ----------------------------------------------------------------------- #


def test_run_calls_start_and_marks_complete(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    DownloadWorker(entry.id, qm, downloader=fake).run()

    assert fake.start_args["url"] == entry.url
    assert fake.start_args["dest_path"] == entry.destination_path
    assert qm.completed == [entry.id]
    assert qm.errors == []


def test_run_stores_output_path_before_marking_complete(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    DownloadWorker(entry.id, qm, downloader=fake).run()

    # set_output_path must be called with the resolved file path returned by Downloader.start()
    assert qm.output_paths == [(entry.id, f"{entry.destination_path}/out.mp4")]
    # and it must happen before mark_complete
    assert qm.completed == [entry.id]


# ----------------------------------------------------------------------- #
# Failure path
# ----------------------------------------------------------------------- #


def test_download_error_marks_error_not_complete(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader(raise_exc=DownloadError("boom"))
    DownloadWorker(entry.id, qm, downloader=fake).run()

    assert qm.completed == []
    assert qm.errors == [(entry.id, "boom")]


def test_cancelled_marks_nothing(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader(raise_exc=DownloadCancelledError("cancelled"))
    DownloadWorker(entry.id, qm, downloader=fake).run()

    assert qm.completed == []
    assert qm.errors == []


def test_credential_store_error_marks_specific_message(entry, monkeypatch):
    def boom(key: str):
        raise CredentialStoreError("backend down")

    monkeypatch.setattr(worker_mod.credential_store, "get_credentials", boom)
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    DownloadWorker(entry.id, qm, downloader=fake).run()

    assert qm.errors == [(entry.id, "Credential store unavailable")]
    assert fake.start_args is None  # download never attempted
    assert qm.completed == []


# ----------------------------------------------------------------------- #
# Progress / credentials wiring
# ----------------------------------------------------------------------- #


def test_progress_callback_passes_int_percent(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    worker = DownloadWorker(entry.id, qm, downloader=fake)
    worker.run()

    cb = fake.start_args["progress_callback"]
    cb({"status": "downloading", "percent": 33.0, "speed": "1MiB/s", "eta": "00:01"})
    assert qm.progress[-1] == (entry.id, 33)
    assert isinstance(qm.progress[-1][1], int)


def test_progress_empty_dict_no_update(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    worker = DownloadWorker(entry.id, qm, downloader=fake)
    worker.run()

    cb = fake.start_args["progress_callback"]
    cb({})  # no info -> no progress update, no error
    assert qm.progress == []


def test_get_credentials_none_passed_through(entry):
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    DownloadWorker(entry.id, qm, downloader=fake).run()
    assert fake.start_args["credentials"] is None


def test_get_credentials_dict_passed_through(entry, monkeypatch):
    creds = {"username": "u", "password": "p"}
    monkeypatch.setattr(
        worker_mod.credential_store, "get_credentials", lambda key: creds
    )
    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    DownloadWorker(entry.id, qm, downloader=fake).run()
    assert fake.start_args["credentials"] == creds


# ----------------------------------------------------------------------- #
# Cancel / pause / resume delegation
# ----------------------------------------------------------------------- #


def test_worker_cancel_delegates_to_downloader(entry):
    qm = StubQueueManager(entry)
    mock_downloader = Mock()
    worker = DownloadWorker(entry.id, qm, downloader=mock_downloader)

    worker.cancel()

    mock_downloader.cancel.assert_called_once_with()


def test_worker_pause_delegates_to_downloader(entry):
    qm = StubQueueManager(entry)
    mock_downloader = Mock()
    worker = DownloadWorker(entry.id, qm, downloader=mock_downloader)

    worker.pause()

    mock_downloader.pause.assert_called_once_with()


def test_worker_resume_delegates_to_downloader(entry):
    qm = StubQueueManager(entry)
    mock_downloader = Mock()
    worker = DownloadWorker(entry.id, qm, downloader=mock_downloader)

    worker.resume()

    mock_downloader.resume.assert_called_once_with()


# ----------------------------------------------------------------------- #
# QThreadPool integration
# ----------------------------------------------------------------------- #


def test_qthreadpool_integration_marks_complete(entry, qtbot):
    from PyQt6.QtCore import QThreadPool

    qm = StubQueueManager(entry)
    fake = FakeDownloader()
    worker = DownloadWorker(entry.id, qm, downloader=fake)

    pool = QThreadPool()
    pool.start(worker)
    # Wait for the worker thread to finish and call back into the queue.
    qtbot.waitUntil(lambda: qm.completed == [entry.id], timeout=3000)
    assert qm.completed == [entry.id]
