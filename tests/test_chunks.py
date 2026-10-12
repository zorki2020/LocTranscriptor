from __future__ import annotations

from math import inf

import pytest

from transcribator.models import SpeakerTurn
from transcribator.pipeline.chunks import plan_chunks


def turn(start: float, end: float, speaker: str = "A") -> SpeakerTurn:
    return SpeakerTurn(start, end, speaker)


def test_close_turns_of_different_speakers_are_merged() -> None:
    chunks = plan_chunks([turn(0, 2, "A"), turn(2.3, 4, "B")], pad_s=0)
    assert [(c.start, c.end) for c in chunks] == [(0, 4)]


def test_short_gap_between_turns_is_covered_so_interjections_are_not_lost() -> None:
    # An "uh-huh" in a 2 s pause between turns lands in the same chunk.
    chunks = plan_chunks([turn(0, 5), turn(7, 10)], pad_s=0)
    assert [(c.start, c.end) for c in chunks] == [(0, 10)]


def test_distant_turns_stay_separate() -> None:
    chunks = plan_chunks([turn(0, 2), turn(10, 12)], pad_s=0)
    assert [(c.start, c.end) for c in chunks] == [(0, 2), (10, 12)]


def test_long_region_pieces_overlap_and_stay_within_limit() -> None:
    chunks = plan_chunks([turn(0, 50)], max_len_s=20, overlap_s=1.5, pad_s=0)
    assert len(chunks) == 3
    assert all(c.end - c.start <= 20 for c in chunks)
    for left, right in zip(chunks, chunks[1:], strict=False):
        # audio of adjacent chunks overlaps by 2 * overlap, while their own bounds meet
        assert left.end - right.start == pytest.approx(3.0)
        assert left.own_end == pytest.approx(right.own_start)


def test_every_moment_has_exactly_one_owner() -> None:
    chunks = plan_chunks([turn(0, 50)], pad_s=0)
    for moment in (0.0, 5.0, 16.0, 16.67, 25.0, 33.4, 49.9, 50.0):
        owners = [c for c in chunks if c.owns(moment)]
        assert len(owners) == 1, moment


def test_outer_edges_of_a_region_belong_to_edge_chunks() -> None:
    chunks = plan_chunks([turn(0, 50)], pad_s=0)
    assert chunks[0].own_start == -inf
    assert chunks[-1].own_end == inf


def test_padding_expands_outer_edges_but_not_below_zero() -> None:
    chunks = plan_chunks([turn(0.05, 1.0)], pad_s=0.15)
    assert chunks[0].start == 0.0
    assert chunks[0].end == pytest.approx(1.15)


def test_zero_length_turns_are_ignored_and_empty_input_gives_no_chunks() -> None:
    assert plan_chunks([turn(1, 1)]) == []
    assert plan_chunks([]) == []


def test_unsorted_and_overlapping_turns_are_merged() -> None:
    chunks = plan_chunks([turn(3, 5), turn(0, 4)], pad_s=0)
    assert [(c.start, c.end) for c in chunks] == [(0, 5)]
