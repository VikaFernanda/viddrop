"""Unit tests for the Downloader.

yt-dlp is fully mocked: no real network calls are ever made. A FakeYoutubeDL
captures the opts dict and drives the registered progress hook so we can
exercise pause/resume/cancel behaviour deterministically.
"""

from __future__ import annotations

import threading
import time

import pytest

from viddrop.core import downloader as downloader_mod
from viddrop.core.downloader import (
    DownloadCancelledError,
    DownloadError,
    Downloader,
)


class FakeYoutubeDL:
    """Records the opts and replays a configurable hook script on download()."""

    # Class-level recorder so tests can inspect the most recent instance.
    last_opts: dict | None = None

    # Hook events to emit per download() call: list of progress dicts.
    hook_script: list[dict] = []

    def __init__(self, opts: dict) -> None:
        FakeYoutubeDL.last_opts = opts
        self._opts = opts

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def download(self, urls: list[str]) -> None:
        hooks = self._opts.get("progress_hooks", [])
        for event in FakeYoutubeDL.hook_script:
            for hook in hooks:
                hook(event)


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYoutubeDL.last_opts = None
    FakeYoutubeDL.hook_script = []
    monkeypatch.setattr(downloader_mod.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    return FakeYoutubeDL


# ----------------------------------------------------------------------- #
# Happy path
# ----------------------------------------------------------------------- #


def test_happy_path_no_credentials(fake_ydl, tmp_path):
    received: list[dict] = []
    out_file = tmp_path / "Video.mp4"
    fake_ydl.hook_script = [
        {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100},
        {"status": "finished", "filename": str(out_file)},
    ]

    dl = Downloader()
    result = dl.start(
        "https://example.com/v",
        str(tmp_path),
        credentials=None,
        progress_callback=received.append,
    )

    assert result == str(out_file.resolve())
    assert len(received) == 2


def test_no_credentials_omits_keys(fake_ydl, tmp_path):
    fake_ydl.hook_script = [{"status": "finished", "filename": str(tmp_path / "v.mp4")}]
    Downloader().start("https://example.com/v", str(tmp_path), credentials=None)
    assert "username" not in fake_ydl.last_opts
    assert "password" not in fake_ydl.last_opts


def test_credentials_injected_via_python_api(fake_ydl, tmp_path):
    fake_ydl.hook_script = [{"status": "finished", "filename": str(tmp_path / "v.mp4")}]
    Downloader().start(
        "https://example.com/v",
        str(tmp_path),
        credentials={"username": "u", "password": "p"},
    )
    assert fake_ydl.last_opts["username"] == "u"
    assert fake_ydl.last_opts["password"] == "p"
    # Quiet/no_warnings are always set; no subprocess CLI args present.
    assert fake_ydl.last_opts["quiet"] is True
    assert fake_ydl.last_opts["no_warnings"] is True


# ----------------------------------------------------------------------- #
# Failure path
# ----------------------------------------------------------------------- #


def test_missing_dest_path_raises_before_download(fake_ydl, tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(DownloadError):
        Downloader().start("https://example.com/v", str(missing))
    # YoutubeDL must not have been constructed.
    assert fake_ydl.last_opts is None


def test_ydl_error_is_redacted_and_capped(monkeypatch, tmp_path):
    long_secret = "x" * 600

    class RaisingYDL:
        def __init__(self, opts: dict) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def download(self, urls):
            raise RuntimeError(f"login failed --password hunter2 {long_secret}")

    monkeypatch.setattr(downloader_mod.yt_dlp, "YoutubeDL", RaisingYDL)

    with pytest.raises(DownloadError) as exc_info:
        Downloader().start("https://example.com/v", str(tmp_path))

    msg = str(exc_info.value)
    assert "--password hunter2" not in msg
    assert "<redacted>" in msg
    assert len(msg) <= 500


def test_finished_without_filename_raises(fake_ydl, tmp_path):
    # "finished" event with no filename -> no captured path -> error.
    fake_ydl.hook_script = [{"status": "finished"}]
    with pytest.raises(DownloadError):
        Downloader().start("https://example.com/v", str(tmp_path))


# ----------------------------------------------------------------------- #
# Edge cases
# ----------------------------------------------------------------------- #


def test_empty_progress_dict_no_keyerror(fake_ydl, tmp_path):
    received: list[dict] = []
    fake_ydl.hook_script = [
        {},  # sparse dict must not raise
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]
    Downloader().start(
        "https://example.com/v",
        str(tmp_path),
        progress_callback=received.append,
    )
    assert received[0] == {
        "percent": 0.0,
        "speed": "N/A",
        "eta": "N/A",
        "status": "N/A",
    }


def test_progress_callback_normalized_keys(fake_ydl, tmp_path):
    received: list[dict] = []
    fake_ydl.hook_script = [
        {
            "status": "downloading",
            "_percent_str": " 42.1%",
            "_speed_str": "1.2MiB/s",
            "_eta_str": "00:03",
        },
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]
    Downloader().start(
        "https://example.com/v",
        str(tmp_path),
        progress_callback=received.append,
    )
    assert received[0] == {
        "percent": 42.1,
        "speed": "1.2MiB/s",
        "eta": "00:03",
        "status": "downloading",
    }


def test_cancel_during_download_raises(fake_ydl, tmp_path):
    dl = Downloader()

    def cancel_on_first(info: dict) -> None:
        dl.cancel()

    fake_ydl.hook_script = [
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100},
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]
    with pytest.raises(DownloadCancelledError):
        dl.start(
            "https://example.com/v",
            str(tmp_path),
            progress_callback=cancel_on_first,
        )


def test_pause_then_resume_completes(fake_ydl, tmp_path):
    dl = Downloader()
    dl.pause()  # paused before start
    fake_ydl.hook_script = [
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100},
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]

    result: dict[str, str] = {}

    def run() -> None:
        result["path"] = dl.start("https://example.com/v", str(tmp_path))

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)  # hook is blocked on the pause wait
    assert t.is_alive()
    dl.resume()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["path"].endswith("v.mp4")


def test_cancel_while_paused_raises_quickly(fake_ydl, tmp_path):
    dl = Downloader()
    dl.pause()
    fake_ydl.hook_script = [
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100},
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]

    error: dict[str, BaseException] = {}

    def run() -> None:
        try:
            dl.start("https://example.com/v", str(tmp_path))
        except BaseException as exc:  # noqa: BLE001 - capture for assertion
            error["exc"] = exc

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)  # ensure hook is parked in the pause loop
    start = time.monotonic()
    dl.cancel()
    t.join(timeout=2)
    elapsed = time.monotonic() - start

    assert not t.is_alive()
    assert isinstance(error.get("exc"), DownloadCancelledError)
    assert elapsed < 2


def test_cancel_removes_partial_files(fake_ydl, tmp_path):
    # Simulate a leftover yt-dlp partial download in the destination dir.
    partial = tmp_path / "video.part"
    partial.write_text("incomplete data")
    assert partial.exists()

    dl = Downloader()

    def cancel_on_first(info: dict) -> None:
        dl.cancel()

    fake_ydl.hook_script = [
        {"status": "downloading", "downloaded_bytes": 1, "total_bytes": 100},
        {"status": "finished", "filename": str(tmp_path / "video.mp4")},
    ]
    with pytest.raises(DownloadCancelledError):
        dl.start(
            "https://example.com/v",
            str(tmp_path),
            progress_callback=cancel_on_first,
        )

    # _cleanup_partials must have actually removed the .part file.
    assert not partial.exists()


def test_credentials_never_logged_in_downloader(fake_ydl, tmp_path, monkeypatch):
    captured_logs: list[str] = []

    def record(*args, **kwargs) -> None:
        # Render the message the way logging would (format string + args).
        if args:
            template = args[0]
            params = args[1:]
            try:
                captured_logs.append(str(template) % params if params else str(template))
            except (TypeError, ValueError):
                captured_logs.append(str(template))
            captured_logs.extend(str(a) for a in params)

    for level in ("debug", "info", "warning", "error", "critical", "exception"):
        monkeypatch.setattr(downloader_mod.log, level, record)

    fake_ydl.hook_script = [
        {"status": "finished", "filename": str(tmp_path / "v.mp4")},
    ]
    Downloader().start(
        "https://example.com/v",
        str(tmp_path),
        credentials={"username": "alice", "password": "hunter2"},
    )

    joined = "\n".join(captured_logs)
    assert "hunter2" not in joined
    assert "alice" not in joined
