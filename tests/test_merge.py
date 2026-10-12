from __future__ import annotations

from transcribator.models import RawResult, SpeakerTurn, Word
from transcribator.pipeline.merge import (
    DEFAULT_SPEAKER,
    assign_speakers,
    build_segments,
    merge_result,
)

A, B = "SPEAKER_00", "SPEAKER_01"


def test_word_gets_speaker_with_largest_overlap() -> None:
    turns = [SpeakerTurn(0.0, 1.2, A), SpeakerTurn(1.2, 3.0, B)]
    # word 1.0-2.0: 0.2 s with A and 0.8 s with B
    assert assign_speakers([Word(1.0, 2.0, "да")], turns) == [B]


def test_word_in_pause_gets_nearest_speaker() -> None:
    turns = [SpeakerTurn(0.0, 1.0, A), SpeakerTurn(3.0, 4.0, B)]
    words = [Word(1.2, 1.4, "а"), Word(2.7, 2.9, "б")]
    assert assign_speakers(words, turns) == [A, B]


def test_zero_length_word_inside_turn_gets_that_turn() -> None:
    turns = [SpeakerTurn(0.0, 1.0, A), SpeakerTurn(1.0, 2.0, B)]
    assert assign_speakers([Word(1.5, 1.5, "э")], turns) == [B]


def test_no_turns_gives_default_speaker() -> None:
    assert assign_speakers([Word(0, 1, "а"), Word(1, 2, "б")], []) == [DEFAULT_SPEAKER] * 2


def test_long_early_turn_still_found_when_later_turns_are_short() -> None:
    # Overlapping turns: a long A with a short B inside it. The word is around 50 s.
    turns = [SpeakerTurn(0.0, 100.0, A), SpeakerTurn(10.0, 12.0, B)]
    assert assign_speakers([Word(50.0, 50.5, "слово")], turns) == [A]


def test_unsorted_turns_are_handled() -> None:
    turns = [SpeakerTurn(3.0, 4.0, B), SpeakerTurn(0.0, 1.0, A)]
    assert assign_speakers([Word(0.2, 0.8, "а"), Word(3.2, 3.8, "б")], turns) == [A, B]


def test_segments_split_on_speaker_change_and_long_gap() -> None:
    words = [
        Word(0.0, 0.5, "Привет"),
        Word(0.6, 1.0, "всем"),
        Word(1.2, 1.6, "Да"),  # speaker change
        Word(5.0, 5.4, "Дальше"),  # same speaker, but a 3.4 s pause
    ]
    segments = build_segments(words, [A, A, B, B])
    assert [(s.speaker, s.text) for s in segments] == [
        (A, "Привет всем"),
        (B, "Да"),
        (B, "Дальше"),
    ]
    assert (segments[0].start, segments[0].end) == (0.0, 1.0)


def test_closing_punctuation_attaches_without_space() -> None:
    words = [
        Word(0.0, 0.4, "Привет"),
        Word(0.4, 0.5, ","),
        Word(0.6, 1.0, "мир"),
        Word(1.0, 1.1, "."),
    ]
    assert build_segments(words, [A] * 4)[0].text == "Привет, мир."


def test_punctuation_token_stays_in_current_segment_even_if_speaker_differs() -> None:
    words = [Word(0.0, 0.4, "Привет"), Word(0.4, 0.5, "."), Word(0.6, 1.0, "Да")]
    segments = build_segments(words, [A, B, B])
    assert [s.text for s in segments] == ["Привет.", "Да"]


def test_leading_punctuation_without_segment_is_dropped() -> None:
    assert build_segments([Word(0.0, 0.1, ".")], [A]) == []


def test_empty_words_are_skipped() -> None:
    words = [Word(0.0, 0.2, "  "), Word(0.3, 0.6, " слово ")]
    assert build_segments(words, [A, A])[0].text == "слово"


def test_long_monologue_is_split_by_max_segment_length() -> None:
    words = [Word(float(i), i + 0.5, f"с{i}") for i in range(10)]
    segments = build_segments(words, [A] * 10, max_gap_s=2.0, max_segment_s=4.0)
    assert len(segments) > 1
    assert " ".join(s.text for s in segments) == " ".join(f"с{i}" for i in range(10))


def test_merge_result_sorts_words_before_merging() -> None:
    raw = RawResult(
        words=[Word(1.0, 1.4, "мир"), Word(0.0, 0.4, "Привет")],
        turns=[SpeakerTurn(0.0, 2.0, A)],
    )
    transcript = merge_result(raw, duration_s=2.0)
    assert [s.text for s in transcript.segments] == ["Привет мир"]
    assert transcript.speakers == [A]


def test_zero_length_word_inside_long_turn_with_short_turns_nearby() -> None:
    # A zero-length word in the middle of a long A turn that contains short B turns.
    turns = [SpeakerTurn(0.0, 100.0, A)] + [SpeakerTurn(10.0 + i, 10.5 + i, B) for i in range(5)]
    assert assign_speakers([Word(50.0, 50.0, "э")], turns) == [A]
