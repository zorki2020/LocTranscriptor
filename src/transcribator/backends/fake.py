"""Stubs for tests and for format checks without models. The data is synthetic."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from transcribator.models import RawResult, SpeakerTurn, Word


class FakeDiarizer:
    def __init__(self, turns: Sequence[SpeakerTurn]) -> None:
        self._turns = list(turns)

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        return list(self._turns)


class FakeTranscriber:
    def __init__(self, words: Sequence[Word]) -> None:
        self._words = list(words)
        self.received_turns: list[SpeakerTurn] = []

    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        self.received_turns = list(turns)
        return list(self._words)


class FakeEngine:
    """Return a preset raw result without reading the file."""

    def __init__(self, raw: RawResult) -> None:
        self._raw = raw

    def analyze(self, audio_path: Path) -> RawResult:
        return self._raw


def demo_raw_result() -> RawResult:
    """A short synthetic dialogue (Russian) for format checks without models."""
    words = [
        Word(0.2, 0.6, "Привет,"),
        Word(0.7, 1.1, "начнём"),
        Word(1.2, 1.8, "встречу."),
        Word(2.4, 2.9, "Да,"),
        Word(3.0, 3.5, "давайте."),
    ]
    turns = [
        SpeakerTurn(0.0, 2.0, "SPEAKER_00"),
        SpeakerTurn(2.2, 3.6, "SPEAKER_01"),
    ]
    return RawResult(words=words, turns=turns, timings={})
