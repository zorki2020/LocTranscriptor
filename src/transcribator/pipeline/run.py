"""Orchestrator: file check, engine, merging, summary.

Two channel layouts of a recording:
- `mono`: one channel, everybody together. Diarization and recognition on the whole file.
- `mic_system`: a recording made by the app with the microphone on. Channel 0 is the
  microphone (the person recording), channel 1 is the system audio (everybody else). The
  channels are split into temporary mono files and go through the single-channel pipeline
  one after the other, never at the same time (one model in memory at a time): channel 1
  with diarization, channel 0 without it and with the one speaker `MIC_SPEAKER`. A
  microphone channel without speech (`mic_has_speech`) is skipped. The segments of both
  are ordered by start time on the recording's timeline; overlapping speech is kept.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from transcribator.models import RawResult, Segment, Transcript
from transcribator.pipeline.audio import (
    AudioError,
    count_loud_blocks,
    probe_duration,
    split_channels,
)
from transcribator.pipeline.interfaces import Engine, Summarizer
from transcribator.pipeline.merge import merge_channels, merge_result
from transcribator.pipeline.tempfiles import job_prefix

LAYOUT_MONO = "mono"
LAYOUT_MIC_SYSTEM = "mic_system"
LAYOUTS = (LAYOUT_MONO, LAYOUT_MIC_SYSTEM)

# Silent microphone gate. A channel without speech is not recognized at all: a recognizer
# given only room noise can invent words, and the transcript must stay verbatim. The
# channel is cut into short blocks and the RMS level of each is measured; it counts as
# speech only if enough blocks are clearly above a quiet room. A few clicks, a fan or a
# muted microphone stay below the bar.
MIC_GATE_BLOCK_S = 0.1  # 100 ms blocks: short enough for single words
# About -50 dBFS RMS in 16-bit sample units (32768 * 10 ** (-50 / 20)): above a quiet room's
# noise floor, below even quiet speech close to a microphone.
MIC_GATE_RMS = 32768 * 10 ** (-50 / 20)
# The channel is kept when at least this share of blocks is loud...
MIC_GATE_MIN_SHARE = 0.02
# ...and at least this much loud audio in total (15 blocks): a few key clicks or a cough
# in a short recording do not count as speech.
MIC_GATE_MIN_LOUD_S = 1.5


def mic_has_speech(wav: Path) -> bool:
    """True if the microphone channel (mono 16-bit WAV) is loud often enough to hold
    speech. Streamed block by block, the file is never loaded whole."""
    loud, total = count_loud_blocks(wav, MIC_GATE_BLOCK_S, MIC_GATE_RMS)
    needed = max(
        math.ceil(total * MIC_GATE_MIN_SHARE), round(MIC_GATE_MIN_LOUD_S / MIC_GATE_BLOCK_S)
    )
    return loud >= needed


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
    layout: str = LAYOUT_MONO,
    mic_engine: Engine | None = None,
    tmp_root: Path | None = None,
) -> ProcessResult:
    """`mic_engine` recognizes the microphone channel of a `mic_system` recording and
    should not diarize (without it `engine` is used and its speaker turns are ignored).
    `tmp_root` sets where the channel files are created."""
    if layout not in LAYOUTS:
        raise AudioError("bad_layout")
    duration = probe_duration(src)

    started = perf_counter()
    if layout == LAYOUT_MIC_SYSTEM:
        transcript, timings = _two_channels(src, engine, mic_engine or engine, duration, tmp_root)
        engine_seconds = perf_counter() - started
    else:
        raw = engine.analyze(src)
        engine_seconds = perf_counter() - started
        transcript = merge_result(raw, duration)
        timings = dict(raw.timings)
    summary = summarizer.summarize(transcript) if summarizer else []

    timings["engine_total"] = engine_seconds
    return ProcessResult(transcript, summary, duration, timings)


def _two_channels(
    src: Path, engine: Engine, mic_engine: Engine, duration: float, tmp_root: Path | None
) -> tuple[Transcript, dict[str, float]]:
    timings: dict[str, float] = {}
    mine: list[Segment] = []
    # The channel files hold the whole meeting: deleted on any exit, including errors.
    with tempfile.TemporaryDirectory(prefix=job_prefix(), dir=tmp_root) as tmp:
        mic_wav = Path(tmp) / "channel0.wav"
        system_wav = Path(tmp) / "channel1.wav"
        started = perf_counter()
        split_channels(src, mic_wav, system_wav)
        timings["split"] = perf_counter() - started

        mic_silent = not mic_has_speech(mic_wav)
        if mic_silent:
            mic_wav.unlink()

        system = engine.analyze(system_wav)
        system_wav.unlink()
        timings.update(system.timings)
        others = merge_result(system, duration).segments

        if not mic_silent:
            mic = mic_engine.analyze(mic_wav)
            timings.update({f"mic_{name}": value for name, value in mic.timings.items()})
            # One known speaker: speaker turns, if any, are ignored.
            mine = merge_result(RawResult(words=mic.words, turns=[]), duration).segments
    return merge_channels(others, mine, duration), timings
