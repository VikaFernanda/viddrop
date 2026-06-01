"""QThreadPool worker that runs a single download off the main thread.

A :class:`DownloadWorker` ties together the three backend pieces:
- :class:`~viddrop.core.queue_manager.QueueManager` (source of truth for state)
- :mod:`viddrop.core.credential_store` (secure credential lookup)
- :class:`~viddrop.core.downloader.Downloader` (the yt-dlp engine)

It never touches the UI directly; it only calls back into ``QueueManager``,
which emits the Qt signals the UI listens to.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from PyQt6.QtCore import QRunnable

from viddrop.core import credential_store
from viddrop.core.credential_store import CredentialStoreError
from viddrop.core.downloader import (
    DownloadCancelledError,
    DownloadError,
    Downloader,
)
from viddrop.utils.logger import log

if TYPE_CHECKING:
    from viddrop.core.queue_manager import QueueManager


class DownloadWorker(QRunnable):
    """Runs one download to completion inside a ``QThreadPool``."""

    def __init__(
        self,
        download_id: str,
        queue_manager: QueueManager,
        downloader: Downloader | None = None,
    ) -> None:
        super().__init__()
        self._download_id = download_id
        self._queue_manager = queue_manager
        # Injectable for testing; defaults to a fresh Downloader per worker.
        self._downloader = downloader or Downloader()

    def run(self) -> None:
        """Execute the download. Errors are routed back through QueueManager."""
        download_id = self._download_id
        try:
            entry = self._queue_manager.get_entry(download_id)
        except ValueError:
            log.warning("DownloadWorker: unknown download id=%s", download_id)
            return

        try:
            service_key = urlparse(entry.url).hostname or ""
            credentials = credential_store.get_credentials(service_key)
        except CredentialStoreError:
            log.warning(
                "DownloadWorker: credential store unavailable for id=%s",
                download_id,
            )
            self._queue_manager.mark_error(download_id, "Credential store unavailable")
            return

        try:
            self._downloader.start(
                entry.url,
                entry.destination_path,
                credentials,
                self._on_progress,
            )
        except DownloadCancelledError:
            # Downloader.cancel() was called via DownloadWorker.cancel().
            # The caller is responsible for transitioning QueueManager state.
            log.info("DownloadWorker: download cancelled id=%s", download_id)
            return
        except DownloadError as exc:
            self._queue_manager.mark_error(download_id, str(exc))
            return

        self._queue_manager.mark_complete(download_id)

    def cancel(self) -> None:
        """Interrupt the in-flight download. Thread-safe."""
        self._downloader.cancel()

    def pause(self) -> None:
        """Pause the in-flight download. Thread-safe."""
        self._downloader.pause()

    def resume(self) -> None:
        """Resume a paused download. Thread-safe."""
        self._downloader.resume()

    def _on_progress(self, info: dict[str, Any]) -> None:
        """Forward a normalized progress dict into a percent update.

        ``info`` is the already-normalized dict produced by
        ``Downloader._normalize_progress`` (keys: ``percent``, ``speed``,
        ``eta``, ``status``), so read ``percent`` directly.
        """
        if info.get("status") == "downloading":
            self._queue_manager.update_progress(
                self._download_id, int(info.get("percent", 0.0))
            )
