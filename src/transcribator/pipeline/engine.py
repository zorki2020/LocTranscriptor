"""Engine: audio normalization, diarization and recognition on the machine with the models."""

from __future__ import annotations

import tempfile
from pathlib import Path
from time import perf_counter

from transcribator.models import RawResult
from transcribator.pipeline.audio import normalize_to_wav
from transcribator.pipeline.interfaces import Diarizer, Transcriber
from transcribator.pipeline.tempfiles import job_prefix


class LocalEngine:
    """Runs the models in the current process. External programs (FluidAudio)
    are called by the backends as subprocesses.

    Temporary files live only inside `analyze` and are deleted on any exit,
    including on error. If the process is killed without `finally`, the
    leftovers are removed by `tempfiles.cleanup_stale` on the next start. `tmp_root` sets
    where the temporary folder is created.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        diarizer: Diarizer,
        tmp_root: Path | None = None,
    ) -> None:
        self._transcriber = transcriber
        self._diarizer = diarizer
        self._tmp_root = tmp_root

    def analyze(self, audio_path: Path) -> RawResult:
        timings: dict[str, float] = {}
        with tempfile.TemporaryDirectory(prefix=job_prefix(), dir=self._tmp_root) as tmp:
            wav = Path(tmp) / "audio.wav"

            started = perf_counter()
            normalize_to_wav(audio_path, wav)
            timings["normalize"] = perf_counter() - started

            started = perf_counter()
            turns = self._diarizer.diarize(wav)
            timings["diarize"] = perf_counter() - started

            started = perf_counter()
            words = self._transcriber.transcribe(wav, turns)
            timings["transcribe"] = perf_counter() - started
        return RawResult(words=words, turns=turns, timings=timings)
