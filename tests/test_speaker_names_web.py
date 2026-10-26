"""Default speaker names for the web layer: the microphone speaker is "Me"/"Я"."""

from pathlib import Path

import pytest

from transcribator.models import Segment, Transcript
from transcribator.paths import db_path, ensure_private_dir
from transcribator.storage import db, repo
from transcribator.web import queries


@pytest.fixture
def meeting(tmp_path: Path):  # type: ignore[no-untyped-def]
    root = ensure_private_dir(tmp_path / "data")
    conn = db.connect(db_path(root))
    meeting_id = repo.enqueue(
        conn,
        title="t",
        recorded_at=None,
        source_kind=repo.SOURCE_PATH,
        source_ref="/synthetic/x.wav",
        language="en",
        layout="mic_system",
    )
    job = repo.claim_next(conn)
    assert job is not None
    segments = [
        Segment(0.0, 2.0, "ME", "hello"),
        Segment(2.0, 4.0, "S1", "hi"),
        Segment(4.0, 6.0, "S2", "right"),
    ]
    repo.complete(conn, job, Transcript(segments, duration_s=6.0))
    yield conn, meeting_id
    conn.close()


def test_microphone_speaker_is_me_and_does_not_take_a_number(meeting) -> None:  # type: ignore[no-untyped-def]
    conn, meeting_id = meeting
    assert queries.speaker_display_names(conn, meeting_id, "en") == {
        "ME": "Me",
        "S1": "Speaker 1",
        "S2": "Speaker 2",
    }


def test_microphone_speaker_is_localized(meeting) -> None:  # type: ignore[no-untyped-def]
    conn, meeting_id = meeting
    assert queries.speaker_display_names(conn, meeting_id, "ru")["ME"] == "Я"


def test_custom_name_wins_over_default(meeting) -> None:  # type: ignore[no-untyped-def]
    conn, meeting_id = meeting
    repo.rename_speaker(conn, meeting_id, "ME", "Anna")
    assert queries.speaker_display_names(conn, meeting_id, "en")["ME"] == "Anna"
