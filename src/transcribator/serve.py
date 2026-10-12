"""Service startup: the web UI on 127.0.0.1 and the queue worker as a separate process.

The address is hard-coded: the service never listens on the network, whatever the
arguments. The access log is off: search URLs contain the
query text. The worker exits together with the server; if the server is killed without
cleanup, the worker puts the interrupted job back in the queue on the next start.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from transcribator.paths import data_dir, db_path
from transcribator.storage import db

HOST = "127.0.0.1"
# How many times in a row the server restarts a crashed worker before giving up.
WORKER_RESTARTS = 5
# After the worker has run this many seconds, the crash counter resets.
STABLE_S = 60.0
# Worker exit code when another worker holds the lock (cli.py).
WORKER_BUSY_EXIT = 4


class AlreadyRunning(Exception):
    """A service with this data directory is already running."""


def worker_command() -> list[str]:
    return [sys.executable, "-m", "transcribator", "worker", "--parent-pid", str(os.getpid())]


def _lock(root: Path) -> IO[str]:
    handle = (root / "serve.lock").open("a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise AlreadyRunning from None
    return handle


class _WorkerKeeper:
    """Keeps the worker running: restarts it after a crash (at most N times)."""

    def __init__(self, cmd: list[str], *, poll_s: float = 2.0, busy_wait_s: float = 30.0) -> None:
        self._cmd = cmd
        self._poll_s = poll_s
        self._busy_wait_s = busy_wait_s
        self._stop = threading.Event()
        self._started = time.monotonic()
        self.proc = self._start()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _start(self) -> subprocess.Popen[bytes]:
        self._started = time.monotonic()
        # Our own interpreter and fixed arguments as a list, no shell.
        return subprocess.Popen(self._cmd, stdin=subprocess.DEVNULL)  # noqa: S603

    def _watch(self) -> None:
        restarts = 0
        while not self._stop.wait(self._poll_s):
            code = self.proc.poll()
            if code is None:
                if time.monotonic() - self._started > STABLE_S:
                    restarts = 0  # ran for a minute: earlier crashes no longer count
                continue
            if code == WORKER_BUSY_EXIT:
                # The previous serve's worker is still finishing a job and holds the lock.
                if self._stop.wait(self._busy_wait_s):
                    return
            elif code == 0 or restarts >= WORKER_RESTARTS:
                print(f"Worker stopped (code {code}), the queue is not processed", flush=True)
                return
            else:
                restarts += 1
                print(f"Worker crashed (code {code}), restart {restarts}", flush=True)
            self.proc = self._start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.proc.terminate()
        try:
            self.proc.wait(timeout=60)  # the worker stops recognition and cleans up files
        except subprocess.TimeoutExpired:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)


def run_server(port: int) -> int:
    import uvicorn  # noqa: PLC0415

    from transcribator.web.app import create_app  # noqa: PLC0415

    root = data_dir()
    lock = _lock(root)  # a second serve on the same data would start a second worker
    db.connect(db_path(root)).close()  # the schema is created before the worker starts
    keeper = _WorkerKeeper(worker_command())
    try:
        app = create_app(db_file=db_path(root), data_root=root, port=port)
        print(f"Service: http://{HOST}:{port}", flush=True)
        uvicorn.run(
            app,
            host=HOST,
            port=port,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            log_level="warning",
        )
    finally:
        keeper.stop()
        lock.close()
    return 0


def run_worker(*, parent_pid: int | None = None) -> int:
    from transcribator.worker.loop import Worker  # noqa: PLC0415

    worker = Worker(data_dir())
    try:
        worker.run_forever(parent_pid=parent_pid)
    finally:
        worker.close()
    return 0
