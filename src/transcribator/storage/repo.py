"""Database operations: meetings, the job queue, results, search.

All queries are parameterized. Times are UTC, ISO 8601 (`2026-10-10T12:00:00+00:00`).
Changes that must apply as a whole go in one transaction (`_tx`).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from transcribator.i18n import RECORDING_LANGS
from transcribator.models import Segment, Transcript

SOURCE_PATH = "path"
SOURCE_UPLOAD = "upload"
_TOKEN = re.compile(r"\w+", re.UNICODE)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def search_form(text: str) -> str:
    """Text normalized for search: lowercase, Cyrillic "ё" folded to "е"."""
    return text.lower().replace("ё", "е")


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    meeting_id: int
    source_kind: str
    source_ref: str
    attempts: int
    language: str = "en"


@dataclass(frozen=True, slots=True)
class MeetingSummary:
    """A row of the meeting list and the queue."""

    id: int
    title: str
    recorded_at: str | None
    added_at: str
    duration_s: float | None
    status: str
    summary: list[str]
    speakers: int
    job_stage: str | None
    error_code: str | None
    attempts: int


@dataclass(slots=True)
class Meeting:
    id: int
    title: str
    recorded_at: str | None
    added_at: str
    duration_s: float | None
    status: str
    summary: list[str]
    summary_model: str | None
    summary_error: str | None
    source_kind: str
    error_code: str | None
    segments: list[Segment] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)

    @property
    def transcript(self) -> Transcript:
        return Transcript(segments=list(self.segments), duration_s=self.duration_s or 0.0)


@dataclass(frozen=True, slots=True)
class SearchHit:
    meeting_id: int
    title: str
    recorded_at: str | None
    idx: int
    start_s: float
    speaker_label: str
    text: str


def _summary(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


# --- queue ---------------------------------------------------------------------------


def enqueue(
    conn: sqlite3.Connection,
    *,
    title: str,
    recorded_at: str | None,
    source_kind: str,
    source_ref: str,
    language: str = "en",
) -> int:
    """Add a new meeting to the queue. Return the meeting id."""
    if source_kind not in (SOURCE_PATH, SOURCE_UPLOAD):
        raise ValueError("unknown source kind")
    if language not in RECORDING_LANGS:
        raise ValueError("unsupported language")
    stamp = now_utc()
    with _tx(conn):
        cur = conn.execute(
            "INSERT INTO meeting"
            " (title, recorded_at, added_at, status, source_kind, source_ref, language)"
            " VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (title, recorded_at, stamp, source_kind, source_ref, language),
        )
        meeting_id = int(cur.lastrowid or 0)
        conn.execute(
            "INSERT INTO job (meeting_id, state, created_at, updated_at)"
            " VALUES (?, 'queued', ?, ?)",
            (meeting_id, stamp, stamp),
        )
    return meeting_id


def claim_next(conn: sqlite3.Connection) -> Job | None:
    """Atomically claim the oldest job in the queue."""
    stamp = now_utc()
    with _tx(conn):
        row = conn.execute(
            "UPDATE job SET state = 'running', stage = 'start', attempts = attempts + 1,"
            " error_code = NULL, child_pgid = NULL, updated_at = ?"
            " WHERE id = (SELECT id FROM job WHERE state = 'queued' ORDER BY id LIMIT 1)"
            " RETURNING id, meeting_id, attempts",
            (stamp,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE meeting SET status = 'processing', error_code = NULL WHERE id = ?",
            (row["meeting_id"],),
        )
        source = conn.execute(
            "SELECT source_kind, source_ref, language FROM meeting WHERE id = ?",
            (row["meeting_id"],),
        ).fetchone()
    return Job(
        id=int(row["id"]),
        meeting_id=int(row["meeting_id"]),
        source_kind=str(source["source_kind"]),
        source_ref=str(source["source_ref"]),
        attempts=int(row["attempts"]),
        language=str(source["language"]),
    )


def set_stage(conn: sqlite3.Connection, job_id: int, stage: str) -> None:
    conn.execute(
        "UPDATE job SET stage = ?, updated_at = ? WHERE id = ?", (stage, now_utc(), job_id)
    )


def complete(
    conn: sqlite3.Connection,
    job: Job,
    transcript: Transcript,
    *,
    summary: Sequence[str] = (),
    summary_model: str | None = None,
    summary_error: str | None = None,
) -> None:
    """Save the result in one transaction. Rerunning a job creates no duplicates."""
    with _tx(conn):
        conn.execute("DELETE FROM segment WHERE meeting_id = ?", (job.meeting_id,))
        conn.executemany(
            "INSERT INTO segment"
            " (meeting_id, idx, start_s, end_s, speaker_label, text, search_text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (job.meeting_id, idx, s.start, s.end, s.speaker, s.text, search_form(s.text))
                for idx, s in enumerate(transcript.segments)
            ],
        )
        # Names the user already gave are kept when a meeting is reprocessed.
        conn.executemany(
            "INSERT OR IGNORE INTO speaker (meeting_id, label) VALUES (?, ?)",
            [(job.meeting_id, label) for label in transcript.speakers],
        )
        conn.execute(
            "UPDATE meeting SET status = 'done', duration_s = ?, summary = ?, summary_model = ?,"
            " summary_error = ?, error_code = NULL WHERE id = ?",
            (
                transcript.duration_s,
                json.dumps(list(summary), ensure_ascii=False),
                summary_model,
                summary_error,
                job.meeting_id,
            ),
        )
        conn.execute(
            "UPDATE job SET state = 'done', stage = 'done', error_code = NULL, updated_at = ?"
            " WHERE id = ?",
            (now_utc(), job.id),
        )


def fail(conn: sqlite3.Connection, job: Job, code: str) -> None:
    """Job error: code only; the stage stays at the one where it failed."""
    with _tx(conn):
        conn.execute(
            "UPDATE job SET state = 'error', error_code = ?, child_pgid = NULL, updated_at = ?"
            " WHERE id = ?",
            (code, now_utc(), job.id),
        )
        conn.execute(
            "UPDATE meeting SET status = 'error', error_code = ? WHERE id = ?",
            (code, job.meeting_id),
        )


def retry(conn: sqlite3.Connection, meeting_id: int) -> bool:
    """Put a failed job back in the queue. False if there is nothing to retry."""
    with _tx(conn):
        cur = conn.execute(
            "UPDATE job SET state = 'queued', error_code = NULL, updated_at = ?"
            " WHERE meeting_id = ? AND state = 'error'",
            (now_utc(), meeting_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE meeting SET status = 'queued', error_code = NULL WHERE id = ?", (meeting_id,)
        )
    return True


def requeue_interrupted(conn: sqlite3.Connection) -> int:
    """Requeue jobs interrupted by a worker crash. Called at startup."""
    with _tx(conn):
        cur = conn.execute(
            "UPDATE job SET state = 'queued', stage = NULL, child_pgid = NULL, updated_at = ?"
            " WHERE state = 'running'",
            (now_utc(),),
        )
        conn.execute(
            "UPDATE meeting SET status = 'queued'"
            " WHERE id IN (SELECT meeting_id FROM job WHERE state = 'queued')"
            " AND status = 'processing'"
        )
    return cur.rowcount


# --- reading -------------------------------------------------------------------------

_LIST_SQL = """
SELECT m.id, m.title, m.recorded_at, m.added_at, m.duration_s, m.status, m.summary,
       m.error_code, j.stage AS job_stage, COALESCE(j.attempts, 0) AS attempts,
       (SELECT COUNT(*) FROM speaker s WHERE s.meeting_id = m.id) AS speakers
FROM meeting m LEFT JOIN job j ON j.meeting_id = m.id
"""


def _row_summary(row: sqlite3.Row) -> MeetingSummary:
    return MeetingSummary(
        id=int(row["id"]),
        title=str(row["title"]),
        recorded_at=row["recorded_at"],
        added_at=str(row["added_at"]),
        duration_s=row["duration_s"],
        status=str(row["status"]),
        summary=_summary(row["summary"]),
        speakers=int(row["speakers"]),
        job_stage=row["job_stage"],
        error_code=row["error_code"],
        attempts=int(row["attempts"]),
    )


def list_meetings(
    conn: sqlite3.Connection, *, limit: int = 100, offset: int = 0
) -> list[MeetingSummary]:
    """Meetings by recording date, newest first."""
    rows = conn.execute(
        _LIST_SQL + " ORDER BY COALESCE(m.recorded_at, m.added_at) DESC, m.id DESC"
        " LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [_row_summary(row) for row in rows]


def queue(conn: sqlite3.Connection, *, limit: int = 20) -> list[MeetingSummary]:
    """Unfinished and recently added meetings for the upload page."""
    rows = conn.execute(
        _LIST_SQL + " ORDER BY CASE m.status WHEN 'processing' THEN 0 WHEN 'queued' THEN 1"
        " WHEN 'error' THEN 2 ELSE 3 END,"
        # The queue in the order the worker will take it; everything else newest first.
        " CASE m.status WHEN 'queued' THEN m.id ELSE -m.id END LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_summary(row) for row in rows]


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> Meeting | None:
    row = conn.execute("SELECT * FROM meeting WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        return None
    segments = [
        Segment(float(s["start_s"]), float(s["end_s"]), str(s["speaker_label"]), str(s["text"]))
        for s in conn.execute(
            "SELECT start_s, end_s, speaker_label, text FROM segment"
            " WHERE meeting_id = ? ORDER BY idx",
            (meeting_id,),
        )
    ]
    speakers = conn.execute(
        "SELECT label, display_name FROM speaker WHERE meeting_id = ? ORDER BY label",
        (meeting_id,),
    ).fetchall()
    return Meeting(
        id=int(row["id"]),
        title=str(row["title"]),
        recorded_at=row["recorded_at"],
        added_at=str(row["added_at"]),
        duration_s=row["duration_s"],
        status=str(row["status"]),
        summary=_summary(row["summary"]),
        summary_model=row["summary_model"],
        summary_error=row["summary_error"],
        source_kind=str(row["source_kind"]),
        error_code=row["error_code"],
        segments=segments,
        names={str(s["label"]): str(s["display_name"]) for s in speakers if s["display_name"]},
        labels=[str(s["label"]) for s in speakers],
    )


def fts_query(text: str) -> str | None:
    """Turn a user query into a safe FTS5 query: each word quoted and matched as a
    prefix, all words required. FTS5 operators and special characters have no effect."""
    tokens = _TOKEN.findall(search_form(text))
    if not tokens:
        return None
    return " ".join(f'"{token}"*' for token in tokens[:20])


def search(conn: sqlite3.Connection, text: str, *, limit: int = 50) -> list[SearchHit]:
    """Full-text search over the utterances of all meetings."""
    query = fts_query(text)
    if query is None:
        return []
    rows = conn.execute(
        "SELECT s.meeting_id, m.title, m.recorded_at, s.idx, s.start_s, s.speaker_label, s.text"
        " FROM segment_fts f JOIN segment s ON s.id = f.rowid JOIN meeting m ON m.id = s.meeting_id"
        " WHERE segment_fts MATCH ?"
        " ORDER BY COALESCE(m.recorded_at, m.added_at) DESC, s.meeting_id DESC, s.idx"
        " LIMIT ?",
        (query, limit),
    ).fetchall()
    return [
        SearchHit(
            meeting_id=int(r["meeting_id"]),
            title=str(r["title"]),
            recorded_at=r["recorded_at"],
            idx=int(r["idx"]),
            start_s=float(r["start_s"]),
            speaker_label=str(r["speaker_label"]),
            text=str(r["text"]),
        )
        for r in rows
    ]


def speaker_names(conn: sqlite3.Connection, meeting_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT label, display_name FROM speaker WHERE meeting_id = ? AND display_name IS NOT NULL",
        (meeting_id,),
    ).fetchall()
    return {str(r["label"]): str(r["display_name"]) for r in rows}


# --- edits and deletion --------------------------------------------------------------


def rename_speaker(conn: sqlite3.Connection, meeting_id: int, label: str, name: str) -> bool:
    """Speaker name for the whole meeting. An empty name restores "Speaker N"."""
    clean = " ".join(name.split())[:100] or None
    cur = conn.execute(
        "UPDATE speaker SET display_name = ? WHERE meeting_id = ? AND label = ?",
        (clean, meeting_id, label),
    )
    return cur.rowcount > 0


def update_meta(
    conn: sqlite3.Connection, meeting_id: int, *, title: str, recorded_at: str | None
) -> bool:
    clean = " ".join(title.split())[:300]
    if not clean:
        return False
    cur = conn.execute(
        "UPDATE meeting SET title = ?, recorded_at = ? WHERE id = ?",
        (clean, recorded_at, meeting_id),
    )
    return cur.rowcount > 0


class MeetingBusy(Exception):
    """The meeting is being processed: it cannot be deleted, the worker would lose its file."""


def delete_meeting(conn: sqlite3.Connection, meeting_id: int) -> tuple[str, str] | None:
    """Delete a meeting completely. Return its source so the caller can delete the
    temporary upload copy. Segments, speakers, the job and FTS rows go by cascade.

    The status check and the deletion run in one transaction, so the worker cannot claim
    the job in between. A meeting being processed is not deleted (`MeetingBusy`)."""
    with _tx(conn):
        row = conn.execute(
            "SELECT source_kind, source_ref, status FROM meeting WHERE id = ?", (meeting_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "processing":
            raise MeetingBusy
        conn.execute("DELETE FROM meeting WHERE id = ?", (meeting_id,))
    return str(row["source_kind"]), str(row["source_ref"])


def upload_refs(conn: sqlite3.Connection) -> set[str]:
    """Names of upload copies still referenced by meetings (for orphan cleanup)."""
    rows = conn.execute(
        "SELECT source_ref FROM meeting WHERE source_kind = 'upload' AND status != 'done'"
    ).fetchall()
    return {str(r["source_ref"]) for r in rows}


def set_child(conn: sqlite3.Connection, job_id: int, pgid: int | None) -> None:
    """The job's recognition process group: it must be stopped after a worker crash."""
    conn.execute("UPDATE job SET child_pgid = ? WHERE id = ?", (pgid, job_id))


def interrupted_children(conn: sqlite3.Connection) -> list[int]:
    """Child process groups of jobs marked as running (call before requeue)."""
    rows = conn.execute(
        "SELECT child_pgid FROM job WHERE state = 'running' AND child_pgid IS NOT NULL"
    ).fetchall()
    return [int(r["child_pgid"]) for r in rows]


def expired_failed_uploads(conn: sqlite3.Connection, before: str) -> list[str]:
    """Upload copies of failed meetings not retried since `before` (ISO UTC)."""
    rows = conn.execute(
        "SELECT m.source_ref FROM meeting m JOIN job j ON j.meeting_id = m.id"
        " WHERE m.source_kind = 'upload' AND m.status = 'error' AND j.updated_at < ?",
        (before,),
    ).fetchall()
    return [str(r["source_ref"]) for r in rows]
