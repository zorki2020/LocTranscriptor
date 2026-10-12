from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from tests.conftest import requires_ffmpeg
from transcribator.pipeline.tempfiles import PREFIX, cleanup_stale, job_prefix


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_job_prefix_carries_pid() -> None:
    assert job_prefix() == f"{PREFIX}{os.getpid()}-"
    assert job_prefix("bench") == f"{PREFIX}bench-{os.getpid()}-"


def test_cleanup_removes_only_dirs_of_dead_processes(tmp_path: Path) -> None:
    dead = _dead_pid()
    stale = tmp_path / f"{PREFIX}{dead}-abc"
    stale.mkdir()
    (stale / "audio.wav").write_bytes(b"\0" * 10)
    stale_bench = tmp_path / f"{PREFIX}bench-{dead}-x"
    stale_bench.mkdir()
    mine = tmp_path / f"{job_prefix()}abc"
    mine.mkdir()
    parent = tmp_path / f"{PREFIX}{os.getppid()}-abc"  # a live process
    parent.mkdir()
    legacy = tmp_path / f"{PREFIX}abc"  # no PID: not ours, leave it alone
    legacy.mkdir()
    other = tmp_path / "something-else"
    other.mkdir()

    assert cleanup_stale(tmp_path) == 2

    assert not stale.exists()
    assert not stale_bench.exists()
    assert mine.exists() and parent.exists() and legacy.exists() and other.exists()


def test_cleanup_ignores_files_and_symlinks(tmp_path: Path) -> None:
    dead = _dead_pid()
    (tmp_path / f"{PREFIX}{dead}-file").write_text("x", encoding="utf-8")
    target = tmp_path / "keep"
    target.mkdir()
    (tmp_path / f"{PREFIX}{dead}-link").symlink_to(target)

    assert cleanup_stale(tmp_path) == 0
    assert target.exists()


def test_cleanup_of_missing_root_is_noop(tmp_path: Path) -> None:
    assert cleanup_stale(tmp_path / "nope") == 0


SLOW_JOB = """
import sys, time
from pathlib import Path
from transcribator.backends.fake import FakeDiarizer
from transcribator.cli import _exit_cleanly_on_signals
from transcribator.pipeline.engine import LocalEngine

class SlowTranscriber:
    def transcribe(self, wav_path, turns):
        print("ready", flush=True)
        time.sleep(60)
        return []

_exit_cleanly_on_signals()
LocalEngine(SlowTranscriber(), FakeDiarizer([]), Path(sys.argv[2])).analyze(Path(sys.argv[1]))
"""


@requires_ffmpeg
def test_sigterm_removes_job_temp_dir(sine_audio: Path, tmp_path: Path) -> None:
    """No temporary WAV is left behind even on SIGTERM."""
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-c", SLOW_JOB, str(sine_audio), str(work)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    assert any(p.name.startswith(PREFIX) for p in work.iterdir())  # the job directory exists

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=20)

    assert proc.returncode == 128 + signal.SIGTERM
    assert list(work.iterdir()) == []
