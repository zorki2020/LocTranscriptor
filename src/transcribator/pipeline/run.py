"""Orchestrator: file check, engine, merging, summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from transcribator.models import Transcript
from transcribator.pipeline.audio import probe_duration
from transcribator.pipeline.interfaces import Engine, Summarizer
from transcribator.pipeline.merge import merge_result


@dataclass(slots=True)
class ProcessResult:
    transcript: Transcript
    summary: list[str]
    duration_s: float
    timings: dict[str, float] = field(default_factory=dict)


def process_audio(
    src: Path,
    engine: Engine,
    *,
    summarizer: Summarizer | None = None,
) -> ProcessResult:
    duration = probe_duration(src)

    started = perf_counter()
    raw = engine.analyze(src)
    engine_seconds = perf_counter() - started

    transcript = merge_result(raw, duration)
    summary = summarizer.summarize(transcript) if summarizer else []

    timings = {**raw.timings, "engine_total": engine_seconds}
    return ProcessResult(transcript, summary, duration, timings)
