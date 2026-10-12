from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator.models import Segment, Transcript
from transcribator.paths import db_path, uploads_dir, work_dir
from transcribator.storage import db, repo
from transcribator.worker import loop
from transcribator.worker.loop import JobError, Worker

TRANSCRIPT = Transcript([Segment(0.0, 1.0, "S1", "Привет"), Segment(1.5, 2.0, "S2", "Да.")], 2.0)


class FakeProcessor:
    def __init__(self, result: Transcript | Exception = TRANSCRIPT) -> None:
        self.result = result
        self.calls: list[tuple[Path, Path]] = []
        self.languages: list[str] = []

    def __call__(
        self, src: Path, work: Path, on_start: object = None, language: str = "en"
    ) -> Transcript:
        self.calls.append((src, work))
        self.languages.append(language)
        if callable(on_start):
            on_start(4242)  # like run_isolated: the child's PGID
        (work / "transcript.json").write_text("{}", encoding="utf-8")  # like the real child
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _worker(root: Path, **kw: object) -> Worker:
    root.mkdir(exist_ok=True)
    return Worker(root, keep_awake=False, **kw)  # type: ignore[arg-type]


def _meeting(root: Path, src: Path, kind: str = "path") -> int:
    conn = db.connect(db_path(root))
    try:
        return repo.enqueue(
            conn, title="Встреча", recorded_at=None, source_kind=kind, source_ref=str(src)
        )
    finally:
        conn.close()


def _get(root: Path, meeting_id: int) -> repo.Meeting:
    conn = db.connect(db_path(root))
    try:
        meeting = repo.get_meeting(conn, meeting_id)
    finally:
        conn.close()
    assert meeting is not None
    return meeting


def test_success_saves_transcript_and_cleans_work(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    processor = FakeProcessor()
    worker = _worker(root, processor=processor)
    meeting_id = _meeting(root, src)

    assert worker.run_once() is True
    assert worker.run_once() is False

    meeting = _get(root, meeting_id)
    assert meeting.status == "done"
    assert meeting.summary == []  # no summary: only the transcript is needed
    assert [s.text for s in meeting.segments] == ["Привет", "Да."]
    assert processor.calls[0][0] == src
    assert list(work_dir(root).iterdir()) == []  # the job JSON is deleted
    assert src.exists()  # the source recording is untouched


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (JobError("fluidaudio_exit_1"), "fluidaudio_exit_1"),
        (RuntimeError("/Users/x/Встреча с Иваном.mp4"), "crash_RuntimeError"),
    ],
)
def test_failures_store_only_code(root: Path, tmp_path: Path, error: Exception, code: str) -> None:
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    worker = _worker(root, processor=FakeProcessor(error))
    meeting_id = _meeting(root, src)

    worker.run_once()

    meeting = _get(root, meeting_id)
    assert meeting.status == "error" and meeting.error_code == code
    assert list(work_dir(root).iterdir()) == []


def test_missing_source_is_an_error(root: Path, tmp_path: Path) -> None:
    worker = _worker(root, processor=FakeProcessor())
    meeting_id = _meeting(root, tmp_path / "gone.mp4")
    worker.run_once()
    assert _get(root, meeting_id).error_code == "source_missing"


def test_upload_copy_deleted_after_success_kept_after_error(root: Path) -> None:
    worker = _worker(root, processor=FakeProcessor())
    ok = uploads_dir(root) / ("a" * 32)
    ok.write_bytes(b"x")
    ok_id = _meeting(root, Path(ok.name), kind="upload")
    worker.run_once()
    assert _get(root, ok_id).status == "done"
    assert not ok.exists()

    bad_worker = _worker(root, processor=FakeProcessor(JobError("fluidaudio_exit_1")))
    bad = uploads_dir(root) / ("b" * 32)
    bad.write_bytes(b"x")
    _meeting(root, Path(bad.name), kind="upload")
    bad_worker.run_once()
    assert bad.exists()  # kept for "Retry"


def test_recover_requeues_and_removes_orphans(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    worker = _worker(root, processor=FakeProcessor())
    meeting_id = _meeting(root, src)
    conn = db.connect(db_path(root))
    assert repo.claim_next(conn) is not None  # "crashed" mid-job
    conn.close()
    orphan = uploads_dir(root) / ("c" * 32)
    orphan.write_bytes(b"x")
    os.utime(orphan, (0, 0))
    fresh = uploads_dir(root) / ("d" * 32)  # an upload in progress right now
    fresh.write_bytes(b"x")

    worker.recover()

    assert _get(root, meeting_id).status == "queued"
    assert not orphan.exists() and fresh.exists()
    worker.run_once()
    assert _get(root, meeting_id).status == "done"


def test_run_forever_stops_on_event(root: Path) -> None:
    worker = _worker(root, processor=FakeProcessor())
    stop = threading.Event()
    thread = threading.Thread(target=worker.run_forever, kwargs={"poll_s": 0.05, "stop": stop})
    thread.start()
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


@requires_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox exists only on macOS")
def test_process_isolated_with_fake_engine(
    sine_audio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real child process under the sandbox, with the engine replaced by a stub."""
    real = loop.run_isolated

    def with_fake_engine(cmd: list[str], **kw: object) -> object:
        return real([*cmd, "--engine", "fake"], **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(loop, "run_isolated", with_fake_engine)
    work = tmp_path / "w"
    work.mkdir()

    pgids: list[int] = []
    transcript = loop.process_isolated(sine_audio, work, pgids.append)

    assert [s.speaker for s in transcript.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert not sine_audio.with_suffix(".fluidaudio.md").exists()
    assert len(pgids) == 1 and pgids[0] > 0


@requires_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin", reason="the sandbox exists only on macOS")
def test_process_isolated_reports_child_error_code(tmp_path: Path) -> None:
    bad = tmp_path / "Личное.mp3"
    bad.write_bytes(b"not audio")
    with pytest.raises(JobError) as info:
        loop.process_isolated(bad, tmp_path, lambda pgid: None)
    assert info.value.code == "probe_failed"


def test_second_worker_on_same_data_is_refused(root: Path) -> None:
    first = _worker(root, processor=FakeProcessor())
    first.acquire_lock()
    second = _worker(root, processor=FakeProcessor())
    with pytest.raises(loop.WorkerBusy):
        second.acquire_lock()
    first.close()
    second.acquire_lock()  # the lock was released along with the first worker
    second.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="pgrep -g is available on macOS")
def test_recover_stops_orphaned_child_of_dead_worker(root: Path, tmp_path: Path) -> None:
    import subprocess  # noqa: PLC0415

    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    worker = _worker(root, processor=FakeProcessor())
    _meeting(root, src)
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "transcribator", "process"],
        start_new_session=True,
    )
    stranger = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    conn = db.connect(db_path(root))
    job = repo.claim_next(conn)
    assert job is not None
    repo.set_child(conn, job.id, orphan.pid)  # the previous worker recorded the PGID and died
    conn.close()
    try:
        worker.recover()
        assert orphan.wait(timeout=40) != 0  # stopped
        conn = db.connect(db_path(root))
        try:
            assert repo.interrupted_children(conn) == []
            # A PGID without our command is left alone, even if recorded.
            assert not loop.kill_orphan_group(stranger.pid)
        finally:
            conn.close()
        assert stranger.poll() is None
    finally:
        stranger.kill()
        orphan.kill()


def test_failed_upload_copy_expires(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _worker(root, processor=FakeProcessor(JobError("fluidaudio_exit_1")))
    copy = uploads_dir(root) / ("e" * 32)
    copy.write_bytes(b"x")
    _meeting(root, Path(copy.name), kind="upload")
    worker.run_once()
    worker.recover()
    assert copy.exists()  # recent failure: the copy waits for "Retry"

    monkeypatch.setattr(loop, "FAILED_UPLOAD_TTL", loop.timedelta(seconds=-1))
    worker.recover()
    assert not copy.exists()


def test_db_write_retries_on_busy_database(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3  # noqa: PLC0415

    worker = _worker(root, processor=FakeProcessor())
    monkeypatch.setattr(loop.time, "sleep", lambda s: None)
    attempts: list[int] = []

    def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert worker._db(flaky) == "ok"
    assert len(attempts) == 3


def test_parent_watch_stops_loop(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worker = _worker(root, processor=FakeProcessor())
    monkeypatch.setattr(
        loop.os, "getppid", lambda: 1
    )  # serve died, the worker is reparented to launchd
    worker.run_forever(poll_s=0.01, parent_pid=100)  # returns on its own


def test_child_pgid_is_recorded_during_and_cleared_after_job(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    seen: list[list[int]] = []

    class Recording(FakeProcessor):
        def __call__(
            self, src: Path, work: Path, on_start: object = None, language: str = "en"
        ) -> Transcript:
            result = super().__call__(src, work, on_start, language)
            conn = db.connect(db_path(root))
            seen.append(repo.interrupted_children(conn))
            conn.close()
            return result

    worker = _worker(root, processor=Recording())
    _meeting(root, src)
    worker.run_once()
    assert seen == [[4242]]
    conn = db.connect(db_path(root))
    assert conn.execute("SELECT child_pgid FROM job").fetchone()[0] is None
    conn.close()


def test_bad_upload_ref_is_rejected(root: Path) -> None:
    worker = _worker(root, processor=FakeProcessor())
    meeting_id = _meeting(root, Path("../../etc/passwd"), kind="upload")
    worker.run_once()
    assert _get(root, meeting_id).error_code == "bad_upload_ref"


def test_worker_passes_meeting_language_to_recognizer(root: Path, tmp_path: Path) -> None:
    src = tmp_path / "rec.mp4"
    src.write_bytes(b"x")
    processor = FakeProcessor()
    worker = _worker(root, processor=processor)
    conn = db.connect(db_path(root))
    repo.enqueue(
        conn, title="x", recorded_at=None, source_kind="path", source_ref=str(src), language="ru"
    )
    conn.close()
    worker.run_once()
    assert processor.languages == ["ru"]


def test_process_isolated_puts_language_into_child_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    class Result:
        timed_out = False
        returncode = 1

    def capture(cmd: list[str], **kw: object) -> Result:
        seen.append(list(cmd))
        return Result()

    monkeypatch.setattr(loop, "run_isolated", capture)
    with pytest.raises(JobError):
        loop.process_isolated(tmp_path / "a.mp3", tmp_path, lambda pgid: None, "ru")
    assert seen[0][seen[0].index("--language") + 1] == "ru"
