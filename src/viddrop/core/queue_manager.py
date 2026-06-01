"""Download queue management for Viddrop.

This module owns the in-memory download queue and is the single source of
truth for download state. It coordinates persistence through
``DatabaseManager`` and dispatches work via Qt signals; it never performs
network or subprocess work itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtCore import QObject, pyqtSignal

from viddrop.core.database import DatabaseManager
from viddrop.utils.logger import log
from viddrop.utils.sanitize import sanitize_error


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


@dataclass
class DownloadEntry:
    """A single download tracked by the queue.

    ``status`` is one of: ``queued``, ``in_progress``, ``paused``,
    ``complete``, ``error``, ``cancelled``.
    """

    id: str
    url: str
    title: str | None
    destination_path: str
    status: str
    progress_percent: int = 0
    error_message: str | None = None
    created_at: str = field(default_factory=_utcnow_iso)
    started_at: str | None = None
    completed_at: str | None = None


class QueueManager(QObject):
    """In-memory FIFO download queue backed by SQLite.

    Emits Qt signals so UI widgets can react to state changes without
    reaching into business logic directly.
    """

    MAX_CONCURRENT: int = 3

    status_changed = pyqtSignal(str, str)  # (id, new_status)
    error_occurred = pyqtSignal(str, str)  # (id, sanitized_message)
    download_completed = pyqtSignal(str)  # (id,)
    download_ready = pyqtSignal(str)  # (id,)

    def __init__(self, db: DatabaseManager | None = None) -> None:
        super().__init__()
        self._db: DatabaseManager = db or DatabaseManager()
        self._db.open()
        self._entries: dict[str, DownloadEntry] = {}
        self._dispatching: bool = False

        for entry in self._db.load_all():
            self._entries[entry.id] = entry

        # Recovery: anything left "in_progress" from a previous run cannot
        # still be running, so requeue it.
        for entry in self._entries.values():
            if entry.status == "in_progress":
                entry.status = "queued"
                self._db.update_status(entry.id, "queued")
                log.warning("Requeued stale in_progress download: id=%s", entry.id)

        self._maybe_dispatch()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def add_download(
        self, url: str, title: str | None, dest_path: str
    ) -> DownloadEntry:
        """Create and enqueue a new download, then attempt dispatch."""
        entry = DownloadEntry(
            id=str(uuid.uuid4()),
            url=url,
            title=title,
            destination_path=dest_path,
            status="queued",
        )
        self._db.insert_download(entry)
        self._entries[entry.id] = entry
        # Never log the full URL: only the hostname.
        log.info("Download added: id=%s host=%s", entry.id, urlparse(url).hostname)
        self._maybe_dispatch()
        return entry

    def pause(self, download_id: str) -> None:
        """Pause an in-progress download. No-op for any other status."""
        entry = self._get_or_raise(download_id)
        if entry.status != "in_progress":
            return
        entry.status = "paused"
        self._db.update_status(download_id, "paused")
        log.info("Download paused: id=%s", download_id)
        self.status_changed.emit(download_id, "paused")
        self._maybe_dispatch()

    def resume(self, download_id: str) -> None:
        """Requeue a paused download. No-op for any other status."""
        entry = self._get_or_raise(download_id)
        if entry.status != "paused":
            return
        entry.status = "queued"
        self._db.update_status(download_id, "queued")
        log.info("Download resumed: id=%s", download_id)
        self.status_changed.emit(download_id, "queued")
        self._maybe_dispatch()

    def stop(self, download_id: str) -> None:
        """Cancel a download. No-op if already terminal."""
        entry = self._get_or_raise(download_id)
        if entry.status in {"complete", "cancelled", "error"}:
            return
        was_active = entry.status == "in_progress"
        entry.status = "cancelled"
        self._db.update_status(download_id, "cancelled")
        log.info("Download cancelled: id=%s", download_id)
        self.status_changed.emit(download_id, "cancelled")
        if was_active:
            self._maybe_dispatch()

    def remove(self, download_id: str) -> None:
        """Remove a terminal download from the list and DB (keeps file)."""
        entry = self._get_or_raise(download_id)
        if entry.status not in {"complete", "cancelled", "error"}:
            raise ValueError(
                f"Cannot remove download {download_id!r}: status is {entry.status!r}"
            )
        self._db.delete_download(download_id)
        del self._entries[download_id]
        log.info("Download removed from list: id=%s", download_id)

    def delete(self, download_id: str) -> None:
        """Remove a terminal download and delete its file from storage.

        Guards against deleting symlinks, directories, or anything outside the
        user's home directory.
        """
        entry = self._get_or_raise(download_id)
        if entry.status not in {"complete", "cancelled", "error"}:
            raise ValueError(
                f"Cannot delete download {download_id!r}: status is {entry.status!r}"
            )
        raw_path = Path(entry.destination_path)
        # Check for a symlink on the unresolved path: resolve() would follow
        # the link and make this guard a no-op, so we must test first.
        if raw_path.is_symlink():
            raise ValueError("Refusing to delete symlink")
        path = raw_path.resolve()
        home = Path.home().resolve()
        if path.is_dir():
            raise ValueError("Refusing to delete directory")
        if not str(path).startswith(str(home)):
            raise ValueError("Refusing to delete path outside home directory")
        if path.exists():
            path.unlink()
        self._db.delete_download(download_id)
        del self._entries[download_id]
        log.info("Download deleted from storage: id=%s", download_id)

    def update_progress(self, download_id: str, percent: int) -> None:
        """Update progress in memory and DB. Emits no signal."""
        entry = self._get_or_raise(download_id)
        percent = max(0, min(100, percent))
        entry.progress_percent = percent
        self._db.update_progress(download_id, percent)

    def mark_complete(self, download_id: str) -> None:
        """Mark a download complete and dispatch any waiting work."""
        entry = self._get_or_raise(download_id)
        now = _utcnow_iso()
        entry.status = "complete"
        entry.completed_at = now
        self._db.update_status(download_id, "complete", completed_at=now)
        log.info("Download complete: id=%s", download_id)
        self.download_completed.emit(download_id)
        self.status_changed.emit(download_id, "complete")
        self._maybe_dispatch()

    def mark_error(self, download_id: str, raw_message: str) -> None:
        """Mark a download as errored with a sanitized message."""
        entry = self._get_or_raise(download_id)
        sanitized = sanitize_error(raw_message)
        entry.status = "error"
        entry.error_message = sanitized
        self._db.update_error(download_id, "error", sanitized)
        log.warning("Download error: id=%s message=%s", download_id, sanitized)
        self.error_occurred.emit(download_id, sanitized)
        self.status_changed.emit(download_id, "error")
        self._maybe_dispatch()

    def get_entry(self, download_id: str) -> DownloadEntry:
        """Return the entry for ``download_id`` or raise ``ValueError``."""
        return self._get_or_raise(download_id)

    def all_entries(self) -> list[DownloadEntry]:
        """Return all entries in insertion (FIFO) order."""
        return list(self._entries.values())

    def active_count(self) -> int:
        """Return the number of currently in-progress downloads."""
        return sum(1 for e in self._entries.values() if e.status == "in_progress")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _maybe_dispatch(self) -> None:
        """Promote queued downloads to in_progress up to MAX_CONCURRENT.

        Guarded against re-entrancy: signal handlers may call back into the
        queue, so a single dispatch pass owns the flag for its duration.
        """
        if self._dispatching:
            return
        self._dispatching = True
        try:
            active = sum(
                1 for e in self._entries.values() if e.status == "in_progress"
            )
            while active < self.MAX_CONCURRENT:
                next_entry = next(
                    (e for e in self._entries.values() if e.status == "queued"),
                    None,
                )
                if next_entry is None:
                    break
                next_entry.status = "in_progress"
                next_entry.started_at = _utcnow_iso()
                self._db.update_status(
                    next_entry.id, "in_progress", started_at=next_entry.started_at
                )
                log.info("Dispatching download: id=%s", next_entry.id)
                self.download_ready.emit(next_entry.id)
                active += 1
        finally:
            self._dispatching = False

    def _get_or_raise(self, download_id: str) -> DownloadEntry:
        if download_id not in self._entries:
            raise ValueError(f"Unknown download id: {download_id!r}")
        return self._entries[download_id]

