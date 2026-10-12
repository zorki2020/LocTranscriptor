"""Worker loop.

One meeting at a time:
1. Take a job from the queue.
2. Transcribe the recording in an isolated child process (`pipeline/isolated.py`: no
   network, lowered priority). The child writes the transcript as JSON to a temporary
   folder inside the data directory.
3. Save the result in one transaction, delete the JSON and the temporary upload copy.

The Mac stays awake for the whole job (`caffeinate -i`). Errors are stored as codes
only. There is no summary: the user needs only the transcript.
On startup, jobs interrupted by a crash go back to the queue, and leftover temporary
files and forgotten upload copies are deleted.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, TypeVar

from transcribator.backends.errors import BackendError
from transcribator.models import Transcript
from transcribator.paths import db_path, uploads_dir, work_dir
from transcribator.pipeline.audio import AudioError
from transcribator.pipeline.isolated import keep_awake, kill_orphan_group, run_isolated
from transcribator.pipeline.tempfiles import cleanup_stale, job_prefix
from transcribator.storage import db, repo

PROCESS_TIMEOUT_S = 4 * 3600
# An upload copy with no meeting older than this is an orphan (an aborted upload).
ORPHAN_UPLOAD_AGE_S = 3600
# After an error, a dropped recording's copy waits for "Retry" at most this long
# (dropped recordings are temporary copies).
FAILED_UPLOAD_TTL = timedelta(days=7)
DB_RETRIES = 3
SWEEP_S = 3600.0
# Upload copy name: a random hex name only, no paths (same as in web/app.py).
_UPLOAD_REF = re.compile(r"[0-9a-f]{32}")

OnStart = Callable[[int], None]
Processor = Callable[[Path, Path, OnStart, str], Transcript]
T = TypeVar("T")


class WorkerBusy(Exception):
    """Another worker is already running with this data directory."""


class JobError(Exception):
    """A job failure. Code only."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def process_isolated(src: Path, work: Path, on_start: OnStart, language: str = "en") -> Transcript:
    """Transcribe a recording in a child process under a network-less sandbox. `on_start`
    receives the child's PGID: the worker stores it to stop the orphan after a crash."""
    out = work / "transcript.json"
    metrics = work / "metrics.json"
    cmd = [
        sys.executable,
        "-m",
        "transcribator",
        "process",
        str(src),
        "--language",
        language,
        "--json",
        str(out),
        "--metrics-json",
        str(metrics),
    ]
    child = run_isolated(cmd, timeout_s=PROCESS_TIMEOUT_S, low_priority=True, on_start=on_start)
    if child.timed_out:
        raise JobError("timeout")
    if child.returncode != 0:
        try:
            code = str(json.loads(metrics.read_text(encoding="utf-8"))["error"])
        except (OSError, ValueError, KeyError, TypeError):
            code = f"process_exit_{child.returncode}"
        raise JobError(code)
    try:
        return Transcript.from_dict(json.loads(out.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        raise JobError("bad_result") from None


class Worker:
    def __init__(
        self,
        data_root: Path,
        *,
        processor: Processor = process_isolated,
        keep_awake: bool = True,
    ) -> None:
        self._root = data_root
        self._processor = processor
        self._keep_awake = keep_awake
        self._conn = db.connect(db_path(data_root))
        self._lock: IO[str] | None = None

    def close(self) -> None:
        self._conn.close()
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    def acquire_lock(self) -> None:
        """One worker per data directory: a second one would requeue the other's job and
        start a second recognition on 16 GB. The lock is released when the process exits."""
        handle = (self._root / "worker.lock").open("a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise WorkerBusy from None
        self._lock = handle

    # --- startup --------------------------------------------------------------------

    def recover(self) -> None:
        """Clean up after a crash: stop the previous worker's children, requeue jobs,
        delete leftover files and expired upload copies."""
        for pgid in repo.interrupted_children(self._conn):
            kill_orphan_group(pgid)
        repo.requeue_interrupted(self._conn)
        cleanup_stale()
        cleanup_stale(work_dir(self._root))
        referenced = repo.upload_refs(self._conn)
        now = time.time()
        for entry in uploads_dir(self._root).iterdir():
            if entry.name in referenced or not entry.is_file():
                continue
            with contextlib.suppress(OSError):
                if now - entry.lstat().st_mtime > ORPHAN_UPLOAD_AGE_S:
                    entry.unlink()
        self.recover_uploads()

    def recover_uploads(self) -> None:
        """Delete upload copies of failed meetings past their retention (temporary copies)."""
        before = (datetime.now(UTC) - FAILED_UPLOAD_TTL).isoformat(timespec="seconds")
        for ref in repo.expired_failed_uploads(self._conn, before):
            path = uploads_dir(self._root) / ref
            with contextlib.suppress(OSError):
                if path.parent == uploads_dir(self._root) and path.is_file():
                    path.unlink()

    # --- one job --------------------------------------------------------------------

    def run_once(self) -> bool:
        """Process one job. False if the queue is empty."""
        job = repo.claim_next(self._conn)
        if job is None:
            return False
        with keep_awake() if self._keep_awake else contextlib.nullcontext():
            self._run(job)
        return True

    def _source(self, job: repo.Job) -> Path:
        if job.source_kind == repo.SOURCE_UPLOAD:
            if not _UPLOAD_REF.fullmatch(job.source_ref):
                raise JobError("bad_upload_ref")
            return uploads_dir(self._root) / job.source_ref
        return Path(job.source_ref)

    def _run(self, job: repo.Job) -> None:
        try:
            src = self._source(job)
            if not src.is_file():
                raise JobError("source_missing")
            repo.set_stage(self._conn, job.id, "transcribe")
            with tempfile.TemporaryDirectory(prefix=job_prefix(), dir=work_dir(self._root)) as tmp:
                transcript = self._processor(
                    src,
                    Path(tmp),
                    lambda pgid: repo.set_child(self._conn, job.id, pgid),
                    job.language,
                )
            repo.set_child(self._conn, job.id, None)
        except (JobError, AudioError, BackendError) as exc:
            failure = exc.code
            self._db(lambda: repo.fail(self._conn, job, failure))
            return
        except sqlite3.Error:
            raise  # not a job error: run_forever puts the job back in the queue
        except Exception as exc:
            # The exception message may contain a path or meeting text: class name only.
            code = f"crash_{type(exc).__name__}"
            self._db(lambda: repo.fail(self._conn, job, code))
            return
        self._db(lambda: repo.complete(self._conn, job, transcript))
        if job.source_kind == repo.SOURCE_UPLOAD:
            # The temporary copy of the dropped recording is no longer needed.
            with contextlib.suppress(OSError):
                self._source(job).unlink()

    def _db(self, action: Callable[[], T]) -> T:
        """Saving the result survives the database being briefly busy (web UI, WAL)."""
        for attempt in range(DB_RETRIES):
            try:
                return action()
            except sqlite3.OperationalError:
                if attempt == DB_RETRIES - 1:
                    raise
                time.sleep(1.0 + attempt)
        raise AssertionError("unreachable")

    # --- loop -----------------------------------------------------------------------

    def run_forever(
        self,
        *,
        poll_s: float = 2.0,
        stop: threading.Event | None = None,
        parent_pid: int | None = None,
    ) -> None:
        """Loop until `stop`. With `parent_pid` the worker exits once its parent is no
        longer the `serve` that started it (serve died and launchd became the parent):
        otherwise it would run without the web UI. The PID is passed explicitly because
        serve may have died before the loop started."""
        event = stop or threading.Event()
        self.acquire_lock()
        self.recover()
        last_sweep = time.monotonic()
        while not event.is_set():
            if parent_pid is not None and os.getppid() != parent_pid:
                return
            if time.monotonic() - last_sweep > SWEEP_S:
                # Retention of failed upload copies holds even without a worker restart.
                self.recover_uploads()
                last_sweep = time.monotonic()
            try:
                busy = self.run_once()
            except sqlite3.Error:
                # The database is unavailable: a claimed job, if any, goes back to the queue.
                with contextlib.suppress(sqlite3.Error):
                    repo.requeue_interrupted(self._conn)
                busy = False
            if not busy:
                event.wait(poll_s)
