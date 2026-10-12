"""Core pipeline data types. Time is always in seconds (float)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


@dataclass(frozen=True, slots=True)
class Segment:
    """An utterance of one speaker: consecutive words without a long pause."""

    start: float
    end: float
    speaker: str
    text: str


@dataclass(slots=True)
class RawResult:
    """Raw engine result: timestamped words and speech turns by speaker.

    This is what the engine returns. Merging and formatting come after.
    """

    words: list[Word]
    turns: list[SpeakerTurn]
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "words": [{"start": w.start, "end": w.end, "text": w.text} for w in self.words],
            "turns": [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in self.turns],
            "timings": dict(self.timings),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RawResult:
        return cls(
            words=[Word(float(w["start"]), float(w["end"]), str(w["text"])) for w in data["words"]],
            turns=[
                SpeakerTurn(float(t["start"]), float(t["end"]), str(t["speaker"]))
                for t in data["turns"]
            ],
            timings={str(k): float(v) for k, v in data.get("timings", {}).items()},
        )


@dataclass(slots=True)
class Transcript:
    segments: list[Segment]
    duration_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": self.duration_s,
            "segments": [
                {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text}
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            segments=[
                Segment(float(s["start"]), float(s["end"]), str(s["speaker"]), str(s["text"]))
                for s in data["segments"]
            ],
            duration_s=float(data["duration_s"]),
        )

    @property
    def speakers(self) -> list[str]:
        """Speaker labels in order of first appearance."""
        seen: list[str] = []
        for segment in self.segments:
            if segment.speaker not in seen:
                seen.append(segment.speaker)
        return seen
