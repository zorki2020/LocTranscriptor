"""Queries needed only by the web layer. All of them are parameterized."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from transcribator.i18n import pick
from transcribator.pipeline.format_md import LABELS
from transcribator.storage import repo


def speaker_display_names(
    conn: sqlite3.Connection, meeting_id: int, lang: str | None = None
) -> dict[str, str]:
    """Label -> display name, as in `format_md.speaker_labels` but without loading the
    whole transcript: "Speaker N" in order of first appearance when no name is set."""
    rows = conn.execute(
        "SELECT speaker_label, MIN(idx) AS first_idx FROM segment WHERE meeting_id = ?"
        " GROUP BY speaker_label ORDER BY first_idx",
        (meeting_id,),
    ).fetchall()
    names = repo.speaker_names(conn, meeting_id)
    return {
        str(row["speaker_label"]): names.get(str(row["speaker_label"]))
        or pick(LABELS, "speaker_n", lang, n=index)
        for index, row in enumerate(rows, start=1)
    }


def job_stage(conn: sqlite3.Connection, meeting_id: int) -> str | None:
    """Current stage of the meeting's job (for the "processing" status)."""
    row = conn.execute("SELECT stage FROM job WHERE meeting_id = ?", (meeting_id,)).fetchone()
    return None if row is None or row["stage"] is None else str(row["stage"])


def stage_started(conn: sqlite3.Connection) -> dict[int, str]:
    """Meeting -> start time of the current stage (UTC ISO) for running jobs."""
    rows = conn.execute("SELECT meeting_id, updated_at FROM job WHERE state = 'running'")
    return {int(row["meeting_id"]): str(row["updated_at"]) for row in rows}


def meeting_languages(conn: sqlite3.Connection, meeting_ids: Iterable[int]) -> dict[int, str]:
    """Meeting -> recording language code, for the given meetings only."""
    ids = list(meeting_ids)
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, language FROM meeting WHERE id IN ({marks})",  # noqa: S608 - only "?"
        ids,
    )
    return {int(row["id"]): str(row["language"]) for row in rows}


STATUSES = ("queued", "processing", "done", "error")


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Number of meetings per status in one query. Statuses without meetings give 0."""
    counts = dict.fromkeys(STATUSES, 0)
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM meeting GROUP BY status"):
        if row["status"] in counts:
            counts[str(row["status"])] = int(row["n"])
    return counts
