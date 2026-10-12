"""Building an engine by stack name.

- `fluidaudio`: FluidAudio end to end, Parakeet v3 and offline diarization (CoreML,
  Neural Engine).

Other candidates (parakeet-mlx, Senko, mlx-whisper) were benchmarked and dropped in
favour of FluidAudio. Models load on first call, not when the
engine is built, so load time counts towards the stage timing. The diarizer is released
right after use, before ASR loads: with 16 GB we never keep two models in memory at once.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from transcribator.backends.errors import BackendError
from transcribator.models import SpeakerTurn, Word
from transcribator.pipeline.engine import LocalEngine
from transcribator.pipeline.interfaces import Diarizer, Transcriber

STACKS = ("fluidaudio",)
DEFAULT_STACK = "fluidaudio"


def _load[T](name: str, factory: Callable[[], T]) -> T:
    try:
        return factory()
    except BackendError:
        raise
    except Exception:
        # The load exception text contains cache paths and more: only the code goes out.
        raise BackendError(f"{name}_load_failed") from None


class _LazyTranscriber:
    def __init__(self, name: str, factory: Callable[[], Transcriber]) -> None:
        self._name = name
        self._factory = factory
        self._impl: Transcriber | None = None

    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        if self._impl is None:
            self._impl = _load(self._name, self._factory)
        return self._impl.transcribe(wav_path, turns)


class _LazyDiarizer:
    """Create a diarizer for a single call and do not keep it afterwards: the model lives
    in memory only during diarization (for FluidAudio, while the subprocess runs)."""

    def __init__(self, name: str, factory: Callable[[], Diarizer]) -> None:
        self._name = name
        self._factory = factory

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        return _load(self._name, self._factory).diarize(wav_path)


def build_local_engine(
    stack: str,
    *,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    tmp_root: Path | None = None,
    language: str = "en",
) -> LocalEngine:
    if stack not in STACKS:
        raise ValueError(f"unknown stack: {stack!r}, expected one of {STACKS}")
    return LocalEngine(
        _LazyTranscriber("fluidaudio", lambda: _fluidaudio_transcriber(language)),
        _LazyDiarizer(
            "fluidaudio",
            lambda: _fluidaudio_diarizer(min_speakers, max_speakers),
        ),
        tmp_root,
    )


def _fluidaudio_transcriber(language: str) -> Transcriber:
    from transcribator.backends.fluidaudio import FluidAudioTranscriber  # noqa: PLC0415

    return FluidAudioTranscriber(language=language)


def _fluidaudio_diarizer(min_speakers: int | None, max_speakers: int | None) -> Diarizer:
    from transcribator.backends.fluidaudio import FluidAudioDiarizer  # noqa: PLC0415

    return FluidAudioDiarizer(min_speakers=min_speakers, max_speakers=max_speakers)
