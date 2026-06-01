"""Unit tests for the FFmpeg :class:`Converter`.

No real FFmpeg/ffprobe is ever launched: ``subprocess.run`` (ffprobe) and
``subprocess.Popen`` (ffmpeg) are mocked. The reference resolution throughout is
720p (1280x720 landscape, or 720x1280 portrait).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from viddrop.core.converter import (
    ConversionCancelledError,
    ConversionError,
    ConversionSettings,
    Converter,
    ResolutionTooLowError,
)

_FFMPEG_LINES = [
    "  Duration: 00:00:10.00, start: 0.000000, bitrate: 1000 kb/s\n",
    "frame=  100 fps=50 q=20.0 time=00:00:05.00 bitrate=1000kb/s\n",
    "frame=  200 fps=50 q=20.0 time=00:00:10.00 bitrate=1000kb/s\n",
]


@pytest.fixture
def converter(monkeypatch):
    """A Converter whose main-thread guard is neutralised.

    pytest-qt creates a process-wide ``QApplication``, so without this the
    guard would fire (these tests run on the main thread). The dedicated
    ``test_main_thread_guard`` test exercises the guard explicitly.
    """
    monkeypatch.setattr(
        Converter, "_assert_not_main_thread", lambda self: None
    )
    return Converter()


@pytest.fixture
def fake_input(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"fake")
    return str(f)


def _settings(**overrides) -> ConversionSettings:
    base = {
        "output_format": "mp4",
        "resolution": "720p",
        "quality_preset": "medium",
        "replace": False,
    }
    base.update(overrides)
    return ConversionSettings(**base)


def _make_popen(lines, returncode=0):
    """Build a fake Popen whose .stderr iterates ``lines``."""
    proc = MagicMock()
    proc.stderr = iter(lines)
    proc.returncode = returncode
    proc.wait = MagicMock()
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.send_signal = MagicMock()
    return proc


def _make_ffprobe(stdout="1280,720\n", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _create_output_on_replace(tmp_path=None):
    """os.replace side effect: actually create the destination file."""
    from pathlib import Path

    def _replace(src, dst):
        Path(dst).write_bytes(b"out")

    return _replace


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_happy_path_720p_mp4(converter, fake_input, tmp_path):
    progress: list[float] = []
    settings = _settings()

    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(_FFMPEG_LINES),
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        result = converter.convert(
            fake_input, settings, progress_callback=progress.append
        )

    assert result.endswith(".mp4")
    assert not result.endswith(".viddrop_tmp")
    assert progress  # at least one callback
    assert all(0.0 <= p <= 100.0 for p in progress)


def test_replace_mode_deletes_input(converter, fake_input, tmp_path):
    from pathlib import Path

    settings = _settings(replace=True)
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(_FFMPEG_LINES),
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        converter.convert(fake_input, settings)

    assert not Path(fake_input).exists()


def test_non_replace_mode_keeps_input(converter, fake_input, tmp_path):
    from pathlib import Path

    settings = _settings(replace=False)
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(_FFMPEG_LINES),
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        converter.convert(fake_input, settings)

    assert Path(fake_input).exists()


def test_vertical_video_uses_portrait_dimensions(
    converter, fake_input, tmp_path
):
    captured: dict[str, list[str]] = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _make_popen(_FFMPEG_LINES)

    settings = _settings()
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(stdout="720,1280\n"),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        side_effect=_popen,
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        converter.convert(fake_input, settings)

    cmd = captured["cmd"]
    assert "-vf" in cmd
    assert "scale=720:1280" in cmd


# --------------------------------------------------------------------------- #
# Progress guards
# --------------------------------------------------------------------------- #


def test_progress_zero_division_guard(converter, fake_input, tmp_path):
    progress: list[float] = []
    # No Duration line -> total_seconds stays 0.0
    lines = ["frame= 100 time=00:00:05.00 bitrate=1000kb/s\n"]
    settings = _settings()
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(lines),
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        converter.convert(
            fake_input, settings, progress_callback=progress.append
        )

    assert progress == [0.0]


def test_duration_zero_no_crash(converter, fake_input, tmp_path):
    progress: list[float] = []
    lines = [
        "  Duration: 00:00:00.00, start: 0.000000, bitrate: 0 kb/s\n",
        "frame= 1 time=00:00:00.00 bitrate=0kb/s\n",
    ]
    settings = _settings()
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(lines),
    ), patch(
        "viddrop.core.converter.os.replace",
        side_effect=_create_output_on_replace(tmp_path),
    ):
        converter.convert(
            fake_input, settings, progress_callback=progress.append
        )

    assert progress == [0.0]


# --------------------------------------------------------------------------- #
# Collision avoidance
# --------------------------------------------------------------------------- #


def test_collision_avoidance_first_suffix(converter, tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"existing")
    input_path = tmp_path / "video.mkv"
    input_path.write_bytes(b"src")

    result = converter._resolve_output_path(str(input_path), "mp4")
    assert result.name == "video (1).mp4"


def test_collision_avoidance_cap_raises(converter, tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"x")
    for n in range(1, 100):
        (tmp_path / f"video ({n}).mp4").write_bytes(b"x")
    input_path = tmp_path / "video.mkv"
    input_path.write_bytes(b"src")

    with pytest.raises(ConversionError):
        converter._resolve_output_path(str(input_path), "mp4")


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


def test_ffmpeg_nonzero_raises_conversion_error(converter, fake_input, tmp_path):
    from pathlib import Path

    settings = _settings(replace=True)
    lines = [
        "  Duration: 00:00:10.00, start: 0.0, bitrate: 1000 kb/s\n",
        "Conversion failed: codec error\n",
    ]
    replace_mock = MagicMock()
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen",
        return_value=_make_popen(lines, returncode=1),
    ), patch(
        "viddrop.core.converter.os.replace", replace_mock
    ):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, settings)

    # Temp file should not survive; input must not be deleted.
    tmp_file = tmp_path / "video.viddrop_tmp"
    assert not tmp_file.exists()
    assert Path(fake_input).exists()
    replace_mock.assert_not_called()


def test_ffprobe_failure_raises_conversion_error(converter, fake_input):
    popen_mock = MagicMock()
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(returncode=1),
    ), patch(
        "viddrop.core.converter.subprocess.Popen", popen_mock
    ):
        with pytest.raises(ConversionError):
            converter.convert(fake_input, _settings())

    popen_mock.assert_not_called()


def test_cancel_during_progress_raises_cancelled(
    converter, fake_input, tmp_path
):
    from pathlib import Path

    cancel_event = threading.Event()
    proc = _make_popen(_FFMPEG_LINES)

    # Set the cancel flag as soon as the first stderr line is consumed.
    original_iter = iter(_FFMPEG_LINES)

    def _gen():
        for line in original_iter:
            cancel_event.set()
            yield line

    proc.stderr = _gen()

    settings = _settings(replace=True)
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(),
    ), patch(
        "viddrop.core.converter.subprocess.Popen", return_value=proc
    ):
        with pytest.raises(ConversionCancelledError):
            converter.convert(
                fake_input, settings, cancel_event=cancel_event
            )

    proc.terminate.assert_called_once()
    assert not (tmp_path / "video.viddrop_tmp").exists()
    assert Path(fake_input).exists()


def test_cancel_before_subprocess_starts(converter, fake_input):
    cancel_event = threading.Event()
    cancel_event.set()
    run_mock = MagicMock()
    popen_mock = MagicMock()
    with patch(
        "viddrop.core.converter.subprocess.run", run_mock
    ), patch(
        "viddrop.core.converter.subprocess.Popen", popen_mock
    ):
        with pytest.raises(ConversionCancelledError):
            converter.convert(
                fake_input, _settings(), cancel_event=cancel_event
            )

    run_mock.assert_not_called()
    popen_mock.assert_not_called()


def test_resolution_too_low_raises(converter, fake_input):
    settings = _settings(resolution="1080p")
    with patch(
        "viddrop.core.converter.subprocess.run",
        return_value=_make_ffprobe(stdout="1280,720\n"),
    ), patch(
        "viddrop.core.converter.subprocess.Popen"
    ) as popen_mock:
        with pytest.raises(ResolutionTooLowError) as exc_info:
            converter.convert(fake_input, settings)

    assert exc_info.value.max_resolution == "720p"
    popen_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# Main-thread guard
# --------------------------------------------------------------------------- #


def test_main_thread_guard(qapp, fake_input):
    # The ``qapp`` fixture (pytest-qt) guarantees a QApplication exists so the
    # guard is actually exercised rather than skipped.
    from PyQt6.QtCore import QCoreApplication

    converter = Converter()  # un-patched: exercise the real guard
    app_thread = QCoreApplication.instance().thread()
    with patch(
        "PyQt6.QtCore.QThread.currentThread",
        return_value=app_thread,
    ):
        with pytest.raises(RuntimeError):
            converter.convert(fake_input, _settings())
