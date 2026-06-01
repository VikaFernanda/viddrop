"""QThreadPool worker that runs a single conversion off the main thread.

A :class:`ConversionWorker` wraps a :class:`~viddrop.core.converter.Converter`
call in a ``QRunnable`` and translates its blocking result (or exceptions) into
Qt signals. It never touches the UI directly; callers connect to
:class:`ConversionSignals` and react there.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from viddrop.core.converter import (
    ConversionCancelledError,
    ConversionError,
    ConversionSettings,
    Converter,
    ResolutionTooLowError,
)
from viddrop.utils.logger import log
from viddrop.utils.sanitize import sanitize_error

_DEFAULT_SETTINGS = ConversionSettings(
    output_format="mp4",
    resolution="1080p",
    quality_preset="medium",
    replace=True,
)


class ConversionSignals(QObject):
    """Qt signals emitted by a :class:`ConversionWorker`."""

    conversion_progress = pyqtSignal(str, float)  # (id, percent)
    conversion_finished = pyqtSignal(str, str)  # (id, output_path)
    conversion_failed = pyqtSignal(str, str)  # (id, sanitized_message)
    conversion_cancelled = pyqtSignal(str)  # (id,)
    conversion_needs_reselect = pyqtSignal(str, str)  # (id, max_resolution)


class ConversionWorker(QRunnable):
    """Runs one conversion to completion inside a ``QThreadPool``."""

    def __init__(
        self,
        download_id: str,
        input_path: str,
        settings: ConversionSettings | None = None,
        converter: Converter | None = None,
    ) -> None:
        super().__init__()
        self._download_id = download_id
        self._input_path = input_path
        self._settings = settings or _DEFAULT_SETTINGS
        # Injectable for testing; defaults to a fresh Converter per worker.
        self._converter = converter or Converter()
        self.signals = ConversionSignals()
        self._cancel_event = threading.Event()

    def run(self) -> None:
        """Execute the conversion, emitting the appropriate result signal."""
        download_id = self._download_id
        try:
            output_path = self._converter.convert(
                self._input_path,
                self._settings,
                progress_callback=self._on_progress,
                cancel_event=self._cancel_event,
            )
        except ConversionCancelledError:
            log.info("ConversionWorker: cancelled id=%s", download_id)
            self.signals.conversion_cancelled.emit(download_id)
            return
        except ResolutionTooLowError as exc:
            self.signals.conversion_failed.emit(
                download_id, sanitize_error(str(exc))
            )
            self.signals.conversion_needs_reselect.emit(
                download_id, exc.max_resolution
            )
            return
        except ConversionError as exc:
            self.signals.conversion_failed.emit(
                download_id, sanitize_error(str(exc))
            )
            return
        except RuntimeError as exc:
            # Main-thread guard or similar programmer error; message is internal
            # and contains no credentials or subprocess output.
            self.signals.conversion_failed.emit(download_id, str(exc))
            return

        self.signals.conversion_finished.emit(download_id, output_path)

    def cancel(self) -> None:
        """Request cancellation of the in-flight conversion. Thread-safe."""
        self._cancel_event.set()

    def _on_progress(self, percent: float) -> None:
        self.signals.conversion_progress.emit(self._download_id, percent)
