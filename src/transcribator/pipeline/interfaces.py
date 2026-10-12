"""Model interfaces. The concrete models behind them are replaceable."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from transcribator.models import RawResult, SpeakerTurn, Transcript, Word


class Diarizer(Protocol):
    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        """Speech turns by speaker for the whole file at once (not in chunks)."""
        ...


class Transcriber(Protocol):
    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        """Words with timestamps in seconds from the start of the file.

        `turns` is the diarization result: backends with a short input limit cut the audio by them,
        backends that split the file into windows themselves (FluidAudio) ignore them.
        """
        ...


class Summarizer(Protocol):
    def summarize(self, transcript: Transcript) -> list[str]:
        """3-5 lines of a short summary."""
        ...


class Engine(Protocol):
    """Everything heavy: normalization, diarization and word recognition.

    Implementation: `LocalEngine`, models in the current process on this Mac.
    """

    def analyze(self, audio_path: Path) -> RawResult: ...
