"""FFmpeg conversion backend for Viddrop.

A :class:`Converter` wraps a single FFmpeg conversion. :meth:`Converter.convert`
is blocking and MUST be invoked from a worker thread (never the Qt main thread);
a guard enforces this when a ``QApplication`` exists.

Security:
- FFmpeg / ffprobe commands are built only from a validated preset table; no
  caller-supplied flags are ever interpolated into the argument list.
- Raw FFmpeg / ffprobe stderr is NEVER logged and never surfaced directly: it is
  always passed through :func:`viddrop.utils.sanitize.sanitize_error` before
  being placed in a :class:`ConversionError` message.
- File paths are logged only at DEBUG level, never at INFO.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from viddrop.utils.logger import log
from viddrop.utils.sanitize import sanitize_error

ProgressCallback = Callable[[float], None]  # receives 0.0-100.0


# --------------------------------------------------------------------------- #
# Settings and exceptions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConversionSettings:
    """Immutable description of a single conversion request."""

    output_format: str  # "mp4" | "mkv" | "mov"
    resolution: str  # "360p" | "480p" | "720p" | "1080p" | "1440p" | "2160p"
    quality_preset: str  # "low" | "medium" | "high"
    replace: bool  # True = delete input on success


class ConversionError(Exception):
    """Raised when a conversion fails. Message is always sanitized."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConversionCancelledError(ConversionError):
    """Raised when a conversion is cancelled via the supplied cancel event."""


class ResolutionTooLowError(ConversionError):
    """Raised when the source video is smaller than the requested resolution."""

    def __init__(self, message: str, max_resolution: str) -> None:
        super().__init__(message)
        self.max_resolution = max_resolution


# --------------------------------------------------------------------------- #
# Module-level tables and helpers
# --------------------------------------------------------------------------- #

# Ordered list of supported resolutions, ascending.
_RESOLUTION_ORDER: tuple[str, ...] = (
    "360p",
    "480p",
    "720p",
    "1080p",
    "1440p",
    "2160p",
)

# resolution -> (landscape (w, h), portrait (w, h))
_RESOLUTION_DIMS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "360p": ((640, 360), (360, 640)),
    "480p": ((854, 480), (480, 854)),
    "720p": ((1280, 720), (720, 1280)),
    "1080p": ((1920, 1080), (1080, 1920)),
    "1440p": ((2560, 1440), (1440, 2560)),
    "2160p": ((3840, 2160), (2160, 3840)),
}

# quality_preset -> (crf, ffmpeg -preset)
_QUALITY_PRESETS: dict[str, tuple[int, str]] = {
    "low": (28, "fast"),
    "medium": (23, "medium"),
    "high": (18, "slow"),
}

# quality_preset -> audio bitrate
_AUDIO_BITRATES: dict[str, str] = {
    "low": "128k",
    "medium": "192k",
    "high": "256k",
}

# output_format -> audio codec
_AUDIO_CODECS: dict[str, str] = {
    "mp4": "aac",
    "mov": "aac",
    "mkv": "libvorbis",
}

_DURATION_RE = re.compile(r"Duration:\s+(\d{2}):(\d{2}):(\d{2})\.(\d{2})")
_TIME_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")


def _find_ffmpeg_binary(name: str) -> str:
    """Locate an FFmpeg-family binary.

    Prefers a binary bundled inside a PyInstaller/AppImage build (``sys._MEIPASS``)
    and falls back to the system ``PATH``. Raises :class:`ConversionError` if the
    binary cannot be found.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / name
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise ConversionError(f"{name} not found")


# --------------------------------------------------------------------------- #
# Converter
# --------------------------------------------------------------------------- #


class Converter:
    """Drives a single FFmpeg conversion with progress and cancellation."""

    def convert(
        self,
        input_path: str,
        settings: ConversionSettings,
        progress_callback: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Convert ``input_path`` per ``settings`` and return the output path.

        Blocking. MUST NOT run on the Qt main thread. Returns the absolute path
        of the produced output file.
        """
        self._assert_not_main_thread()

        # Honour an already-set cancel event before doing any work.
        if cancel_event is not None and cancel_event.is_set():
            raise ConversionCancelledError("Conversion cancelled")

        source_width, source_height = self._probe_dimensions(input_path)
        self._assert_resolution_fits(
            settings.resolution, source_width, source_height
        )

        final_path = self._resolve_output_path(
            input_path, settings.output_format
        )
        tmp_path = final_path.with_suffix(".viddrop_tmp")

        cmd = self._build_ffmpeg_cmd(
            input_path, tmp_path, settings, source_width, source_height
        )

        log.info("Conversion started: format=%s", settings.output_format)
        log.debug("Conversion input path: %s", input_path)

        self._run_ffmpeg(cmd, progress_callback, cancel_event, tmp_path)

        # Promote temp file to final, then confirm it landed.
        try:
            os.replace(str(tmp_path), str(final_path))
        except OSError as exc:
            self._cleanup_temp(tmp_path)
            raise ConversionError(
                sanitize_error(f"Failed to finalize output: {exc}")
            ) from exc

        if not final_path.exists():
            raise ConversionError("Conversion produced no output file")

        if settings.replace:
            self._delete_input(input_path)

        log.info("Conversion finished: format=%s", settings.output_format)
        return str(final_path)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _assert_not_main_thread(self) -> None:
        """Raise ``RuntimeError`` if running on the Qt main thread.

        When no ``QApplication`` exists (e.g. a pure unit test), the check is
        skipped because there is no main thread to protect.
        """
        from PyQt6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is None:
            return
        if QThread.currentThread() is app.thread():
            raise RuntimeError(
                "Converter.convert must not run on the Qt main thread"
            )

    def _probe_dimensions(self, input_path: str) -> tuple[int, int]:
        """Return ``(width, height)`` of the first video stream via ffprobe."""
        ffprobe = _find_ffmpeg_binary("ffprobe")
        cmd = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            input_path,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise ConversionError(
                sanitize_error(f"ffprobe failed to start: {exc}")
            ) from exc

        if proc.returncode != 0:
            log.warning("ffprobe exited non-zero: code=%s", proc.returncode)
            raise ConversionError(
                sanitize_error(f"ffprobe failed: {proc.stderr}")
            )

        raw = (proc.stdout or "").strip()
        parts = raw.split(",")
        try:
            width = int(parts[0])
            height = int(parts[1])
        except (IndexError, ValueError) as exc:
            raise ConversionError(
                sanitize_error(f"Could not parse video dimensions: {raw!r}")
            ) from exc
        return width, height

    def _assert_resolution_fits(
        self, resolution: str, source_width: int, source_height: int
    ) -> None:
        """Raise :class:`ResolutionTooLowError` if upscaling would be required."""
        is_vertical = source_height > source_width
        landscape, portrait = _RESOLUTION_DIMS[resolution]
        target_w, target_h = portrait if is_vertical else landscape

        # Dominant axis: height for landscape, width for vertical.
        if is_vertical:
            source_dominant = source_width
            target_dominant = target_w
        else:
            source_dominant = source_height
            target_dominant = target_h

        if source_dominant < target_dominant:
            max_resolution = self._largest_fitting_resolution(
                source_width, source_height, is_vertical
            )
            raise ResolutionTooLowError(
                "Source resolution is lower than the requested resolution",
                max_resolution=max_resolution,
            )

    def _largest_fitting_resolution(
        self, source_width: int, source_height: int, is_vertical: bool
    ) -> str:
        """Return the highest preset resolution that does not upscale."""
        source_dominant = source_width if is_vertical else source_height
        for resolution in reversed(_RESOLUTION_ORDER):
            landscape, portrait = _RESOLUTION_DIMS[resolution]
            target_w, target_h = portrait if is_vertical else landscape
            target_dominant = target_w if is_vertical else target_h
            if source_dominant >= target_dominant:
                return resolution
        # Source is smaller than even the lowest preset; offer the lowest.
        return _RESOLUTION_ORDER[0]

    def _resolve_output_path(self, input_path: str, output_format: str) -> Path:
        """Return a non-colliding output path, capped at 99 numeric suffixes."""
        stem = Path(input_path).stem
        suffix = f".{output_format}"
        directory = Path(input_path).parent
        candidate = directory / f"{stem}{suffix}"
        if not candidate.exists():
            return candidate
        for n in range(1, 100):
            candidate = directory / f"{stem} ({n}){suffix}"
            if not candidate.exists():
                return candidate
        raise ConversionError(
            "Could not find a free output path after 99 attempts"
        )

    def _build_ffmpeg_cmd(
        self,
        input_path: str,
        tmp_path: Path,
        settings: ConversionSettings,
        source_width: int,
        source_height: int,
    ) -> list[str]:
        """Build the FFmpeg argument list from the validated preset tables."""
        ffmpeg = _find_ffmpeg_binary("ffmpeg")

        is_vertical = source_height > source_width
        landscape, portrait = _RESOLUTION_DIMS[settings.resolution]
        target_w, target_h = portrait if is_vertical else landscape

        try:
            crf, preset = _QUALITY_PRESETS[settings.quality_preset]
            audio_codec = _AUDIO_CODECS[settings.output_format]
            audio_bitrate = _AUDIO_BITRATES[settings.quality_preset]
        except KeyError as exc:
            raise ConversionError(
                f"Unsupported format or quality preset: {exc}"
            ) from None

        return [
            ffmpeg,
            "-y",
            "-i",
            input_path,
            "-vf",
            f"scale={target_w}:{target_h}",
            "-c:v",
            "libx264",
            "-crf",
            str(crf),
            "-preset",
            preset,
            "-c:a",
            audio_codec,
            "-b:a",
            audio_bitrate,
            str(tmp_path),
        ]

    def _run_ffmpeg(
        self,
        cmd: list[str],
        progress_callback: ProgressCallback | None,
        cancel_event: threading.Event | None,
        tmp_path: Path,
    ) -> None:
        """Run FFmpeg, parsing progress and honouring cancellation."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            self._cleanup_temp(tmp_path)
            raise ConversionError(
                sanitize_error(f"FFmpeg failed to start: {exc}")
            ) from exc

        collected_stderr: list[str] = []
        total_seconds = 0.0

        stderr = proc.stderr
        if stderr is not None:
            for line in stderr:
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate(proc)
                    self._cleanup_temp(tmp_path)
                    raise ConversionCancelledError("Conversion cancelled")

                collected_stderr.append(line)

                if "Duration:" in line and total_seconds == 0.0:
                    total_seconds = self._parse_duration(line) or 0.0

                if "time=" in line:
                    pos = self._parse_time_position(line)
                    if pos is not None:
                        if total_seconds > 0.0:
                            percent = min(pos / total_seconds * 100.0, 100.0)
                        else:
                            percent = 0.0
                        if progress_callback is not None:
                            progress_callback(percent)

        proc.wait()

        if proc.returncode != 0:
            log.warning("FFmpeg exited non-zero: code=%s", proc.returncode)
            self._cleanup_temp(tmp_path)
            raise ConversionError(
                sanitize_error("".join(collected_stderr))
            )

    def _terminate(self, proc: subprocess.Popen[str]) -> None:
        """Send SIGTERM to a running FFmpeg process, ignoring failures."""
        try:
            proc.terminate()
        except (OSError, ProcessLookupError):
            # Process already gone; nothing to do.
            pass
        else:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.send_signal(signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass

    def _parse_duration(self, line: str) -> float | None:
        """Parse total duration in seconds from an FFmpeg ``Duration:`` line."""
        match = _DURATION_RE.search(line)
        if match is None:
            return None
        return self._hms_to_seconds(match)

    def _parse_time_position(self, line: str) -> float | None:
        """Parse the current time position in seconds from a ``time=`` token."""
        match = _TIME_RE.search(line)
        if match is None:
            return None
        return self._hms_to_seconds(match)

    @staticmethod
    def _hms_to_seconds(match: re.Match[str]) -> float:
        h, m, s, cs = (int(g) for g in match.groups())
        return h * 3600 + m * 60 + s + cs / 100

    def _cleanup_temp(self, tmp_path: Path) -> None:
        """Remove a temp file, swallowing OS errors (best-effort cleanup)."""
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to clean up temp file: %s", exc)

    def _delete_input(self, input_path: str) -> None:
        """Delete the source file after a successful replace conversion."""
        try:
            Path(input_path).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Failed to delete source after replace: %s", exc)
