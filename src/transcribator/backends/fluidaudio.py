"""FluidAudio (Swift, CoreML and the Neural Engine): Parakeet v3 ASR and offline diarization.

Called as the external program `fluidaudiocli`, built from source. The path
is set by `TRANSCRIBATOR_FLUIDAUDIO_CLI`. In the app (`TRANSCRIBATOR_MODELS_DIR` set) the
models come from the app's models directory: ASR through `--model-dir`, diarization through
`CFFIXED_USER_HOME` (FluidAudio has no flag for it and looks in
`~/Library/Application Support/FluidAudio/Models` of that home).
The program only ever receives the temporary normalized WAV from `LocalEngine`, so the
recording's name ends up neither in the arguments nor in the `audioFile` field of its JSON.
The program's output is not read (except the "no speech" marker): it may contain meeting
text. The JSON is written to a temporary folder and deleted right after parsing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from transcribator.backends.errors import BackendError
from transcribator.models import SpeakerTurn, Word
from transcribator.modelstore import models_dir

CLI_ENV = "TRANSCRIBATOR_FLUIDAUDIO_CLI"
# Same override as scripts/install_fluidaudio.sh uses for the install location.
_TOOLS = Path(
    os.environ.get("TRANSCRIBATOR_TOOLS_DIR")
    or Path.home() / ".local/share/transcribator-tools/fluidaudio"
)
DEFAULT_CLI = _TOOLS / "bin/fluidaudiocli"
# Our own ASR model directory rather than FluidAudio's shared cache: other programs on
# this Mac may fill that cache, and the model revision there is unknown. `--model-dir`
# expects the model repository directory itself. Diarization has no such flag and uses
# the shared cache.
DEFAULT_ASR_MODEL_DIR = _TOOLS / "models/parakeet-tdt-0.6b-v3"
ASR_MODEL_NAME = "parakeet-tdt-0.6b-v3"
TIMEOUT_S = 3 * 3600
NO_SPEECH = "fluidaudio_no_speech"
# Marker in stderr when offline diarization found no speech (exit code 1). It is the only
# thing taken from stderr: the rest of the output is not read and never leaves.
_NO_SPEECH_MARKER = b"noSpeechDetected"

Runner = Callable[[list[str]], None]


def cli_path() -> Path:
    return Path(os.environ.get(CLI_ENV) or DEFAULT_CLI)


def asr_model_dir() -> Path:
    root = models_dir()
    return root / ASR_MODEL_NAME if root is not None else DEFAULT_ASR_MODEL_DIR


def cli_env() -> dict[str, str] | None:
    """Environment for the program: in the app, a home directory inside the models
    directory, so FluidAudio finds the diarization models (and keeps its caches) there."""
    root = models_dir()
    if root is None:
        return None
    return {**os.environ, "CFFIXED_USER_HOME": str(root / "home")}


def run_cli(args: list[str]) -> None:
    """Run `fluidaudiocli` without a shell or stdin; output is discarded."""
    try:
        proc = subprocess.run(
            [str(cli_path()), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=TIMEOUT_S,
            env=cli_env(),
        )
    except FileNotFoundError:
        raise BackendError("fluidaudio_missing") from None
    except subprocess.TimeoutExpired:
        raise BackendError("fluidaudio_timeout") from None
    if proc.returncode != 0:
        if proc.returncode == 1 and _NO_SPEECH_MARKER in proc.stderr:
            raise BackendError(NO_SPEECH)
        raise BackendError(f"fluidaudio_exit_{proc.returncode}")


def _run_to_json(run: Runner, wav_path: Path, args: Sequence[str], out_flag: str) -> Any:
    # Next to the WAV: that is the job's temporary folder, deleted on any exit.
    with tempfile.TemporaryDirectory(prefix="fluidaudio-", dir=wav_path.parent) as tmp:
        out = Path(tmp) / "out.json"
        run([*args, out_flag, str(out)])
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise BackendError("fluidaudio_bad_output") from None


class FluidAudioTranscriber:
    def __init__(
        self,
        *,
        language: str = "en",
        model_version: str = "v3",
        # int8-v2: a rebuilt int8 encoder that works around issue #760. On the synthetic
        # Russian sample it fixed a misheard word that plain int8 got wrong.
        encoder_precision: str = "int8-v2",
        model_dir: Path | None = None,
        run: Runner = run_cli,
    ) -> None:
        self._language = language
        self._model_version = model_version
        self._encoder_precision = encoder_precision
        self._model_dir = model_dir or asr_model_dir()
        self._run = run

    def transcribe(self, wav_path: Path, turns: Sequence[SpeakerTurn]) -> list[Word]:
        # turns are not needed: FluidAudio splits the file into windows itself.
        args = [
            "transcribe",
            str(wav_path),
            "--model-version",
            self._model_version,
            "--language",
            self._language,
            "--word-timestamps",
            "--encoder-precision",
            self._encoder_precision,
            "--model-dir",
            str(self._model_dir),
        ]
        return parse_asr_json(_run_to_json(self._run, wav_path, args, "--output-json"))


class FluidAudioDiarizer:
    def __init__(
        self,
        *,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
        run: Runner = run_cli,
    ) -> None:
        self._min_speakers = min_speakers
        self._max_speakers = max_speakers
        self._run = run

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        args = ["process", str(wav_path), "--mode", "offline"]
        if self._min_speakers:
            args += ["--min-speakers", str(self._min_speakers)]
        if self._max_speakers:
            args += ["--max-speakers", str(self._max_speakers)]
        try:
            data = _run_to_json(self._run, wav_path, args, "--output")
        except BackendError as exc:
            if exc.code == NO_SPEECH:
                return []  # silence: no utterances, not an error
            raise
        return parse_diarization_json(data)


def parse_asr_json(data: Any) -> list[Word]:
    """`wordTimings` [{word, startTime, endTime, confidence}] -> words, seconds from start.

    Only words with empty text are dropped: the transcript is verbatim. A negative start
    is raised to zero, an end before the start is pulled up to the start.
    """
    try:
        words = []
        # A missing key means the format changed. That is an error, not "an empty recording".
        for item in data["wordTimings"]:
            text = str(item["word"]).strip()
            if not text:
                continue
            start = max(0.0, float(item["startTime"]))
            end = max(float(item["endTime"]), start)
            words.append(Word(start, end, text))
    except (AttributeError, KeyError, TypeError, ValueError):
        raise BackendError("fluidaudio_bad_output") from None
    return words


def parse_diarization_json(data: Any) -> list[SpeakerTurn]:
    """`segments` [{speakerId, startTimeSeconds, endTimeSeconds}] -> speaker turns."""
    try:
        turns = [
            SpeakerTurn(
                float(item["startTimeSeconds"]),
                float(item["endTimeSeconds"]),
                str(item["speakerId"]),
            )
            for item in data["segments"]
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        raise BackendError("fluidaudio_bad_output") from None
    return sorted((t for t in turns if t.end > t.start), key=lambda t: (t.start, t.end))
