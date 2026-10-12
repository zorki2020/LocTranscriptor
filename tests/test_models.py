from __future__ import annotations

from transcribator.models import RawResult, Segment, SpeakerTurn, Transcript, Word


def test_raw_result_roundtrip_through_dict() -> None:
    raw = RawResult(
        words=[Word(0.0, 0.5, "слово")],
        turns=[SpeakerTurn(0.0, 1.0, "SPEAKER_00")],
        timings={"diarize": 1.5},
    )
    assert RawResult.from_dict(raw.to_dict()) == raw


def test_from_dict_ignores_extra_keys_and_missing_timings() -> None:
    data = {"words": [], "turns": [], "duration_s": 12.0}
    assert RawResult.from_dict(data) == RawResult(words=[], turns=[], timings={})


def test_speakers_are_listed_once_in_order_of_appearance() -> None:
    segments = [
        Segment(0, 1, "B", "а"),
        Segment(1, 2, "A", "б"),
        Segment(2, 3, "B", "в"),
    ]
    assert Transcript(segments, 3.0).speakers == ["B", "A"]


def test_transcript_round_trips_through_dict() -> None:
    from transcribator.models import Segment, Transcript  # noqa: PLC0415

    t = Transcript([Segment(0.0, 1.5, "S1", "Привет"), Segment(2.0, 3.0, "S2", "Да.")], 3.0)
    assert Transcript.from_dict(t.to_dict()) == t
