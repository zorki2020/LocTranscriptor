from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator import cli
from transcribator.backends.errors import BackendError
from transcribator.cli import main
from transcribator.models import RawResult, SpeakerTurn, Word


@requires_ffmpeg
def test_process_with_fake_engine_writes_markdown(
    sine_audio: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "result.md"

    code = main(
        ["process", str(sine_audio), "--engine", "fake", "--out", str(out), "--title", "Тест"]
    )

    text = out.read_text(encoding="utf-8")
    assert code == 0
    assert text.startswith("# Тест\n")
    assert "## Transcript" in text  # the interface is English by default
    assert "**[00:00:00] Speaker 1:** Привет, начнём встречу." in text
    assert "engine_total" in capsys.readouterr().out


@requires_ffmpeg
def test_process_default_output_goes_next_to_input(sine_audio: Path) -> None:
    assert main(["process", str(sine_audio), "--engine", "fake"]) == 0
    assert sine_audio.with_suffix(".fake.md").exists()


@requires_ffmpeg
def test_non_audio_input_returns_error_code_without_leaking_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "Личная встреча.mp3"
    bad.write_bytes(b"not audio")

    code = main(["process", str(bad), "--engine", "fake"])

    captured = capsys.readouterr()
    assert code == 2
    assert "probe_failed" in captured.err
    assert "Личная" not in captured.err + captured.out


def test_render_rebuilds_markdown_from_saved_raw_json(tmp_path: Path) -> None:
    raw = RawResult(
        words=[Word(0.0, 0.5, "Привет"), Word(0.6, 1.0, "всем")],
        turns=[SpeakerTurn(0.0, 1.0, "SPEAKER_00")],
    )
    raw_path = tmp_path / "meeting.gigaam.raw.json"
    raw_path.write_text(json.dumps({"duration_s": 1.0, **raw.to_dict()}), encoding="utf-8")

    assert main(["render", str(raw_path)]) == 0

    text = raw_path.with_suffix(".md").read_text(encoding="utf-8")
    assert text.startswith("# meeting\n")
    assert "**[00:00:00] Speaker 1:** Привет всем" in text


def test_wer_command_compares_reference_with_transcript(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = tmp_path / "ref.txt"
    reference.write_text("привет всем начнём встречу", encoding="utf-8")
    transcript = tmp_path / "t.md"
    transcript.write_text(
        "# X\n\n## Транскрипт\n\n**[00:00:01] Спикер 1:** Привет всем, начнем встречу.\n",
        encoding="utf-8",
    )

    assert main(["wer", str(reference), str(transcript)]) == 0

    assert "WER: 0.0%" in capsys.readouterr().out


def test_wer_window_ignores_text_outside_reference_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reference = tmp_path / "ref.txt"
    reference.write_text("только это", encoding="utf-8")
    transcript = tmp_path / "t.md"
    transcript.write_text(
        "## Транскрипт\n\n"
        "**[00:00:05] Спикер 1:** только это\n\n"
        "**[00:10:00] Спикер 1:** совсем другой длинный текст дальше\n",
        encoding="utf-8",
    )

    assert main(["wer", str(reference), str(transcript), "--to-s", "60"]) == 0

    assert "WER: 0.0%" in capsys.readouterr().out


def test_io_error_prints_class_not_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "Личная встреча.raw.json"

    assert main(["render", str(missing)]) == 2

    captured = capsys.readouterr()
    assert "FileNotFoundError" in captured.err
    assert "Личная" not in captured.err


@requires_ffmpeg
def test_process_does_not_print_output_path(
    tmp_path: Path, sine_audio: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "Встреча с Иваном.mp3"
    secret.write_bytes(sine_audio.read_bytes())

    assert main(["process", str(secret), "--engine", "fake"]) == 0

    captured = capsys.readouterr()
    assert "Иван" not in captured.out + captured.err
    assert "Done" in captured.out


@requires_ffmpeg
def test_process_writes_metrics_json(sine_audio: Path, tmp_path: Path) -> None:
    metrics = tmp_path / "m.json"
    out = tmp_path / "r.md"

    assert (
        main(
            [
                "process",
                str(sine_audio),
                "--engine",
                "fake",
                "--out",
                str(out),
                "--metrics-json",
                str(metrics),
            ]
        )
        == 0
    )

    data = json.loads(metrics.read_text(encoding="utf-8"))
    assert data["speakers"] == 2
    assert data["segments"] == 2
    assert data["duration_s"] == pytest.approx(2.0, abs=0.2)
    assert "engine_total" in data["timings"]


@requires_ffmpeg
def test_process_failure_writes_only_error_code(tmp_path: Path) -> None:
    bad = tmp_path / "Личное.mp3"
    bad.write_bytes(b"not audio")
    metrics = tmp_path / "m.json"

    assert main(["process", str(bad), "--engine", "fake", "--metrics-json", str(metrics)]) == 2

    assert json.loads(metrics.read_text(encoding="utf-8")) == {"error": "probe_failed"}


def test_commands_force_offline_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    ref = tmp_path / "r.txt"
    ref.write_text("а", encoding="utf-8")
    main(["wer", str(ref), str(ref)])
    assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_models_download_does_not_force_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    seen: dict[str, str | None] = {}

    def fake_download(args: object) -> int:
        seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
        return 0

    monkeypatch.setattr(cli, "_cmd_download", fake_download)
    parser_args = ["models", "download", "--stack", "fluidaudio"]
    assert main(parser_args) == 0
    assert seen["offline"] is None


class _RaisingEngine:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def analyze(self, audio_path: Path) -> object:
        raise self._exc


@requires_ffmpeg
@pytest.mark.parametrize(
    ("exc", "code", "err", "metric"),
    [
        (BackendError("fluidaudio_exit_1"), 3, "fluidaudio_exit_1", "fluidaudio_exit_1"),
        (RuntimeError("Встреча с Иваном"), 1, "RuntimeError", "crash_RuntimeError"),
        (PermissionError("Встреча с Иваном"), 2, "PermissionError", "crash_PermissionError"),
    ],
)
def test_process_failures_print_only_codes(
    sine_audio: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: Exception,
    code: int,
    err: str,
    metric: str,
) -> None:
    monkeypatch.setattr(cli, "_make_engine", lambda args: _RaisingEngine(exc))
    metrics = tmp_path / "m.json"

    assert main(["process", str(sine_audio), "--metrics-json", str(metrics)]) == code

    captured = capsys.readouterr()
    assert err in captured.err
    assert "Иван" not in captured.out + captured.err
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"error": metric}


@requires_ffmpeg
def test_process_refuses_to_overwrite_input(sine_audio: Path) -> None:
    before = sine_audio.read_bytes()
    assert main(["process", str(sine_audio), "--engine", "fake", "--out", str(sine_audio)]) == 2
    assert sine_audio.read_bytes() == before


@requires_ffmpeg
def test_output_is_private_to_owner(sine_audio: Path, tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    previous = os.umask(0o022)
    try:
        assert main(["process", str(sine_audio), "--engine", "fake", "--out", str(out)]) == 0
    finally:
        os.umask(previous)
    assert out.stat().st_mode & 0o077 == 0


@requires_ffmpeg
def test_missing_out_dir_fails_before_processing(sine_audio: Path, tmp_path: Path) -> None:
    metrics = tmp_path / "m.json"
    args = ["process", str(sine_audio), "--engine", "fake", "--metrics-json", str(metrics)]
    assert main([*args, "--out", str(tmp_path / "nope" / "r.md")]) == 2
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"error": "out_dir_missing"}


def test_bench_rejects_non_positive_interval(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["bench", str(tmp_path), "--interval", "0"])


def test_write_tone_is_valid_16k_mono_wav(tmp_path: Path) -> None:
    import wave  # noqa: PLC0415

    from transcribator.cli import write_tone  # noqa: PLC0415

    path = tmp_path / "t.wav"
    write_tone(path, seconds=0.5)
    with wave.open(str(path), "rb") as reader:
        assert (reader.getnchannels(), reader.getframerate(), reader.getsampwidth()) == (
            1,
            16000,
            2,
        )
        assert reader.getnframes() == 8000


@requires_ffmpeg
def test_process_json_only_writes_no_markdown(sine_audio: Path, tmp_path: Path) -> None:
    out = tmp_path / "t.json"
    assert main(["process", str(sine_audio), "--engine", "fake", "--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [s["speaker"] for s in data["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
    assert not sine_audio.with_suffix(".fake.md").exists()


@requires_ffmpeg
def test_process_passes_recording_language(
    sine_audio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def fake_build(stack: str, **kw: object) -> object:
        seen["language"] = str(kw["language"])
        from transcribator.backends.fake import FakeEngine, demo_raw_result  # noqa: PLC0415

        return FakeEngine(demo_raw_result())

    import transcribator.stacks as stacks  # noqa: PLC0415

    monkeypatch.setattr(stacks, "build_local_engine", fake_build)
    out = tmp_path / "r.md"
    assert main(["process", str(sine_audio), "--language", "ru", "--out", str(out)]) == 0
    assert seen == {"language": "ru"}


def test_default_recording_language_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRANSCRIBATOR_LANGUAGE", "ru")
    args = cli._build_parser().parse_args(["process", "x.mp3"])
    assert args.language == "ru"
    monkeypatch.delenv("TRANSCRIBATOR_LANGUAGE")
    assert cli._build_parser().parse_args(["process", "x.mp3"]).language == "en"
