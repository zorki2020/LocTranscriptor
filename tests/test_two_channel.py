"""Two-channel recordings (microphone + system audio): splitting, recognition per channel,
merging, the "me" speaker, the layout through the worker. Synthetic audio only, generated
at run time; the engines are stubs."""

from __future__ import annotations

import json
import math
import os
import time
import wave
from array import array
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator import cli
from transcribator.backends.fake import FakeEngine
from transcribator.models import RawResult, Segment, SpeakerTurn, Transcript, Word
from transcribator.paths import db_path, uploads_dir
from transcribator.pipeline import audio
from transcribator.pipeline.audio import AudioError
from transcribator.pipeline.format_md import (
    default_speaker_names,
    render_markdown,
    speaker_labels,
)
from transcribator.pipeline.merge import MIC_SPEAKER, merge_channels
from transcribator.pipeline.run import (
    MIC_GATE_BLOCK_S,
    MIC_GATE_RMS,
    mic_has_speech,
    process_audio,
)
from transcribator.storage import db, repo
from transcribator.worker import loop
from transcribator.worker.loop import FAILED_UPLOAD_TTL, ORPHAN_UPLOAD_AGE_S, JobError, Worker

MIC_AMP = 0.5
SYSTEM_AMP = 0.2
REC_REF = "rec0123456789abcdef01234567.wav"


def _stereo(path: Path, mic_amp: float, system_amp: float, seconds: float = 2.0) -> Path:
    """Stereo 16-bit WAV like the app writes: channel 0 microphone, channel 1 system."""
    rate = 16000
    frames = bytearray()
    for i in range(int(seconds * rate)):
        mic = int(mic_amp * 32767 * math.sin(2 * math.pi * 300 * i / rate))
        system = int(system_amp * 32767 * math.sin(2 * math.pi * 500 * i / rate))
        frames += mic.to_bytes(2, "little", signed=True)
        frames += system.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))
    return path


class ChannelEngine:
    """Returns a preset result and records what it was given: the channel's peak and
    channel count (read while the file exists) and the call order."""

    def __init__(self, name: str, raw: RawResult, order: list[str]) -> None:
        self.name = name
        self.raw = raw
        self.order = order
        self.peaks: list[int] = []
        self.paths: list[Path] = []

    def analyze(self, audio_path: Path) -> RawResult:
        with wave.open(str(audio_path), "rb") as reader:
            assert reader.getnchannels() == 1
            assert reader.getframerate() == 16000
        self.paths.append(audio_path)
        self.peaks.append(_peak(audio_path))
        self.order.append(self.name)
        return self.raw


def _peak(path: Path) -> int:
    """Largest absolute sample of a 16-bit mono WAV (test helper)."""
    with wave.open(str(path), "rb") as reader:
        samples = array("h", reader.readframes(reader.getnframes()))
    return max(max(samples), -min(samples))


class ExplodingEngine:
    def analyze(self, audio_path: Path) -> RawResult:
        raise RuntimeError("модель упала")


SYSTEM_RAW = RawResult(
    words=[Word(0.2, 0.6, "Привет,"), Word(0.7, 1.1, "коллеги."), Word(1.6, 1.9, "Да.")],
    turns=[SpeakerTurn(0.0, 1.2, "S1"), SpeakerTurn(1.5, 2.0, "S2")],
    timings={"normalize": 0.1, "diarize": 0.2, "transcribe": 0.3},
)
# Starts while S1 is still talking (overlap) and continues between S1 and S2.
MIC_RAW = RawResult(
    words=[Word(0.9, 1.3, "Здравствуйте,"), Word(1.35, 1.7, "начнём.")],
    turns=[SpeakerTurn(0.0, 2.0, "SPEAKER_07")],  # ignored: the channel has one speaker
    timings={"normalize": 0.1, "transcribe": 0.2},
)


# --- splitting and silence ---------------------------------------------------------------


@requires_ffmpeg
def test_split_with_ffmpeg_keeps_channels_apart(tmp_path: Path) -> None:
    src = _stereo(tmp_path / "in", MIC_AMP, SYSTEM_AMP)
    out0, out1 = tmp_path / "c0.wav", tmp_path / "c1.wav"
    audio.split_channels(src, out0, out1)
    for out in (out0, out1):
        with wave.open(str(out), "rb") as reader:
            assert reader.getnchannels() == 1
            assert reader.getframerate() == 16000
            assert reader.getsampwidth() == 2
            assert reader.getnframes() == pytest.approx(32000, abs=200)
    assert _peak(out0) == pytest.approx(MIC_AMP * 32767, rel=0.03)
    assert _peak(out1) == pytest.approx(SYSTEM_AMP * 32767, rel=0.03)


@requires_ffmpeg
def test_split_with_ffmpeg_refuses_mono_and_leaves_nothing(tmp_path: Path) -> None:
    src = tmp_path / "mono.wav"
    cli.write_tone(src, seconds=0.5)
    out0, out1 = tmp_path / "c0.wav", tmp_path / "c1.wav"
    with pytest.raises(AudioError) as exc:
        audio.split_channels(src, out0, out1)
    assert exc.value.code == "not_two_channels"
    assert not out0.exists() and not out1.exists()


@requires_ffmpeg
def test_split_failure_removes_partial_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _stereo(tmp_path / "in", MIC_AMP, SYSTEM_AMP, seconds=0.5)
    out0, out1 = tmp_path / "c0.wav", tmp_path / "c1.wav"

    def half_done(src: Path, a: Path, b: Path) -> None:
        a.write_bytes(b"partial")
        raise AudioError("split_failed")

    monkeypatch.setattr(audio, "_split_ffmpeg", half_done)
    with pytest.raises(AudioError):
        audio.split_channels(src, out0, out1)
    assert not out0.exists() and not out1.exists()


def test_split_outputs_must_share_a_directory(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    with pytest.raises(AudioError) as exc:
        audio.split_channels(tmp_path / "in", tmp_path / "a" / "c0.wav", tmp_path / "c1.wav")
    assert exc.value.code == "split_failed"


def _mono(path: Path, samples: list[int]) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(array("h", samples).tobytes())
    return path


def _tone(seconds: float, dbfs: float) -> list[int]:
    """Sine whose RMS is `dbfs` relative to full scale."""
    amp = 32768 * 10 ** (dbfs / 20) * math.sqrt(2)
    return [int(amp * math.sin(2 * math.pi * 300 * i / 16000)) for i in range(int(seconds * 16000))]


def _noise_with_clicks(seconds: float, clicks: int) -> list[int]:
    """Noise floor around -70 dBFS plus a few 5 ms full-scale clicks (key presses)."""
    # Deterministic pseudo-noise within +-18 (about -70 dBFS RMS).
    samples = [(i * 7919) % 37 - 18 for i in range(int(seconds * 16000))]
    for n in range(clicks):
        at = int((n + 0.5) * len(samples) / clicks)
        for i in range(80):
            samples[at + i] = 30000 if i % 2 else -30000
    return samples


SECONDS = 30.0


@pytest.mark.parametrize(
    ("name", "samples", "kept"),
    [
        ("digital_silence", [0] * int(SECONDS * 16000), False),
        ("noise_and_clicks", _noise_with_clicks(SECONDS, 8), False),
        ("quiet_tone", _tone(SECONDS, -56.0), False),
        ("speech_level_tone", _tone(SECONDS, -30.0), True),
    ],
)
def test_microphone_gate(tmp_path: Path, name: str, samples: list[int], kept: bool) -> None:
    assert mic_has_speech(_mono(tmp_path / f"{name}.wav", samples)) is kept


def test_microphone_gate_needs_a_share_and_a_minimum_of_loud_audio(tmp_path: Path) -> None:
    quiet = [0] * int(60 * 16000)
    # 1 s of speech in a minute: below 1.5 s in total.
    short = quiet[:]
    short[16000:32000] = _tone(1.0, -30.0)
    assert not mic_has_speech(_mono(tmp_path / "short.wav", short))
    # 3 s of speech in a minute: 5 % of blocks and above 1.5 s.
    enough = quiet[:]
    enough[16000:64000] = _tone(3.0, -30.0)
    assert mic_has_speech(_mono(tmp_path / "enough.wav", enough))


def test_gate_constants() -> None:
    assert MIC_GATE_BLOCK_S == 0.1
    assert abs(MIC_GATE_RMS - 103.6) < 0.5  # -50 dBFS


def test_count_loud_blocks_streams_blocks_and_reports_codes(tmp_path: Path) -> None:
    path = _mono(tmp_path / "b.wav", [0] * 1600 + [1000] * 1600 + [1000] * 800)
    assert audio.count_loud_blocks(path, 0.1, 500.0) == (2, 3)  # the last block is short
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"RIFF....")
    with pytest.raises(AudioError) as exc:
        audio.count_loud_blocks(broken, 0.1, 500.0)
    assert exc.value.code == "level_failed"
    stereo = _stereo(tmp_path / "s.wav", 0.1, 0.1, seconds=0.2)
    with pytest.raises(AudioError):
        audio.count_loud_blocks(stereo, 0.1, 500.0)


# --- processing --------------------------------------------------------------------------


@requires_ffmpeg
def test_two_channels_run_one_after_another_and_merge_by_time(tmp_path: Path) -> None:
    src = _stereo(tmp_path / "rec", MIC_AMP, SYSTEM_AMP)
    work = tmp_path / "work"
    work.mkdir()
    order: list[str] = []
    system = ChannelEngine("system", SYSTEM_RAW, order)
    mic = ChannelEngine("mic", MIC_RAW, order)

    result = process_audio(src, system, layout="mic_system", mic_engine=mic, tmp_root=work)

    # The system engine got channel 1, the microphone engine channel 0, one at a time.
    assert order == ["system", "mic"]
    assert system.peaks[0] == pytest.approx(SYSTEM_AMP * 32767, rel=0.03)
    assert mic.peaks[0] == pytest.approx(MIC_AMP * 32767, rel=0.03)
    segments = result.transcript.segments
    assert [(s.speaker, s.text) for s in segments] == [
        ("S1", "Привет, коллеги."),
        (MIC_SPEAKER, "Здравствуйте, начнём."),
        ("S2", "Да."),
    ]
    # Timestamps stay on the recording's timeline, the overlap with S1 is kept.
    assert segments[0].end > segments[1].start
    assert segments[1].start == pytest.approx(0.9)
    assert result.duration_s == pytest.approx(2.0, abs=0.05)
    assert {"split", "diarize", "transcribe", "mic_transcribe", "engine_total"} <= set(
        result.timings
    )
    assert "mic_diarize" not in result.timings
    # Channel files are gone after success.
    assert list(work.iterdir()) == []
    assert not any(p.exists() for p in [*system.paths, *mic.paths])


@requires_ffmpeg
def test_silent_microphone_channel_is_skipped(tmp_path: Path) -> None:
    src = _stereo(tmp_path / "rec", 0.0005, SYSTEM_AMP)  # peak ~16: below the threshold
    order: list[str] = []
    mic = ChannelEngine("mic", MIC_RAW, order)
    result = process_audio(
        src, ChannelEngine("system", SYSTEM_RAW, order), layout="mic_system", mic_engine=mic
    )
    assert order == ["system"]
    assert MIC_SPEAKER not in result.transcript.speakers
    assert result.transcript.speakers == ["S1", "S2"]


@requires_ffmpeg
def test_quiet_but_audible_microphone_is_recognized(tmp_path: Path) -> None:
    src = _stereo(tmp_path / "rec", 0.01, SYSTEM_AMP)  # peak ~327: real, if quiet
    order: list[str] = []
    process_audio(
        src,
        ChannelEngine("system", SYSTEM_RAW, order),
        layout="mic_system",
        mic_engine=ChannelEngine("mic", MIC_RAW, order),
    )
    assert order == ["system", "mic"]


@requires_ffmpeg
@pytest.mark.parametrize("failing", ["system", "mic"])
def test_channel_files_are_removed_on_failure(tmp_path: Path, failing: str) -> None:
    src = _stereo(tmp_path / "rec", MIC_AMP, SYSTEM_AMP)
    work = tmp_path / "work"
    work.mkdir()
    order: list[str] = []
    system = ExplodingEngine() if failing == "system" else ChannelEngine("s", SYSTEM_RAW, order)
    mic = ExplodingEngine() if failing == "mic" else ChannelEngine("m", MIC_RAW, order)
    with pytest.raises(RuntimeError):
        process_audio(src, system, layout="mic_system", mic_engine=mic, tmp_root=work)
    assert list(work.iterdir()) == []


@requires_ffmpeg
def test_mono_recording_given_as_mic_system_fails_with_code(tmp_path: Path) -> None:
    src = tmp_path / "mono.wav"
    cli.write_tone(src, seconds=0.5)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(AudioError) as exc:
        process_audio(src, FakeEngine(SYSTEM_RAW), layout="mic_system", tmp_root=work)
    assert exc.value.code == "not_two_channels"
    assert list(work.iterdir()) == []


def test_unknown_layout_is_rejected_before_anything_runs(tmp_path: Path) -> None:
    order: list[str] = []
    engine = ChannelEngine("system", SYSTEM_RAW, order)
    with pytest.raises(AudioError) as exc:
        process_audio(tmp_path / "missing", engine, layout="stereo")
    assert exc.value.code == "bad_layout"
    assert order == []


@requires_ffmpeg
def test_mono_layout_is_unchanged(tmp_path: Path) -> None:
    src = _stereo(tmp_path / "rec", MIC_AMP, SYSTEM_AMP)
    result = process_audio(src, FakeEngine(SYSTEM_RAW))
    assert result.transcript.speakers == ["S1", "S2"]
    assert "split" not in result.timings


# --- merging and names -------------------------------------------------------------------


def test_merge_channels_orders_by_start_and_keeps_overlaps() -> None:
    others = [Segment(0.0, 3.0, "S1", "a"), Segment(5.0, 6.0, "S2", "c")]
    mine = [Segment(2.0, 4.0, "SPEAKER_00", "b"), Segment(5.0, 5.5, "SPEAKER_00", "d")]
    merged = merge_channels(others, mine, 10.0)
    assert [(s.speaker, s.text) for s in merged.segments] == [
        ("S1", "a"),
        (MIC_SPEAKER, "b"),
        (MIC_SPEAKER, "d"),  # same start: the shorter one first
        ("S2", "c"),
    ]
    assert merged.duration_s == 10.0


def test_merge_channels_keeps_a_clashing_other_speaker_apart() -> None:
    merged = merge_channels([Segment(0.0, 1.0, MIC_SPEAKER, "x")], [], 1.0)
    assert merged.speakers != [MIC_SPEAKER]


def _two_channel_transcript() -> Transcript:
    return Transcript(
        [
            Segment(0.0, 1.0, "S1", "Привет."),
            Segment(1.0, 2.0, MIC_SPEAKER, "Да."),
            Segment(2.0, 3.0, "S2", "Ок."),
        ],
        3.0,
    )


@pytest.mark.parametrize(
    ("lang", "me", "speaker"), [("en", "Me", "Speaker"), ("ru", "Я", "Спикер")]
)
def test_me_label_in_markdown(lang: str, me: str, speaker: str) -> None:
    text = render_markdown(_two_channel_transcript(), title="t", lang=lang)
    assert f"**[00:00:01] {me}:** Да." in text
    # The microphone speaker takes no number: the others stay Speaker 1, 2.
    assert f"**[00:00:00] {speaker} 1:** Привет." in text
    assert f"**[00:00:02] {speaker} 2:** Ок." in text


def test_me_speaker_can_be_renamed() -> None:
    labels = speaker_labels(_two_channel_transcript(), {MIC_SPEAKER: "Анна"}, lang="ru")
    assert labels[MIC_SPEAKER] == "Анна"
    text = render_markdown(
        _two_channel_transcript(), title="t", names={MIC_SPEAKER: "Anna"}, lang="en"
    )
    assert "**[00:00:01] Anna:** Да." in text


def test_default_speaker_names_without_me_are_unchanged() -> None:
    assert default_speaker_names(["SPEAKER_01", "SPEAKER_00"], lang="en") == {
        "SPEAKER_01": "Speaker 1",
        "SPEAKER_00": "Speaker 2",
    }


# --- command line ------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.parametrize(("lang", "me"), [("en", "Me"), ("ru", "Я")])
def test_cli_fake_engine_two_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lang: str, me: str
) -> None:
    monkeypatch.setenv("TRANSCRIBATOR_UI_LANG", lang)
    src = _stereo(tmp_path / "rec", MIC_AMP, SYSTEM_AMP)
    out = tmp_path / "out.md"
    data = tmp_path / "out.json"
    code = cli.main(
        [
            "process",
            str(src),
            "--engine",
            "fake",
            "--layout",
            "mic_system",
            "--out",
            str(out),
            "--json",
            str(data),
        ]
    )
    assert code == 0
    assert f"] {me}:**" in out.read_text(encoding="utf-8")
    speakers = [s["speaker"] for s in json.loads(data.read_text(encoding="utf-8"))["segments"]]
    assert MIC_SPEAKER in speakers
    assert speakers == ["SPEAKER_00", MIC_SPEAKER, "SPEAKER_01"]  # ordered by time


def test_cli_unknown_layout_fails_with_code(tmp_path: Path) -> None:
    metrics = tmp_path / "m.json"
    code = cli.main(
        [
            "process",
            str(tmp_path / "missing"),
            "--engine",
            "fake",
            "--layout",
            "surround",
            "--json",
            str(tmp_path / "t.json"),
            "--metrics-json",
            str(metrics),
        ]
    )
    assert code == 2
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"error": "bad_layout"}


@requires_ffmpeg
def test_cli_mono_file_with_layout_mic_system_gives_code(tmp_path: Path) -> None:
    src = tmp_path / "mono.wav"
    cli.write_tone(src, seconds=0.5)
    metrics = tmp_path / "m.json"
    code = cli.main(
        [
            "process",
            str(src),
            "--engine",
            "fake",
            "--layout",
            "mic_system",
            "--json",
            str(tmp_path / "t.json"),
            "--metrics-json",
            str(metrics),
        ]
    )
    assert code == 2
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"error": "not_two_channels"}


# --- worker ------------------------------------------------------------------------------


class LayoutProcessor:
    def __init__(self) -> None:
        self.layouts: list[str] = []
        self.sources: list[Path] = []

    def __call__(
        self,
        src: Path,
        work: Path,
        on_start: object = None,
        language: str = "en",
        layout: str = "mono",
    ) -> Transcript:
        self.sources.append(src)
        self.layouts.append(layout)
        return Transcript([Segment(0.0, 1.0, MIC_SPEAKER, "Да.")], 1.0)


def _worker(root: Path, processor: LayoutProcessor) -> Worker:
    root.mkdir(exist_ok=True)
    return Worker(root, processor=processor, keep_awake=False)


def _enqueue(root: Path, ref: str, layout: str) -> int:
    conn = db.connect(db_path(root))
    try:
        return repo.enqueue(
            conn,
            title="Запись",
            recorded_at=None,
            source_kind=repo.SOURCE_UPLOAD,
            source_ref=ref,
            layout=layout,
        )
    finally:
        conn.close()


def _meeting(root: Path, meeting_id: int) -> repo.Meeting:
    conn = db.connect(db_path(root))
    try:
        meeting = repo.get_meeting(conn, meeting_id)
    finally:
        conn.close()
    assert meeting is not None
    return meeting


@pytest.mark.parametrize("layout", ["mono", "mic_system"])
def test_worker_passes_layout_and_accepts_app_recording_ref(tmp_path: Path, layout: str) -> None:
    root = tmp_path / "data"
    processor = LayoutProcessor()
    worker = _worker(root, processor)
    recording = uploads_dir(root) / REC_REF
    recording.write_bytes(b"x" * 100)
    meeting_id = _enqueue(root, REC_REF, layout)

    assert worker.run_once()

    assert processor.layouts == [layout]
    assert processor.sources == [recording]
    assert _meeting(root, meeting_id).status == "done"
    assert not recording.exists()  # deleted after success, like other uploads
    worker.close()


@pytest.mark.parametrize(
    "ref",
    [
        "rec0123456789abcdef0123456.wav",  # 23 hex
        "rec0123456789abcdef012345678.wav",  # 25 hex
        "rec0123456789ABCDEF01234567.wav",
        "rec0123456789abcdef01234567.wav/x",
        "../rec0123456789abcdef01234567.wav",
        "rec0123456789abcdef01234567.wav\n",
        "rec0123456789abcdef01234567.mp3",
    ],
)
def test_worker_rejects_malformed_recording_refs(tmp_path: Path, ref: str) -> None:
    root = tmp_path / "data"
    processor = LayoutProcessor()
    worker = _worker(root, processor)
    meeting_id = _enqueue(root, ref, "mic_system")
    worker.run_once()
    assert _meeting(root, meeting_id).error_code == "bad_upload_ref"
    assert processor.layouts == []
    worker.close()


def test_recover_keeps_app_recordings_only_within_retention(tmp_path: Path) -> None:
    root = tmp_path / "data"
    worker = _worker(root, LayoutProcessor())
    hours_old = time.time() - ORPHAN_UPLOAD_AGE_S - 600
    expired = time.time() - FAILED_UPLOAD_TTL.total_seconds() - 600
    fresh = uploads_dir(root) / "rec0123456789abcdef01234567.wav"
    stale = uploads_dir(root) / "rec89abcdef0123456789abcdef.wav"
    orphan = uploads_dir(root) / ("c" * 32)
    lookalike = uploads_dir(root) / "rec0123456789abcdef01234567.wav.part"
    for path, stamp in (
        (fresh, hours_old),
        (stale, expired),
        (orphan, hours_old),
        (lookalike, hours_old),
    ):
        path.write_bytes(b"x")
        os.utime(path, (stamp, stamp))

    worker.recover()

    assert fresh.exists()  # not enqueued yet: the app retries
    assert not stale.exists()  # past the retention: an orphan like any other
    assert not orphan.exists()
    assert not lookalike.exists()
    worker.close()


def test_process_isolated_passes_layout_to_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    class Result:
        timed_out = False
        returncode = 1

    def capture(cmd: list[str], **kw: object) -> Result:
        seen.append(list(cmd))
        return Result()

    monkeypatch.setattr(loop, "run_isolated", capture)
    with pytest.raises(JobError):
        loop.process_isolated(tmp_path / "a.wav", tmp_path, lambda pgid: None, "ru", "mic_system")
    assert seen[0][seen[0].index("--layout") + 1] == "mic_system"
    with pytest.raises(JobError):
        loop.process_isolated(tmp_path / "a.wav", tmp_path, lambda pgid: None, "ru")
    assert seen[1][seen[1].index("--layout") + 1] == "mono"


def test_process_isolated_rejects_unknown_layout_without_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []
    monkeypatch.setattr(loop, "run_isolated", lambda *a, **kw: started.append(a))
    with pytest.raises(JobError) as exc:
        loop.process_isolated(tmp_path / "a.wav", tmp_path, lambda pgid: None, "en", "quad")
    assert exc.value.code == "bad_layout"
    assert started == []
