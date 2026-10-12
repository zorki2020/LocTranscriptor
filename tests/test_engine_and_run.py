from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator.backends.fake import FakeDiarizer, FakeEngine, FakeTranscriber, demo_raw_result
from transcribator.models import SpeakerTurn, Word
from transcribator.pipeline.audio import AudioError
from transcribator.pipeline.engine import LocalEngine
from transcribator.pipeline.run import process_audio

pytestmark = requires_ffmpeg

TURNS = [SpeakerTurn(0.0, 1.0, "SPEAKER_00")]
WORDS = [Word(0.1, 0.5, "тест")]


def test_engine_passes_diarization_to_transcriber_and_reports_timings(
    sine_audio: Path, tmp_path: Path
) -> None:
    transcriber = FakeTranscriber(WORDS)
    engine = LocalEngine(transcriber, FakeDiarizer(TURNS), tmp_root=tmp_path)

    raw = engine.analyze(sine_audio)

    assert raw.words == WORDS
    assert raw.turns == TURNS
    assert transcriber.received_turns == TURNS
    assert set(raw.timings) == {"normalize", "diarize", "transcribe"}


def test_engine_removes_temporary_files_after_success(sine_audio: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    LocalEngine(FakeTranscriber(WORDS), FakeDiarizer(TURNS), tmp_root=work).analyze(sine_audio)
    assert list(work.iterdir()) == []


class ExplodingTranscriber:
    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        raise RuntimeError("модель упала")


def test_engine_removes_temporary_files_after_failure(sine_audio: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    engine = LocalEngine(ExplodingTranscriber(), FakeDiarizer(TURNS), tmp_root=work)
    with pytest.raises(RuntimeError):
        engine.analyze(sine_audio)
    assert list(work.iterdir()) == []


def test_process_audio_builds_transcript_from_engine_result(sine_audio: Path) -> None:
    result = process_audio(sine_audio, FakeEngine(demo_raw_result()))

    assert [s.speaker for s in result.transcript.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert result.transcript.segments[0].text == "Привет, начнём встречу."
    assert result.summary == []
    assert result.duration_s == pytest.approx(2.0, abs=0.2)
    assert "engine_total" in result.timings


def test_process_audio_rejects_non_audio_before_running_models(tmp_path: Path) -> None:
    bad = tmp_path / "x.mp3"
    bad.write_bytes(b"not audio")
    with pytest.raises(AudioError):
        process_audio(bad, FakeEngine(demo_raw_result()))
