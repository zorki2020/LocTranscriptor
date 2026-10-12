from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from transcribator.models import Segment, Transcript
from transcribator.paths import DATA_ENV, data_dir, db_path, uploads_dir
from transcribator.storage import db, repo


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "t.sqlite3")


def _transcript() -> Transcript:
    return Transcript(
        segments=[
            Segment(0.0, 2.0, "S1", "Привет, давайте обсудим Ёлку и миграцию."),
            Segment(2.5, 4.0, "S2", "Да, миграция в среду."),
            Segment(4.5, 6.0, "S1", "Отлично."),
        ],
        duration_s=6.0,
    )


def _add(conn: sqlite3.Connection, title: str = "Встреча", recorded_at: str | None = None) -> int:
    return repo.enqueue(
        conn, title=title, recorded_at=recorded_at, source_kind="path", source_ref="/x/a.mp4"
    )


def test_database_file_is_private_and_schema_current(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    conn = db.connect(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
    # Reopening does not reapply migrations.
    assert db.connect(path).execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_data_dir_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    monkeypatch.setenv(DATA_ENV, str(root))
    assert data_dir() == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(uploads_dir(root).stat().st_mode) == 0o700
    assert db_path(root).parent == root


def test_queue_claims_oldest_and_marks_processing(conn: sqlite3.Connection) -> None:
    first = _add(conn, "Первая")
    _add(conn, "Вторая")

    job = repo.claim_next(conn)

    assert job is not None and job.meeting_id == first
    assert job.attempts == 1 and job.source_ref == "/x/a.mp4"
    meeting = repo.get_meeting(conn, first)
    assert meeting is not None and meeting.status == "processing"
    second = repo.claim_next(conn)
    assert second is not None and second.meeting_id != first
    assert repo.claim_next(conn) is None


def test_complete_saves_transcript_and_is_idempotent(conn: sqlite3.Connection) -> None:
    meeting_id = _add(conn)
    job = repo.claim_next(conn)
    assert job is not None

    repo.complete(conn, job, _transcript(), summary=["Обсудили миграцию."], summary_model="m")
    repo.rename_speaker(conn, meeting_id, "S1", "Иван")
    repo.complete(conn, job, _transcript(), summary=["Обсудили миграцию."], summary_model="m")

    meeting = repo.get_meeting(conn, meeting_id)
    assert meeting is not None
    assert meeting.status == "done" and meeting.duration_s == 6.0
    assert [s.text for s in meeting.segments][1] == "Да, миграция в среду."
    assert len(meeting.segments) == 3  # the retry did not duplicate segments
    assert meeting.summary == ["Обсудили миграцию."]
    assert meeting.names == {"S1": "Иван"}  # the name survived the retry
    assert meeting.labels == ["S1", "S2"]
    assert repo.list_meetings(conn)[0].speakers == 2


def test_fail_and_retry(conn: sqlite3.Connection) -> None:
    meeting_id = _add(conn)
    job = repo.claim_next(conn)
    assert job is not None

    repo.fail(conn, job, "fluidaudio_exit_1")

    meeting = repo.get_meeting(conn, meeting_id)
    assert meeting is not None and meeting.status == "error"
    assert meeting.error_code == "fluidaudio_exit_1"
    assert repo.retry(conn, meeting_id) is True
    assert repo.retry(conn, meeting_id) is False  # already queued
    again = repo.claim_next(conn)
    assert again is not None and again.attempts == 2


def test_interrupted_jobs_return_to_queue(conn: sqlite3.Connection) -> None:
    meeting_id = _add(conn)
    assert repo.claim_next(conn) is not None

    assert repo.requeue_interrupted(conn) == 1

    meeting = repo.get_meeting(conn, meeting_id)
    assert meeting is not None and meeting.status == "queued"
    assert repo.claim_next(conn) is not None


def test_search_ignores_case_yo_and_word_endings_prefix(conn: sqlite3.Connection) -> None:
    meeting_id = _add(conn, "Синк", "2026-10-09T12:00:00+00:00")
    job = repo.claim_next(conn)
    assert job is not None
    repo.complete(conn, job, _transcript())

    hits = repo.search(conn, "ЕЛКУ")
    assert [(h.meeting_id, h.idx) for h in hits] == [(meeting_id, 0)]
    assert hits[0].text == "Привет, давайте обсудим Ёлку и миграцию."  # original text
    assert {h.idx for h in repo.search(conn, "миграц")} == {0, 1}
    assert [h.idx for h in repo.search(conn, "миграция среду")] == [1]  # all words at once


@pytest.mark.parametrize("query", ['"', "AND OR NOT", "*", "миграц*) OR (", "NEAR(", "   ", ""])
def test_search_survives_fts_syntax(conn: sqlite3.Connection, query: str) -> None:
    job_id = _add(conn)
    job = repo.claim_next(conn)
    assert job is not None and job.meeting_id == job_id
    repo.complete(conn, job, _transcript())
    repo.search(conn, query)  # does not crash on FTS5 syntax


def test_fts_query_quotes_every_token() -> None:
    assert repo.fts_query('Ёлка "AND" миграц*') == '"елка"* "and"* "миграц"*'
    assert repo.fts_query("!!!") is None


def test_rename_update_and_delete_cascade(conn: sqlite3.Connection) -> None:
    meeting_id = repo.enqueue(
        conn, title="Встреча", recorded_at=None, source_kind="upload", source_ref="abc"
    )
    job = repo.claim_next(conn)
    assert job is not None
    repo.complete(conn, job, _transcript())

    assert repo.rename_speaker(conn, meeting_id, "S2", "  Мария   Петровна ")
    assert repo.speaker_names(conn, meeting_id) == {"S2": "Мария Петровна"}
    assert repo.rename_speaker(conn, meeting_id, "S2", "")
    assert repo.speaker_names(conn, meeting_id) == {}
    assert not repo.rename_speaker(conn, meeting_id, "S9", "Кто-то")
    assert repo.update_meta(conn, meeting_id, title="Синк\nпо релизу", recorded_at="2026-10-01")
    assert not repo.update_meta(conn, meeting_id, title="   ", recorded_at=None)
    meeting = repo.get_meeting(conn, meeting_id)
    assert meeting is not None and meeting.title == "Синк по релизу"

    assert repo.delete_meeting(conn, meeting_id) == ("upload", "abc")

    assert repo.get_meeting(conn, meeting_id) is None
    for table in ("segment", "speaker", "job"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
    assert repo.search(conn, "миграция") == []
    assert repo.delete_meeting(conn, meeting_id) is None


def test_list_orders_by_recording_date_and_queue_puts_active_first(
    conn: sqlite3.Connection,
) -> None:
    old = _add(conn, "Старая", "2026-01-01T10:00:00+00:00")
    new = _add(conn, "Новая", "2026-10-01T10:00:00+00:00")
    assert [m.id for m in repo.list_meetings(conn)] == [new, old]
    job = repo.claim_next(conn)  # takes the older one (first in the queue)
    assert job is not None and job.meeting_id == old
    assert repo.queue(conn)[0].id == old  # the one in progress is on top
    assert repo.queue(conn)[0].job_stage == "start"


def test_upload_refs_only_for_unfinished(conn: sqlite3.Connection) -> None:
    repo.enqueue(conn, title="a", recorded_at=None, source_kind="upload", source_ref="u1")
    done = repo.enqueue(conn, title="b", recorded_at=None, source_kind="upload", source_ref="u2")
    for _ in range(2):
        job = repo.claim_next(conn)
        assert job is not None
        if job.meeting_id == done:
            repo.complete(conn, job, _transcript())
    assert repo.upload_refs(conn) == {"u1"}


def test_unknown_source_kind_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        repo.enqueue(conn, title="a", recorded_at=None, source_kind="url", source_ref="x")


def test_delete_refuses_meeting_in_processing(conn: sqlite3.Connection) -> None:
    meeting_id = _add(conn)
    assert repo.claim_next(conn) is not None
    with pytest.raises(repo.MeetingBusy):
        repo.delete_meeting(conn, meeting_id)
    assert repo.get_meeting(conn, meeting_id) is not None


def test_queue_lists_waiting_items_in_processing_order(conn: sqlite3.Connection) -> None:
    a = _add(conn, "a")
    b = _add(conn, "b")
    assert [m.id for m in repo.queue(conn)] == [a, b]


def test_concurrent_first_open_applies_migrations_once(tmp_path: Path) -> None:
    import threading  # noqa: PLC0415

    path = tmp_path / "race.sqlite3"
    errors: list[BaseException] = []

    def open_db() -> None:
        try:
            db.connect(path).close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_db) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert db.connect(path).execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_child_pgid_and_expired_uploads(conn: sqlite3.Connection) -> None:
    repo.enqueue(conn, title="a", recorded_at=None, source_kind="upload", source_ref="u1")
    job = repo.claim_next(conn)
    assert job is not None
    repo.set_child(conn, job.id, 4242)
    assert repo.interrupted_children(conn) == [4242]
    repo.fail(conn, job, "x")
    assert repo.expired_failed_uploads(conn, "9999-01-01T00:00:00+00:00") == ["u1"]
    assert repo.expired_failed_uploads(conn, "2000-01-01T00:00:00+00:00") == []


def test_migration_from_version_1_adds_child_pgid(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    for statement in db._split(db._MIGRATIONS[0]):
        raw.execute(statement)
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = db.connect(path)

    columns = [r[1] for r in conn.execute("PRAGMA table_info(job)")]
    assert "child_pgid" in columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_enqueue_stores_language_and_rejects_unknown(conn: sqlite3.Connection) -> None:
    repo.enqueue(
        conn, title="a", recorded_at=None, source_kind="path", source_ref="/x", language="ru"
    )
    job = repo.claim_next(conn)
    assert job is not None and job.language == "ru"
    with pytest.raises(ValueError):
        repo.enqueue(
            conn, title="a", recorded_at=None, source_kind="path", source_ref="/x", language="xx"
        )
