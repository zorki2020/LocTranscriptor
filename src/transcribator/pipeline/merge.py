"""Merging words with speakers and building utterances.

Each word gets the speaker with the largest time overlap.
A word in a pause between utterances gets the speaker closest in time.
Consecutive words of one speaker form an utterance.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Sequence

from transcribator.models import RawResult, Segment, SpeakerTurn, Transcript, Word

DEFAULT_SPEAKER = "SPEAKER_00"

# Characters that attach to the previous word in text without a space.
_CLOSING = re.compile(r"^[.,!?;:…)\]»”]+$")


def assign_speakers(words: Sequence[Word], turns: Sequence[SpeakerTurn]) -> list[str]:
    """The speaker for each word, in the same order as `words`."""
    if not turns:
        return [DEFAULT_SPEAKER] * len(words)

    ordered = sorted(turns, key=lambda t: (t.start, t.end))
    starts = [t.start for t in ordered]
    max_end: list[float] = []
    running = float("-inf")
    for turn in ordered:
        running = max(running, turn.end)
        max_end.append(running)

    return [_speaker_for(word, ordered, starts, max_end) for word in words]


def _speaker_for(
    word: Word,
    ordered: Sequence[SpeakerTurn],
    starts: Sequence[float],
    max_end: Sequence[float],
) -> str:
    # All turns from index `hi` on start no earlier than the word ends: no overlap.
    hi = bisect_left(starts, word.end)

    best: str | None = None
    best_overlap = 0.0
    j = hi - 1
    while j >= 0 and max_end[j] > word.start:
        turn = ordered[j]
        overlap = min(word.end, turn.end) - max(word.start, turn.start)
        if overlap > best_overlap:
            best, best_overlap = turn.speaker, overlap
        elif best is None and word.end == word.start and turn.start <= word.start <= turn.end:
            # A zero-length word inside a turn: take the latest turn containing it.
            best = turn.speaker
        j -= 1
    if best is not None:
        return best

    # A word in a pause (or zero-length): take the turn closest in time.
    middle = (word.start + word.end) / 2
    lo = max(hi - 3, 0)
    candidates = list(enumerate(ordered[lo : hi + 1], start=lo))
    _, nearest = min(candidates, key=lambda item: (_distance(middle, item[1]), item[0]))
    return nearest.speaker


def _distance(moment: float, turn: SpeakerTurn) -> float:
    if turn.start <= moment <= turn.end:
        return 0.0
    return min(abs(moment - turn.start), abs(moment - turn.end))


def build_segments(
    words: Sequence[Word],
    speakers: Sequence[str],
    *,
    max_gap_s: float = 2.0,
    max_segment_s: float = 60.0,
) -> list[Segment]:
    """Build utterances. A new utterance starts on a speaker change, a pause longer than
    `max_gap_s`, or when the utterance grows longer than `max_segment_s`."""
    segments: list[Segment] = []
    tokens: list[str] = []
    speaker = ""
    start = end = 0.0

    def flush() -> None:
        if tokens:
            segments.append(Segment(start, end, speaker, _join(tokens)))
            tokens.clear()

    for word, word_speaker in zip(words, speakers, strict=True):
        text = word.text.strip()
        if not text:
            continue
        if _CLOSING.match(text):
            # A punctuation mark always stays in the current utterance.
            if tokens:
                tokens.append(text)
                end = max(end, word.end)
            continue
        if tokens and (
            word_speaker != speaker
            or word.start - end > max_gap_s
            or word.end - start > max_segment_s
        ):
            flush()
        if not tokens:
            speaker, start = word_speaker, word.start
        tokens.append(text)
        end = max(end, word.end)
    flush()
    return segments


def _join(tokens: Sequence[str]) -> str:
    out = ""
    for token in tokens:
        if out and not _CLOSING.match(token):
            out += " "
        out += token
    return out


def merge_result(raw: RawResult, duration_s: float) -> Transcript:
    """Turn the engine's raw result into a finished transcript."""
    words = sorted(raw.words, key=lambda w: (w.start, w.end))
    speakers = assign_speakers(words, raw.turns)
    return Transcript(segments=build_segments(words, speakers), duration_s=duration_s)
