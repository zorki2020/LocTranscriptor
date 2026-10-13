"""Service startup: the web UI on 127.0.0.1 and the queue worker as a separate process.

The address is hard-coded: the service never listens on the network, whatever the
arguments. The access log is off: search URLs contain the
query text. The worker exits together with the server; if the server is killed without
cleanup, the worker puts the interrupted job back in the queue on the next start.

App mode (`serve --app`, started by LocTranscriptor.app): the access key comes as the first
line of stdin (never in arguments or the environment, which other processes can read),
the port may be 0 (any free port), and once the socket listens the service prints exactly
one line to stdout: `{"ready": true, "port": N}`. With `--parent-pid` the service stops
by itself when the app is gone. Models live in `models/` of the data directory.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from transcribator.interpreter import python_command
from transcribator.modelstore import MODELS_ENV, ModelStore, bundled_manifest
from transcribator.paths import data_dir, db_path
from transcribator.storage import db

HOST = "127.0.0.1"
# How many times in a row the server restarts a crashed worker before giving up.
WORKER_RESTARTS = 5
# After the worker has run this many seconds, the crash counter resets.
STABLE_S = 60.0
# Worker exit code when another worker holds the lock (cli.py).
WORKER_BUSY_EXIT = 4
# How often the app-mode service checks that the app that started it is still alive.
PARENT_POLL_S = 1.0
_KEY = re.compile(r"[0-9a-f]{64}")


class BadAccessKey(Exception):
    """The app did not pass a valid access key on stdin."""


class AlreadyRunning(Exception):
    """A service with this data directory is already running."""


def worker_command() -> list[str]:
    return python_command("worker", "--parent-pid", str(os.getpid()))


def read_access_key(stream: IO[str]) -> str:
    """The first stdin line: 64 lowercase hex characters (32 random bytes)."""
    key = stream.readline(200).strip()
    if not _KEY.fullmatch(key):
        raise BadAccessKey
    return key


def listen_socket(port: int) -> socket.socket:
    """A listening socket on 127.0.0.1. Port 0 picks a free one. Connections that come
    before uvicorn starts wait in the backlog."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def watch_parent(parent_pid: int, *, poll_s: float = PARENT_POLL_S) -> threading.Thread:
    """Stop the service (as on SIGTERM: the worker cleans up) once the app is gone."""

    def watch() -> None:
        while os.getppid() == parent_pid:
            time.sleep(poll_s)
        signal.raise_signal(signal.SIGTERM)

    thread = threading.Thread(target=watch, name="parent-watch", daemon=True)
    thread.start()
    return thread


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
                print(
                    f"Worker stopped (code {code}), the queue is not processed",
                    file=sys.stderr,
                    flush=True,
                )
                return
            else:
                restarts += 1
                print(
                    f"Worker crashed (code {code}), restart {restarts}", file=sys.stderr, flush=True
                )
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


_UVICORN_OPTIONS = {
    "access_log": False,
    "proxy_headers": False,
    "server_header": False,
    "date_header": False,
    "log_level": "warning",
}


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
        uvicorn.run(app, host=HOST, port=port, **_UVICORN_OPTIONS)  # type: ignore[arg-type]
    finally:
        keeper.stop()
        lock.close()
    return 0


def run_app_server(port: int, *, parent_pid: int | None, key_stream: IO[str]) -> int:
    """The service of LocTranscriptor.app. See the module docstring."""
    import uvicorn  # noqa: PLC0415

    from transcribator.web.app import create_app  # noqa: PLC0415

    key = read_access_key(key_stream)
    root = data_dir()
    lock = _lock(root)
    db.connect(db_path(root)).close()
    models_root = root / "models"
    # The worker and the recognition child read the models from here.
    os.environ[MODELS_ENV] = str(models_root)
    store = ModelStore(models_root, bundled_manifest())
    sock = listen_socket(port)
    actual = int(sock.getsockname()[1])
    keeper = _WorkerKeeper(worker_command())
    try:
        app = create_app(
            db_file=db_path(root),
            data_root=root,
            port=actual,
            app_mode=True,
            access_key=key,
            models=store,
        )
        if parent_pid is not None:
            watch_parent(parent_pid)
        print(json.dumps({"ready": True, "port": actual}), flush=True)
        config = uvicorn.Config(app, host=HOST, port=actual, **_UVICORN_OPTIONS)  # type: ignore[arg-type]
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        keeper.stop()
        sock.close()
        lock.close()
    return 0


def run_worker(*, parent_pid: int | None = None) -> int:
    from transcribator.modelstore import models_dir  # noqa: PLC0415
    from transcribator.worker.loop import RECOGNITION_LOCK, Worker  # noqa: PLC0415

    models = models_dir()
    # In the app the worker takes jobs only once the models are downloaded.
    ready = ModelStore(models, bundled_manifest()).ready if models is not None else None
    worker = Worker(data_dir(), models_ready=ready, recognition_lock=RECOGNITION_LOCK)
    try:
        worker.run_forever(parent_pid=parent_pid)
    finally:
        worker.close()
    return 0
