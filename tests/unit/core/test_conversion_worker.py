"""Unit tests for :class:`ConversionWorker`.

The :class:`Converter` is replaced with a fake so no FFmpeg runs. ``qtbot`` is
used purely to drive signal capture; the worker's ``run`` is called directly.
"""

from __future__ import annotations

from viddrop.core.conversion_worker import ConversionWorker
from viddrop.core.converter import (
    ConversionCancelledError,
    ConversionError,
    ConversionSettings,
    ResolutionTooLowError,
)


class FakeConverter:
    """Records ``convert`` arguments and replays a programmed result."""

    def __init__(self, result="/out.mp4", raises=None):
        self.result = result
        self.raises = raises
        self.calls: list[dict] = []
        self.progress_callback = None

    def convert(
        self, input_path, settings, progress_callback=None, cancel_event=None
    ):
        self.calls.append(
            {
                "input_path": input_path,
                "settings": settings,
                "progress_callback": progress_callback,
                "cancel_event": cancel_event,
            }
        )
        self.progress_callback = progress_callback
        if self.raises is not None:
            raise self.raises
        return self.result


def _worker(converter, settings=None):
    return ConversionWorker(
        download_id="id1",
        input_path="/in.mkv",
        settings=settings,
        converter=converter,
    )


def test_finished_signal_emitted_on_success(qtbot):
    worker = _worker(FakeConverter(result="/out.mp4"))
    with qtbot.waitSignal(
        worker.signals.conversion_finished, timeout=500
    ) as blocker:
        worker.run()
    assert blocker.args == ["id1", "/out.mp4"]


def test_progress_signal_forwarded(qtbot):
    fake = FakeConverter()
    worker = _worker(fake)
    worker.run()  # populates fake.progress_callback
    with qtbot.waitSignal(
        worker.signals.conversion_progress, timeout=500
    ) as blocker:
        fake.progress_callback(50.0)
    assert blocker.args == ["id1", 50.0]


def test_default_settings_are_mp4_1080p_medium_replace(qtbot):
    worker = ConversionWorker(download_id="id1", input_path="/in.mkv")
    assert worker._settings == ConversionSettings(
        "mp4", "1080p", "medium", True
    )


def test_failed_signal_on_conversion_error(qtbot):
    worker = _worker(FakeConverter(raises=ConversionError("bad")))
    finished: list = []
    worker.signals.conversion_finished.connect(
        lambda *a: finished.append(a)
    )
    with qtbot.waitSignal(
        worker.signals.conversion_failed, timeout=500
    ) as blocker:
        worker.run()
    assert blocker.args[0] == "id1"
    assert finished == []


def test_both_signals_on_resolution_too_low(qtbot):
    worker = _worker(
        FakeConverter(
            raises=ResolutionTooLowError("low", max_resolution="720p")
        )
    )
    failed: list = []
    worker.signals.conversion_failed.connect(lambda *a: failed.append(a))
    with qtbot.waitSignal(
        worker.signals.conversion_needs_reselect, timeout=500
    ) as blocker:
        worker.run()
    assert blocker.args == ["id1", "720p"]
    assert failed and failed[0][0] == "id1"


def test_cancelled_signal_on_cancelled_error(qtbot):
    worker = _worker(FakeConverter(raises=ConversionCancelledError("x")))
    failed: list = []
    worker.signals.conversion_failed.connect(lambda *a: failed.append(a))
    with qtbot.waitSignal(
        worker.signals.conversion_cancelled, timeout=500
    ) as blocker:
        worker.run()
    assert blocker.args == ["id1"]
    assert failed == []


def test_cancel_sets_event():
    worker = ConversionWorker(download_id="id1", input_path="/in.mkv")
    assert not worker._cancel_event.is_set()
    worker.cancel()
    assert worker._cancel_event.is_set()
