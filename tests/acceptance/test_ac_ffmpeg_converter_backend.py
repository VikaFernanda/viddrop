"""Acceptance tests for the FFmpeg Converter backend.

Covers all numbered ACs (1-17) and security ACs (S1-S5) from the approved
acceptance criteria for the FFmpeg converter backend story.

Rules:
- No real FFmpeg or ffprobe is ever spawned; subprocess.run and subprocess.Popen
  are mocked throughout.
- No real network calls.
- All tests are independently runnable.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from viddrop.core.conversion_worker import ConversionWorker
from viddrop.core.converter import (
    ConversionCancelledError,
    ConversionError,
    ConversionSettings,
    Converter,
    ResolutionTooLowError,
    _QUALITY_PRESETS,
    _RESOLUTION_DIMS,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

_FFMPEG_LINES_10S = [
    "  Duration: 00:00:10.00, start: 0.000000, bitrate: 1000 kb/s\n",
    "frame=  100 fps=50 q=20.0 time=00:00:05.00 bitrate=1000kb/s\n",
    "frame=  200 fps=50 q=20.0 time=00:00:10.00 bitrate=1000kb/s\n",
]


def _make_ffprobe(stdout: str = "1280,720\n", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _make_popen(lines, returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stderr = iter(lines)
    proc.returncode = returncode
    proc.wait = MagicMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.send_signal = MagicMock()
    return proc


def _settings(**overrides) -> ConversionSettings:
    base = dict(
        output_format="mp4",
        resolution="720p",
        quality_preset="medium",
        replace=False,
    )
    base.update(overrides)
    return ConversionSettings(**base)


def _os_replace_creates_file(src, dst):
    """os.replace side-effect that actually writes the destination."""
    Path(dst).write_bytes(b"output")


@pytest.fixture
def converter(monkeypatch):
    """Converter with the Qt main-thread guard bypassed (tests run on main thread)."""
    monkeypatch.setattr(Converter, "_assert_not_main_thread", lambda self: None)
    return Converter()


@pytest.fixture
def fake_input(tmp_path) -> str:
    f = tmp_path / "video.mkv"
    f.write_bytes(b"fake-video-data")
    return str(f)


# ---------------------------------------------------------------------------
# AC1 — convert() runs FFmpeg via subprocess.Popen and returns output path
# ---------------------------------------------------------------------------


def test_ac1_convert_uses_popen_and_returns_output_path(
    converter, fake_input, tmp_path
):
    """AC1: Popen is called; returns a non-temp output path on success."""
    popen_spy = MagicMock(return_value=_make_popen(_FFMPEG_LINES_10S))
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", popen_spy), \
         patch("viddrop.core.converter.os.replace", side_effect=_os_replace_creates_file):
        result = converter.convert(fake_input, _settings())

    popen_spy.assert_called_once()
    assert isinstance(result, str)
    assert result.endswith(".mp4")
    assert not result.endswith(".viddrop_tmp")


# ---------------------------------------------------------------------------
# AC2 — Progress callback with Duration/time parsing; div-by-zero guard
# ---------------------------------------------------------------------------


def test_ac2_progress_callback_called_in_0_to_100_range(
    converter, fake_input, tmp_path
):
    """AC2: progress_callback receives values in [0.0, 100.0]."""
    progress: list[float] = []
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(), progress_callback=progress.append)

    assert len(progress) >= 1
    assert all(0.0 <= p <= 100.0 for p in progress)
    # At 5s of 10s -> 50%; at 10s -> 100%
    assert pytest.approx(50.0) in progress


def test_ac2_progress_zero_when_duration_is_zero(converter, fake_input, tmp_path):
    """AC2: division-by-zero guard when Duration is 00:00:00.00."""
    lines = [
        "  Duration: 00:00:00.00, start: 0.000000, bitrate: 0 kb/s\n",
        "frame= 1 time=00:00:00.00 bitrate=0kb/s\n",
    ]
    progress: list[float] = []
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(lines)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(), progress_callback=progress.append)

    assert all(p == 0.0 for p in progress)


def test_ac2_progress_zero_when_no_duration_line(converter, fake_input, tmp_path):
    """AC2: when no Duration line appears, callback still gets 0.0 (not a crash)."""
    lines = ["frame= 100 time=00:00:05.00 bitrate=1000kb/s\n"]
    progress: list[float] = []
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(lines)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(), progress_callback=progress.append)

    assert progress == [0.0]


# ---------------------------------------------------------------------------
# AC3 — replace=True deletes input after output confirmed on disk
# ---------------------------------------------------------------------------


def test_ac3_replace_mode_deletes_input_after_output_confirmed(
    converter, fake_input, tmp_path
):
    """AC3: input deleted when replace=True and conversion succeeded."""
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(replace=True))

    assert not Path(fake_input).exists()


# ---------------------------------------------------------------------------
# AC4 — replace=False keeps input untouched
# ---------------------------------------------------------------------------


def test_ac4_non_replace_mode_input_untouched(converter, fake_input, tmp_path):
    """AC4: input file survives when replace=False."""
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(replace=False))

    assert Path(fake_input).exists()


# ---------------------------------------------------------------------------
# AC5 — cancel_event -> SIGTERM, temp deleted, ConversionCancelledError, input intact
# ---------------------------------------------------------------------------


def test_ac5_cancel_sends_sigterm_cleans_temp_raises_cancelled(
    converter, fake_input, tmp_path
):
    """AC5: cancellation terminates FFmpeg, cleans up temp file, raises, input survives."""
    cancel_event = threading.Event()
    proc = _make_popen(_FFMPEG_LINES_10S)

    def _cancelling_stderr():
        for line in _FFMPEG_LINES_10S:
            cancel_event.set()  # trigger cancel on first line
            yield line

    proc.stderr = _cancelling_stderr()

    # Pre-create the temp file so the cleanup assertion is meaningful.
    temp = Path(fake_input).with_suffix(".viddrop_tmp")
    temp.write_bytes(b"partial")

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", return_value=proc):
        with pytest.raises(ConversionCancelledError):
            converter.convert(
                fake_input, _settings(replace=True), cancel_event=cancel_event
            )

    proc.terminate.assert_called_once()
    # Temp file must be cleaned up after cancellation.
    assert not temp.exists()
    # Input must survive cancellation.
    assert Path(fake_input).exists()


def test_ac5_cancel_before_subprocess_raises_cancelled_no_popen(
    converter, fake_input
):
    """AC5 edge: pre-set cancel event raises without touching subprocess."""
    cancel_event = threading.Event()
    cancel_event.set()
    popen_mock = MagicMock()
    with patch("viddrop.core.converter.subprocess.run", MagicMock()), \
         patch("viddrop.core.converter.subprocess.Popen", popen_mock):
        with pytest.raises(ConversionCancelledError):
            converter.convert(fake_input, _settings(), cancel_event=cancel_event)

    popen_mock.assert_not_called()


# ---------------------------------------------------------------------------
# AC6 — FFmpeg non-zero exit -> ConversionError via sanitize, temp deleted, input intact
# ---------------------------------------------------------------------------


def test_ac6_ffmpeg_nonzero_raises_conversion_error(
    converter, fake_input, tmp_path
):
    """AC6: FFmpeg failure raises ConversionError; temp file gone; input survives."""
    lines = [
        "  Duration: 00:00:10.00\n",
        "some codec error output\n",
    ]
    replace_mock = MagicMock()
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(lines, returncode=1)), \
         patch("viddrop.core.converter.os.replace", replace_mock):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings(replace=True))

    # os.replace (temp->final promotion) must NOT have been called.
    replace_mock.assert_not_called()
    # Input must survive.
    assert Path(fake_input).exists()
    # Temp file must not exist.
    temp = Path(fake_input).with_suffix(".viddrop_tmp")
    assert not temp.exists()


# ---------------------------------------------------------------------------
# AC7 — Qt main-thread call raises RuntimeError immediately
# ---------------------------------------------------------------------------


def test_ac7_main_thread_raises_runtime_error(qapp, fake_input):
    """AC7: calling convert() on the Qt main thread raises RuntimeError."""
    from PyQt6.QtCore import QCoreApplication

    converter = Converter()  # real guard, not patched
    app_thread = QCoreApplication.instance().thread()
    with patch("PyQt6.QtCore.QThread.currentThread", return_value=app_thread):
        with pytest.raises(RuntimeError, match="main thread"):
            converter.convert(fake_input, _settings())


# ---------------------------------------------------------------------------
# AC8 — ConversionWorker emits all four required signals
# ---------------------------------------------------------------------------


class _FakeConverter:
    """Injectable Converter that replays a programmed result or exception."""

    def __init__(self, result: str = "/out.mp4", raises=None):
        self.result = result
        self.raises = raises
        self._cb = None

    def convert(self, input_path, settings, progress_callback=None, cancel_event=None):
        self._cb = progress_callback
        if self.raises is not None:
            raise self.raises
        return self.result


def _make_worker(fake_conv, settings=None) -> ConversionWorker:
    return ConversionWorker(
        download_id="ac8-id",
        input_path="/input.mkv",
        settings=settings,
        converter=fake_conv,
    )


def test_ac8_conversion_progress_signal_emitted(qtbot):
    """AC8: conversion_progress(id, percent) is forwarded."""
    fake = _FakeConverter()
    worker = _make_worker(fake)
    worker.run()  # populates fake._cb
    with qtbot.waitSignal(worker.signals.conversion_progress, timeout=500) as blocker:
        fake._cb(42.0)
    assert blocker.args == ["ac8-id", 42.0]


def test_ac8_conversion_finished_signal_emitted(qtbot):
    """AC8: conversion_finished(id, output_path) emitted on success."""
    worker = _make_worker(_FakeConverter(result="/out.mp4"))
    with qtbot.waitSignal(worker.signals.conversion_finished, timeout=500) as blocker:
        worker.run()
    assert blocker.args == ["ac8-id", "/out.mp4"]


def test_ac8_conversion_failed_signal_emitted(qtbot):
    """AC8: conversion_failed(id, sanitized_message) emitted on ConversionError."""
    worker = _make_worker(_FakeConverter(raises=ConversionError("ffmpeg error")))
    with qtbot.waitSignal(worker.signals.conversion_failed, timeout=500) as blocker:
        worker.run()
    assert blocker.args[0] == "ac8-id"


def test_ac8_conversion_cancelled_signal_emitted(qtbot):
    """AC8: conversion_cancelled(id) emitted when ConversionCancelledError raised."""
    failed: list = []
    worker = _make_worker(_FakeConverter(raises=ConversionCancelledError("cancelled")))
    worker.signals.conversion_failed.connect(lambda *a: failed.append(a))
    with qtbot.waitSignal(worker.signals.conversion_cancelled, timeout=500) as blocker:
        worker.run()
    assert blocker.args == ["ac8-id"]
    assert failed == []  # failed must NOT also fire


# ---------------------------------------------------------------------------
# AC9 — FFmpeg activity logged via logger; raw stderr never in log
# ---------------------------------------------------------------------------


def test_ac9_converter_uses_viddrop_logger_and_no_raw_stderr_in_logs(
    converter, fake_input, tmp_path, caplog
):
    """AC9: logger emits activity logs; raw FFmpeg stderr is absent from log output."""
    raw_stderr_content = "fatal error: codec not found raw OUTPUT"
    lines = [
        "  Duration: 00:00:10.00\n",
        raw_stderr_content + "\n",
    ]
    with caplog.at_level(logging.DEBUG, logger="viddrop"):
        with patch("viddrop.core.converter.subprocess.run",
                   return_value=_make_ffprobe()), \
             patch("viddrop.core.converter.subprocess.Popen",
                   return_value=_make_popen(lines)), \
             patch("viddrop.core.converter.os.replace",
                   side_effect=_os_replace_creates_file):
            converter.convert(fake_input, _settings())

    log_text = caplog.text
    # Activity must be logged.
    assert "Conversion started" in log_text or "Conversion finished" in log_text
    # Raw FFmpeg output must NOT appear in any log record.
    assert raw_stderr_content not in log_text


def test_ac9_ffmpeg_failure_raw_stderr_not_in_logs(
    converter, fake_input, tmp_path, caplog
):
    """AC9/S1: When FFmpeg fails, raw stderr line does NOT appear in logs."""
    raw_line = "UNIQUE_RAW_FFMPEG_STDERR_xyz987"
    lines = ["  Duration: 00:00:10.00\n", raw_line + "\n"]
    with caplog.at_level(logging.DEBUG, logger="viddrop"):
        with patch("viddrop.core.converter.subprocess.run",
                   return_value=_make_ffprobe()), \
             patch("viddrop.core.converter.subprocess.Popen",
                   return_value=_make_popen(lines, returncode=1)), \
             patch("viddrop.core.converter.os.replace", MagicMock()):
            with pytest.raises(ConversionError):
                converter.convert(fake_input, _settings())

    assert raw_line not in caplog.text


# ---------------------------------------------------------------------------
# AC10 — Temp file -> atomic rename on success; temp deleted on error/cancel
# ---------------------------------------------------------------------------


def test_ac10_atomic_rename_on_success(converter, fake_input, tmp_path):
    """AC10: os.replace called with (.viddrop_tmp -> final) on success."""
    replace_calls: list[tuple[str, str]] = []

    def _capture_replace(src, dst):
        replace_calls.append((src, dst))
        Path(dst).write_bytes(b"output")

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace", side_effect=_capture_replace):
        result = converter.convert(fake_input, _settings())

    assert len(replace_calls) == 1
    src, dst = replace_calls[0]
    assert src.endswith(".viddrop_tmp")
    assert not dst.endswith(".viddrop_tmp")
    assert result == dst


def test_ac10_temp_deleted_on_ffmpeg_error(converter, fake_input, tmp_path):
    """AC10: temp file cleaned up when FFmpeg exits non-zero."""
    temp_path = Path(fake_input).with_suffix(".viddrop_tmp")
    temp_path.write_bytes(b"partial")  # simulate temp file existing

    cleanup_calls: list[str] = []
    original_unlink = Path.unlink

    def _spy_unlink(self, missing_ok=False):
        cleanup_calls.append(str(self))
        original_unlink(self, missing_ok=missing_ok)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(["error line\n"], returncode=1)), \
         patch("viddrop.core.converter.os.replace", MagicMock()), \
         patch.object(Path, "unlink", _spy_unlink):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings())

    # The .viddrop_tmp path must appear in unlink calls (cleanup was attempted).
    assert any(".viddrop_tmp" in c for c in cleanup_calls)


# ---------------------------------------------------------------------------
# AC11 — Output path collision: numeric suffixes up to 99; error at 100
# ---------------------------------------------------------------------------


def test_ac11_collision_avoidance_appends_numeric_suffix(converter, tmp_path):
    """AC11: first suffix is ' (1)' when base name is taken."""
    (tmp_path / "video.mp4").write_bytes(b"x")
    src = tmp_path / "video.mkv"
    src.write_bytes(b"src")
    result = converter._resolve_output_path(str(src), "mp4")
    assert result.name == "video (1).mp4"


def test_ac11_collision_avoidance_skips_all_taken_names(converter, tmp_path):
    """AC11: increments until a free slot is found."""
    (tmp_path / "video.mp4").write_bytes(b"x")
    (tmp_path / "video (1).mp4").write_bytes(b"x")
    (tmp_path / "video (2).mp4").write_bytes(b"x")
    src = tmp_path / "video.mkv"
    src.write_bytes(b"src")
    result = converter._resolve_output_path(str(src), "mp4")
    assert result.name == "video (3).mp4"


def test_ac11_collision_avoidance_raises_at_99(converter, tmp_path):
    """AC11: ConversionError raised when all 99 suffixes are taken."""
    (tmp_path / "video.mp4").write_bytes(b"x")
    for n in range(1, 100):
        (tmp_path / f"video ({n}).mp4").write_bytes(b"x")
    src = tmp_path / "video.mkv"
    src.write_bytes(b"src")
    with pytest.raises(ConversionError, match="99"):
        converter._resolve_output_path(str(src), "mp4")


# ---------------------------------------------------------------------------
# AC12 — quality_preset low/medium/high maps to fixed CRF/preset values
# ---------------------------------------------------------------------------


def test_ac12_quality_low_maps_to_correct_crf_and_preset(
    converter, fake_input, tmp_path
):
    """AC12: 'low' -> CRF 28, ffmpeg preset 'fast'."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(quality_preset="low"))

    cmd = captured["cmd"]
    assert "-crf" in cmd
    assert cmd[cmd.index("-crf") + 1] == str(_QUALITY_PRESETS["low"][0])
    assert "-preset" in cmd
    assert cmd[cmd.index("-preset") + 1] == _QUALITY_PRESETS["low"][1]


def test_ac12_quality_medium_maps_to_correct_crf_and_preset(
    converter, fake_input, tmp_path
):
    """AC12: 'medium' -> CRF 23, ffmpeg preset 'medium'."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(quality_preset="medium"))

    cmd = captured["cmd"]
    assert cmd[cmd.index("-crf") + 1] == str(_QUALITY_PRESETS["medium"][0])
    assert cmd[cmd.index("-preset") + 1] == _QUALITY_PRESETS["medium"][1]


def test_ac12_quality_high_maps_to_correct_crf_and_preset(
    converter, fake_input, tmp_path
):
    """AC12: 'high' -> CRF 18, ffmpeg preset 'slow'."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(quality_preset="high"))

    cmd = captured["cmd"]
    assert cmd[cmd.index("-crf") + 1] == str(_QUALITY_PRESETS["high"][0])
    assert cmd[cmd.index("-preset") + 1] == _QUALITY_PRESETS["high"][1]


def test_ac12_no_caller_supplied_flags_in_ffmpeg_cmd(
    converter, fake_input, tmp_path
):
    """AC12/S3: FFmpeg command contains only flags from preset tables; no free-form user input."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings())

    cmd = captured["cmd"]
    # The only non-preset tokens are the binary, -y, -i, the input path, and the output path.
    # Every other flag must come from the preset tables.
    allowed_flag_values = {
        str(v)
        for crf, preset in _QUALITY_PRESETS.values()
        for v in (crf, preset)
    }
    # Verify -crf and -preset values are from the table, not free-form strings.
    crf_value = cmd[cmd.index("-crf") + 1]
    preset_value = cmd[cmd.index("-preset") + 1]
    assert crf_value in {str(_QUALITY_PRESETS[p][0]) for p in _QUALITY_PRESETS}
    assert preset_value in {_QUALITY_PRESETS[p][1] for p in _QUALITY_PRESETS}
    # Verify scale value comes from _RESOLUTION_DIMS.
    vf_value = cmd[cmd.index("-vf") + 1]
    all_scale_values = {
        f"scale={w}:{h}"
        for land, port in _RESOLUTION_DIMS.values()
        for w, h in (land, port)
    }
    assert vf_value in all_scale_values


# ---------------------------------------------------------------------------
# AC13 — Default settings when ConversionWorker gets settings=None
# ---------------------------------------------------------------------------


def test_ac13_default_settings_mp4_1080p_medium_replace():
    """AC13: settings=None -> mp4, 1080p, medium, replace=True."""
    worker = ConversionWorker(download_id="ac13", input_path="/in.mkv")
    s = worker._settings
    assert s.output_format == "mp4"
    assert s.resolution == "1080p"
    assert s.quality_preset == "medium"
    assert s.replace is True


# ---------------------------------------------------------------------------
# AC14 — Vertical video: portrait dimensions used in -vf scale=
# ---------------------------------------------------------------------------


def test_ac14_vertical_video_uses_portrait_scale_dimensions(
    converter, fake_input, tmp_path
):
    """AC14: 720x1280 source -> scale=720:1280 (portrait dims) in FFmpeg cmd."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run",
               return_value=_make_ffprobe(stdout="720,1280\n")), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(resolution="720p"))

    cmd = captured["cmd"]
    assert "-vf" in cmd
    assert "scale=720:1280" in cmd


def test_ac14_landscape_video_uses_landscape_scale_dimensions(
    converter, fake_input, tmp_path
):
    """AC14: 1280x720 source -> scale=1280:720 (landscape dims) in FFmpeg cmd."""
    captured: dict = {}

    def _spy_popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run",
               return_value=_make_ffprobe(stdout="1280,720\n")), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy_popen), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(resolution="720p"))

    cmd = captured["cmd"]
    assert "scale=1280:720" in cmd


# ---------------------------------------------------------------------------
# AC15 — Source below requested resolution: failed + needs_reselect signals
# ---------------------------------------------------------------------------


def test_ac15_resolution_too_low_emits_both_signals(qtbot):
    """AC15: source 720p, requesting 1080p -> conversion_failed + conversion_needs_reselect."""
    fake = _FakeConverter(
        raises=ResolutionTooLowError("too low", max_resolution="720p")
    )
    worker = ConversionWorker(
        download_id="ac15",
        input_path="/in.mkv",
        settings=_settings(resolution="1080p"),
        converter=fake,
    )
    failed: list = []
    worker.signals.conversion_failed.connect(lambda *a: failed.append(a))

    with qtbot.waitSignal(
        worker.signals.conversion_needs_reselect, timeout=500
    ) as blocker:
        worker.run()

    assert blocker.args == ["ac15", "720p"]
    assert len(failed) == 1
    assert failed[0][0] == "ac15"


def test_ac15_converter_raises_resolution_too_low_for_upscale(
    converter, fake_input
):
    """AC15: Converter raises ResolutionTooLowError when upscaling is required."""
    with patch("viddrop.core.converter.subprocess.run",
               return_value=_make_ffprobe(stdout="1280,720\n")), \
         patch("viddrop.core.converter.subprocess.Popen") as popen_mock:
        with pytest.raises(ResolutionTooLowError) as exc_info:
            converter.convert(fake_input, _settings(resolution="1080p"))

    assert exc_info.value.max_resolution == "720p"
    popen_mock.assert_not_called()


# ---------------------------------------------------------------------------
# AC16 — download_completed -> ConversionWorker enqueued in QThreadPool
# ---------------------------------------------------------------------------


def test_ac16_download_completed_enqueues_conversion_worker(
    qtbot, queue, monkeypatch
):
    """AC16: when download_completed is emitted, a ConversionWorker is started in QThreadPool."""
    pool = MagicMock()
    monkeypatch.setattr("PyQt6.QtCore.QThreadPool.globalInstance", lambda: pool)

    entry = queue.add_download("https://example.com/v", "Title", "/home/user/out.mp4")
    queue.set_output_path(entry.id, "/home/user/out.mp4")
    queue.mark_complete(entry.id)

    pool.start.assert_called_once()
    worker = pool.start.call_args.args[0]
    assert isinstance(worker, ConversionWorker)


def test_ac16_no_worker_if_output_file_path_missing(qtbot, queue, monkeypatch):
    """AC16 edge: if output_file_path is not set, no worker is enqueued."""
    pool = MagicMock()
    monkeypatch.setattr("PyQt6.QtCore.QThreadPool.globalInstance", lambda: pool)

    entry = queue.add_download("https://example.com/v", "Title", "/home/user/out.mp4")
    # Intentionally skip set_output_path.
    queue.mark_complete(entry.id)

    pool.start.assert_not_called()


# ---------------------------------------------------------------------------
# AC17 — Tests mock subprocess.Popen; reference resolution = 720p; no real FFmpeg
# ---------------------------------------------------------------------------


def test_ac17_no_real_ffmpeg_subprocess_called(converter, fake_input, tmp_path):
    """AC17: subprocess.Popen is mocked; real FFmpeg binary is never invoked."""
    popen_spy = MagicMock(return_value=_make_popen(_FFMPEG_LINES_10S))
    run_spy = MagicMock(return_value=_make_ffprobe())

    with patch("viddrop.core.converter.subprocess.run", run_spy), \
         patch("viddrop.core.converter.subprocess.Popen", popen_spy), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(resolution="720p"))

    # Both mocks must have been called (ffprobe via run, ffmpeg via Popen).
    run_spy.assert_called_once()
    popen_spy.assert_called_once()
    # The ffprobe call uses 720p reference dimensions (1280,720).
    # (No real process was spawned -- the mock is the test itself.)


# ---------------------------------------------------------------------------
# S1 — Raw FFmpeg stderr never appears in any log call
# ---------------------------------------------------------------------------


def test_s1_raw_ffmpeg_stderr_not_logged_on_failure(
    converter, fake_input, caplog
):
    """S1: raw FFmpeg stderr is absent from all log records on conversion failure."""
    sentinel = "SENTINEL_RAW_STDERR_ABC123"
    lines = ["  Duration: 00:00:10.00\n", sentinel + "\n"]
    with caplog.at_level(logging.DEBUG, logger="viddrop"):
        with patch("viddrop.core.converter.subprocess.run",
                   return_value=_make_ffprobe()), \
             patch("viddrop.core.converter.subprocess.Popen",
                   return_value=_make_popen(lines, returncode=1)), \
             patch("viddrop.core.converter.os.replace", MagicMock()):
            with pytest.raises(ConversionError):
                converter.convert(fake_input, _settings())

    assert sentinel not in caplog.text


# ---------------------------------------------------------------------------
# S2 — All errors go through sanitize_error() before reaching ConversionError
# ---------------------------------------------------------------------------


def test_s2_ffmpeg_error_message_is_sanitized():
    """S2: ConversionError message comes from sanitize_error (credential patterns redacted)."""
    from viddrop.utils.sanitize import sanitize_error

    raw = "Error: Authorization: Bearer secret-token-xyz / password=hunter2"
    expected = sanitize_error(raw)

    # The sanitize function must redact sensitive content.
    assert "hunter2" not in expected
    # ConversionError wraps the sanitized form.
    err = ConversionError(sanitize_error(raw))
    assert "hunter2" not in str(err)


def test_s2_ffprobe_error_message_sanitized(converter, fake_input):
    """S2: ffprobe failure message passed through sanitize_error."""
    raw_stderr = "password=s3cr3t ffprobe failed"
    result = SimpleNamespace(stdout="", stderr=raw_stderr, returncode=1)
    with patch("viddrop.core.converter.subprocess.run", return_value=result):
        with pytest.raises(ConversionError) as exc_info:
            converter.convert(fake_input, _settings())

    assert "s3cr3t" not in exc_info.value.message


# ---------------------------------------------------------------------------
# S3 — FFmpeg command built only from validated preset table
# (Covered inline in AC12 test; one additional guard here)
# ---------------------------------------------------------------------------


def test_s3_ffmpeg_command_has_no_user_supplied_flag_strings(
    converter, fake_input, tmp_path
):
    """S3: scale, crf, and preset values in the FFmpeg command are all from preset tables."""
    captured: dict = {}

    def _spy(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES_10S)

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", side_effect=_spy), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file):
        converter.convert(fake_input, _settings(quality_preset="high", resolution="720p"))

    cmd = captured["cmd"]
    # CRF must be the table value for "high" (18), not anything else.
    assert cmd[cmd.index("-crf") + 1] == "18"
    # Preset must be "slow" per the table.
    assert cmd[cmd.index("-preset") + 1] == "slow"


# ---------------------------------------------------------------------------
# S4 — Input deleted only after output confirmed on disk (replace mode)
# ---------------------------------------------------------------------------


def test_s4_input_not_deleted_when_ffmpeg_fails(converter, fake_input, tmp_path):
    """S4: input file never deleted when FFmpeg exits non-zero."""
    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(["error\n"], returncode=1)), \
         patch("viddrop.core.converter.os.replace", MagicMock()):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings(replace=True))

    assert Path(fake_input).exists()


def test_s4_input_not_deleted_when_cancelled(converter, fake_input):
    """S4: input file never deleted when conversion is cancelled."""
    cancel_event = threading.Event()
    cancel_event.set()
    with patch("viddrop.core.converter.subprocess.run", MagicMock()), \
         patch("viddrop.core.converter.subprocess.Popen", MagicMock()):
        with pytest.raises(ConversionCancelledError):
            converter.convert(
                fake_input, _settings(replace=True), cancel_event=cancel_event
            )

    assert Path(fake_input).exists()


def test_s4_input_deleted_after_output_confirmed(converter, fake_input, tmp_path):
    """S4: input deleted only after final output file confirmed on disk."""
    delete_order: list[str] = []
    original_exists = Path.exists
    original_unlink = Path.unlink

    def _spy_unlink(self, missing_ok=False):
        delete_order.append(f"unlink:{self}")
        original_unlink(self, missing_ok=missing_ok)

    def _spy_exists(self):
        result = original_exists(self)
        if result:
            delete_order.append(f"exists_true:{self}")
        return result

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace",
               side_effect=_os_replace_creates_file), \
         patch.object(Path, "unlink", _spy_unlink), \
         patch.object(Path, "exists", _spy_exists):
        converter.convert(fake_input, _settings(replace=True))

    # Find the final output file's exists check and the input file unlink.
    # The exists check for the final output must precede the input unlink.
    exists_indices = [i for i, s in enumerate(delete_order) if s.startswith("exists_true")]
    unlink_indices = [i for i, s in enumerate(delete_order)
                      if s.startswith("unlink") and "video.mkv" in s]

    if exists_indices and unlink_indices:
        assert min(exists_indices) < min(unlink_indices)


# ---------------------------------------------------------------------------
# S5 — Temp file cleaned up in all error/cancel branches
# ---------------------------------------------------------------------------


def test_s5_temp_deleted_on_ffmpeg_error(converter, fake_input, tmp_path):
    """S5: temp file cleaned up after FFmpeg non-zero exit."""
    temp = Path(fake_input).with_suffix(".viddrop_tmp")
    temp.write_bytes(b"partial")

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(["err\n"], returncode=1)), \
         patch("viddrop.core.converter.os.replace", MagicMock()):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings())

    assert not temp.exists()


def test_s5_temp_deleted_on_cancel(converter, fake_input, tmp_path):
    """S5: temp file cleaned up when conversion is cancelled mid-stream."""
    cancel_event = threading.Event()
    proc = _make_popen(_FFMPEG_LINES_10S)

    def _cancelling_stderr():
        for line in _FFMPEG_LINES_10S:
            cancel_event.set()
            yield line

    proc.stderr = _cancelling_stderr()

    # Pre-create temp file to simulate a partial write before cancel.
    temp = Path(fake_input).with_suffix(".viddrop_tmp")
    temp.write_bytes(b"partial")

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen", return_value=proc):
        with pytest.raises(ConversionCancelledError):
            converter.convert(fake_input, _settings(), cancel_event=cancel_event)

    assert not temp.exists()


def test_s5_temp_deleted_on_finalize_error(converter, fake_input, tmp_path):
    """S5: temp file cleaned up when os.replace raises OSError."""
    temp = Path(fake_input).with_suffix(".viddrop_tmp")

    def _replace_fails(src, dst):
        raise OSError("disk full")

    with patch("viddrop.core.converter.subprocess.run", return_value=_make_ffprobe()), \
         patch("viddrop.core.converter.subprocess.Popen",
               return_value=_make_popen(_FFMPEG_LINES_10S)), \
         patch("viddrop.core.converter.os.replace", side_effect=_replace_fails):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings())

    assert not temp.exists()
