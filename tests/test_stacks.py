from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from transcribator import stacks
from transcribator.backends.errors import BackendError
from transcribator.models import SpeakerTurn, Word
from transcribator.stacks import DEFAULT_STACK, STACKS, build_local_engine


def test_unknown_stack_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown stack"):
        build_local_engine("nope")


def test_known_stacks() -> None:
    assert STACKS == ("fluidaudio",)
    assert DEFAULT_STACK in STACKS


@pytest.mark.parametrize("stack", STACKS)
def test_building_engine_loads_no_models(stack: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The backend is imported only on first call: block the import and build the engine.
    monkeypatch.setitem(sys.modules, "transcribator.backends.fluidaudio", None)
    build_local_engine(stack, min_speakers=2, max_speakers=4)


class CountingDiarizer:
    instances = 0

    def __init__(self) -> None:
        CountingDiarizer.instances += 1

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        return [SpeakerTurn(0.0, 1.0, "S1")]


def test_lazy_diarizer_loads_on_call_and_releases_after() -> None:
    CountingDiarizer.instances = 0
    lazy = stacks._LazyDiarizer("fluidaudio", CountingDiarizer)
    assert CountingDiarizer.instances == 0
    assert lazy.diarize(Path("x.wav")) == [SpeakerTurn(0.0, 1.0, "S1")]
    assert not hasattr(lazy, "_impl")  # holds no reference to the model
    lazy.diarize(Path("x.wav"))
    assert CountingDiarizer.instances == 2


class EchoTranscriber:
    instances = 0

    def __init__(self) -> None:
        EchoTranscriber.instances += 1

    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        return [Word(0.0, 0.5, "слово")]


def test_lazy_transcriber_loads_once() -> None:
    EchoTranscriber.instances = 0
    lazy = stacks._LazyTranscriber("fluidaudio", EchoTranscriber)
    assert EchoTranscriber.instances == 0
    lazy.transcribe(Path("x.wav"), [])
    lazy.transcribe(Path("x.wav"), [])
    assert EchoTranscriber.instances == 1


def test_load_failure_becomes_code_without_text() -> None:
    def broken() -> CountingDiarizer:
        raise RuntimeError("/Users/someone/Встреча с Иваном/кэш")

    with pytest.raises(BackendError) as info:
        stacks._LazyDiarizer("fluidaudio", broken).diarize(Path("x.wav"))
    assert info.value.code == "fluidaudio_load_failed"
    assert "Иван" not in str(info.value)
    assert info.value.__cause__ is None


def test_engine_passes_language_to_fluidaudio(monkeypatch: pytest.MonkeyPatch) -> None:
    from transcribator.backends import fluidaudio  # noqa: PLC0415

    seen: list[str] = []

    class Recorder:
        def __init__(self, *, language: str) -> None:
            seen.append(language)

        def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
            return []

    monkeypatch.setattr(fluidaudio, "FluidAudioTranscriber", Recorder)
    engine = build_local_engine("fluidaudio", language="ru")
    engine._transcriber.transcribe(Path("x.wav"), [])
    assert seen == ["ru"]
