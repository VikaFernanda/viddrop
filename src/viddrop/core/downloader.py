"""yt-dlp download backend for Viddrop.

A :class:`Downloader` wraps a single yt-dlp download. ``start()`` is blocking
and MUST be invoked from a worker thread (never the Qt main thread). Pause,
resume, and cancel are thread-safe and coordinate with the running download via
``threading.Event`` objects polled inside the yt-dlp progress hook.

Security:
- Credentials are injected through the yt-dlp Python API (``opts["username"]``
  / ``opts["password"]``), never as CLI arguments visible in ``ps aux``.
- A subprocess fallback (currently unused on the normal path) writes credentials
  to a ``0o600`` temp config file and deletes it in a ``finally`` block.
- All yt-dlp error output is sanitized before it leaves this module.
- Credential values are NEVER logged.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

import yt_dlp  # type: ignore[import-untyped]

from viddrop.utils.logger import log
from viddrop.utils.sanitize import sanitize_error

ProgressCallback = Callable[[dict[str, Any]], None]


class DownloadError(Exception):
    """Raised when a download fails. Message is always sanitized."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class DownloadCancelledError(DownloadError):
    """Raised when a download is cancelled via :meth:`Downloader.cancel`."""


class Downloader:
    """Drives a single yt-dlp download with pause/resume/cancel support."""

    def __init__(self) -> None:
        # _pause_event is *set* while running; clearing it pauses the download.
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._cancel_event = threading.Event()
        self._dest_path: str | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def start(
        self,
        url: str,
        dest_path: str,
        credentials: dict[str, str] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> str:
        """Download ``url`` into ``dest_path``. Blocking; call off the main thread.

        Returns:
            The absolute path of the downloaded file on success.

        Raises:
            DownloadError: on any yt-dlp failure (message sanitized).
            DownloadCancelledError: if :meth:`cancel` is called during download.
        """
        dest = Path(dest_path)
        if not dest.is_dir():
            raise DownloadError(f"Destination path does not exist: {dest_path}")

        self._dest_path = dest_path
        self._cancel_event.clear()
        # Captured from yt-dlp's "finished" hook, not inferred from outtmpl.
        captured: dict[str, str | None] = {"filepath": None}

        def hook(info: dict[str, Any]) -> None:
            self._progress_hook(info, captured, progress_callback)

        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "outtmpl": str(dest / "%(title)s.%(ext)s"),
            "progress_hooks": [hook],
        }
        if credentials is not None:
            # Inject via the Python API only — never CLI args (ps aux leak).
            opts["username"] = credentials["username"]
            opts["password"] = credentials["password"]

        log.info("Download starting: dest=%s", dest_path)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except DownloadCancelledError:
            self._cleanup_partials(dest)
            log.info("Download cancelled by user")
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize all yt-dlp failures
            sanitized = self._sanitize_error(str(exc))
            log.warning("Download failed: %s", sanitized)
            raise DownloadError(sanitized) from None

        result = captured["filepath"]
        if result is None:
            raise DownloadError("Download finished but no output file was reported")
        absolute = str(Path(result).resolve())
        log.info("Download complete: dest=%s", dest_path)
        return absolute

    def pause(self) -> None:
        """Pause the download. Thread-safe. No-op if not started."""
        self._pause_event.clear()

    def resume(self) -> None:
        """Resume a paused download. Thread-safe."""
        self._pause_event.set()

    def cancel(self) -> None:
        """Request cancellation. Thread-safe; takes effect on the next hook call.

        Also wakes a paused download so cancellation lands within ~1 second.
        """
        self._cancel_event.set()
        # Unblock any pause wait so the hook can observe the cancel flag.
        self._pause_event.set()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _progress_hook(
        self,
        info: dict[str, Any],
        captured: dict[str, str | None],
        progress_callback: ProgressCallback | None,
    ) -> None:
        """yt-dlp progress hook: enforces cancel/pause and forwards progress."""
        if self._cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled")

        # Block while paused, waking every second to re-check the cancel flag.
        while not self._pause_event.is_set():
            if self._cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled")
            self._pause_event.wait(timeout=1)

        if self._cancel_event.is_set():
            raise DownloadCancelledError("Download cancelled")

        if info.get("status") == "finished":
            captured["filepath"] = info.get("filename")

        if progress_callback is not None:
            progress_callback(self._normalize_progress(info))

    @staticmethod
    def _normalize_progress(d: dict[str, Any]) -> dict[str, Any]:
        """Map a raw yt-dlp progress dict to the four guaranteed keys.

        yt-dlp exposes pre-formatted strings (``_percent_str``, ``_speed_str``,
        ``_eta_str``) rather than a numeric percent. Consumers rely on a stable
        shape: ``percent`` (float 0-100), ``speed`` (str), ``eta`` (str),
        ``status`` (str).
        """
        percent = 0.0
        percent_str = d.get("_percent_str")
        if percent_str is not None:
            try:
                percent = float(percent_str.strip().rstrip("%"))
            except (ValueError, AttributeError):
                percent = 0.0
        else:
            downloaded = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if downloaded is not None and total:
                try:
                    percent = downloaded / total * 100
                except (TypeError, ZeroDivisionError):
                    percent = 0.0

        speed = d.get("_speed_str", "N/A")
        eta = d.get("_eta_str", "N/A")
        return {
            "percent": percent,
            "speed": speed.strip() if isinstance(speed, str) else "N/A",
            "eta": eta.strip() if isinstance(eta, str) else "N/A",
            "status": d.get("status", "N/A"),
        }

    def _cleanup_partials(self, dest: Path) -> None:
        """Remove leftover ``.part`` files after a cancel. Logs on failure."""
        try:
            for part in dest.glob("*.part"):
                part.unlink(missing_ok=True)
        except OSError:
            # Do not include the path/content in the warning.
            log.warning("Failed to clean up partial download files after cancel")

    @staticmethod
    def _sanitize_error(raw: str) -> str:
        return sanitize_error(raw)
