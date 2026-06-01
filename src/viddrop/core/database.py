"""SQLite persistence for the Viddrop download queue.

All queries use ``?`` positional placeholders exclusively; no values are
ever interpolated into SQL strings. Raw SQLite errors are logged in full
but surfaced to callers in a sanitized form (no paths or internals).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from viddrop.utils.logger import log

if TYPE_CHECKING:
    from viddrop.core.queue_manager import DownloadEntry


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS downloads (
    id                TEXT    PRIMARY KEY,
    url               TEXT    NOT NULL,
    title             TEXT,
    destination_path  TEXT    NOT NULL,
    status            TEXT    NOT NULL,
    progress_percent  INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT,
    created_at        TEXT    NOT NULL,
    started_at        TEXT,
    completed_at      TEXT
);
"""

_CREATE_INDEX_STATUS = (
    "CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads (status);"
)
_CREATE_INDEX_CREATED = (
    "CREATE INDEX IF NOT EXISTS idx_downloads_created_at "
    "ON downloads (created_at);"
)


class DatabaseManager:
    """Owns the SQLite connection and all CRUD for the downloads table."""

    DEFAULT_DB_DIR = Path.home() / ".local" / "share" / "viddrop"
    DEFAULT_DB_PATH = DEFAULT_DB_DIR / "viddrop.db"

    def __init__(self) -> None:
        # Instance attributes so tests can override the location per-instance.
        self.DB_DIR: Path = self.DEFAULT_DB_DIR
        self.DB_PATH: Path = self.DEFAULT_DB_PATH
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #

    def open(self) -> None:
        """Open the connection and ensure schema exists.

        Idempotent thanks to ``IF NOT EXISTS`` in the DDL.
        """
        try:
            self.DB_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.DB_PATH), check_same_thread=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX_STATUS)
            conn.execute(_CREATE_INDEX_CREATED)
            conn.commit()
            self._conn = conn
        except sqlite3.OperationalError as exc:
            log.error("Failed to open database: %s", exc)
            raise RuntimeError("Database unavailable") from exc

    def close(self) -> None:
        """Close the connection if open."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def insert_download(self, entry: DownloadEntry) -> None:
        """Insert a new download row."""
        conn = self._require_conn()
        conn.execute(
            "INSERT INTO downloads ("
            "id, url, title, destination_path, status, progress_percent, "
            "error_message, created_at, started_at, completed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.url,
                entry.title,
                entry.destination_path,
                entry.status,
                entry.progress_percent,
                entry.error_message,
                entry.created_at,
                entry.started_at,
                entry.completed_at,
            ),
        )
        conn.commit()

    def update_status(
        self,
        download_id: str,
        status: str,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Update status and, when provided, timestamps.

        Timestamp columns are only written when their argument is non-None,
        so an existing value is preserved when the caller omits it.
        """
        conn = self._require_conn()
        if started_at is not None and completed_at is not None:
            conn.execute(
                "UPDATE downloads SET status = ?, started_at = ?, "
                "completed_at = ? WHERE id = ?",
                (status, started_at, completed_at, download_id),
            )
        elif started_at is not None:
            conn.execute(
                "UPDATE downloads SET status = ?, started_at = ? WHERE id = ?",
                (status, started_at, download_id),
            )
        elif completed_at is not None:
            conn.execute(
                "UPDATE downloads SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, download_id),
            )
        else:
            conn.execute(
                "UPDATE downloads SET status = ? WHERE id = ?",
                (status, download_id),
            )
        conn.commit()

    def update_progress(self, download_id: str, percent: int) -> None:
        """Update the progress percentage for a download."""
        conn = self._require_conn()
        conn.execute(
            "UPDATE downloads SET progress_percent = ? WHERE id = ?",
            (percent, download_id),
        )
        conn.commit()

    def update_error(
        self, download_id: str, status: str, error_message: str
    ) -> None:
        """Set the error status and message for a download."""
        conn = self._require_conn()
        conn.execute(
            "UPDATE downloads SET status = ?, error_message = ? WHERE id = ?",
            (status, error_message, download_id),
        )
        conn.commit()

    def delete_download(self, download_id: str) -> None:
        """Delete a download row. No-op if the id does not exist."""
        conn = self._require_conn()
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
        conn.commit()

    def load_all(self) -> list[DownloadEntry]:
        """Return all downloads ordered by ``created_at`` ascending."""
        # Imported lazily to avoid a circular import at module load time.
        from viddrop.core.queue_manager import DownloadEntry

        conn = self._require_conn()
        rows = conn.execute(
            "SELECT id, url, title, destination_path, status, "
            "progress_percent, error_message, created_at, started_at, "
            "completed_at FROM downloads ORDER BY created_at ASC"
        ).fetchall()
        return [
            DownloadEntry(
                id=row["id"],
                url=row["url"],
                title=row["title"],
                destination_path=row["destination_path"],
                status=row["status"],
                progress_percent=row["progress_percent"],
                error_message=row["error_message"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn
