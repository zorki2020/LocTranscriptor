from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from transcribator import serve
from transcribator.cli import main
from transcribator.paths import DATA_ENV


class FakePopen:
    instances: list[FakePopen] = []

    def __init__(self, cmd: list[str], **kw: Any) -> None:
        self.cmd = cmd
        self.terminated = False
        FakePopen.instances.append(self)

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return None

    def kill(self) -> None:  # pragma: no cover - not needed in the test
        pass


def test_serve_binds_only_localhost_and_stops_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.setenv(DATA_ENV, str(tmp_path / "data"))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(serve.subprocess, "Popen", FakePopen)
    FakePopen.instances.clear()

    assert main(["serve", "--port", "8799"]) == 0

    assert seen["host"] == "127.0.0.1" and seen["port"] == 8799
    assert seen["access_log"] is False
    worker = FakePopen.instances[0]
    assert worker.cmd[:6] == [
        sys.executable,
        "-m",
        "transcribator",
        "worker",
        "--parent-pid",
        str(os.getpid()),
    ]
    assert worker.terminated
    assert (tmp_path / "data" / "transcribator.sqlite3").exists()


def test_serve_rejects_privileged_port() -> None:
    with pytest.raises(SystemExit):
        main(["serve", "--port", "80"])


def test_second_serve_on_same_data_is_refused(tmp_path: Path) -> None:
    lock = serve._lock(tmp_path)
    with pytest.raises(serve.AlreadyRunning):
        serve._lock(tmp_path)
    lock.close()
    serve._lock(tmp_path).close()


def test_foreign_non_empty_data_dir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from transcribator.paths import DataDirError, data_dir

    (tmp_path / "чужой файл.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(DATA_ENV, str(tmp_path))
    mode = tmp_path.stat().st_mode
    with pytest.raises(DataDirError):
        data_dir()
    assert tmp_path.stat().st_mode == mode


def test_own_service_dir_without_database_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from transcribator.paths import data_dir

    (tmp_path / "uploads").mkdir()
    (tmp_path / "work").mkdir()
    monkeypatch.setenv(DATA_ENV, str(tmp_path))
    assert data_dir() == tmp_path


class ScriptedPopen:
    """A worker that exits with the given codes in turn, then keeps running."""

    codes: list[int | None] = []
    started = 0

    def __init__(self, cmd: list[str], **kw: Any) -> None:
        ScriptedPopen.started += 1
        self._code = ScriptedPopen.codes.pop(0) if ScriptedPopen.codes else None

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover
        pass


def _keeper(monkeypatch: pytest.MonkeyPatch, codes: list[int | None]) -> serve._WorkerKeeper:
    ScriptedPopen.codes = list(codes)
    ScriptedPopen.started = 0
    monkeypatch.setattr(serve.subprocess, "Popen", ScriptedPopen)
    return serve._WorkerKeeper(["worker"], poll_s=0.001, busy_wait_s=0.001)


def test_keeper_restarts_crashed_worker_up_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    keeper = _keeper(monkeypatch, [1] * 10)
    keeper._thread.join(timeout=5)
    assert ScriptedPopen.started == serve.WORKER_RESTARTS + 1


def test_keeper_waits_out_busy_lock_without_counting(monkeypatch: pytest.MonkeyPatch) -> None:
    keeper = _keeper(monkeypatch, [serve.WORKER_BUSY_EXIT] * 8 + [None])
    for _ in range(500):
        if ScriptedPopen.started >= 9:
            break
        time.sleep(0.01)
    keeper.stop()
    assert ScriptedPopen.started >= 9  # above the crash limit: "busy" is not a crash


def test_serve_already_running_exits_zero_so_launchd_does_not_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_ENV, str(tmp_path))
    lock = serve._lock(tmp_path)
    try:
        assert main(["serve"]) == 0
    finally:
        lock.close()
