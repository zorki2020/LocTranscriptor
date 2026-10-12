"""Splitting speech into length-limited chunks for ASR with a short input limit (e.g. 25 s)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil, inf

from transcribator.models import SpeakerTurn


@dataclass(frozen=True, slots=True)
class Chunk:
    """An audio chunk for ASR.

    Neighbouring chunks of one speech region overlap so that a word at the seam falls
    entirely into at least one of them. A word belongs to the chunk whose "own" bounds
    [own_start, own_end) contain its midpoint: so it is neither duplicated nor lost.
    """

    start: float
    end: float
    own_start: float
    own_end: float

    def owns(self, moment: float) -> bool:
        return self.own_start <= moment < self.own_end


def plan_chunks(
    turns: Sequence[SpeakerTurn],
    *,
    max_len_s: float = 20.0,
    merge_gap_s: float = 2.5,
    pad_s: float = 0.3,
    overlap_s: float = 1.5,
) -> list[Chunk]:
    """Audio chunks covering the speech.

    Turns of different speakers separated by at most `merge_gap_s` are merged. Words get
    their speakers from timestamp merging later, so there is no need to cut at speaker
    boundaries. A larger gap makes regions shorter and avoids running ASR on silence; a
    smaller one covers short replies ("yes", "uh-huh") that diarization may have missed.
    The outer edges of a region are widened by `pad_s` so word onsets are not cut off.
    Long regions are split into chunks so that each, including the overlap, is no longer than
    `max_len_s`.
    """
    intervals = sorted((t.start, t.end) for t in turns if t.end > t.start)
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if merged and start - merged[-1][1] <= merge_gap_s:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    own_max = max_len_s - 2 * overlap_s
    chunks: list[Chunk] = []
    for raw_start, raw_end in merged:
        start = max(raw_start - pad_s, 0.0)
        end = raw_end + pad_s
        parts = max(1, ceil((end - start) / own_max))
        step = (end - start) / parts
        for i in range(parts):
            own_start = start + i * step
            own_end = start + (i + 1) * step
            chunks.append(
                Chunk(
                    start=own_start - (overlap_s if i > 0 else 0.0),
                    end=own_end + (overlap_s if i < parts - 1 else 0.0),
                    # The region edges belong entirely to the outermost chunks.
                    own_start=-inf if i == 0 else own_start,
                    own_end=inf if i == parts - 1 else own_end,
                )
            )
    return chunks
