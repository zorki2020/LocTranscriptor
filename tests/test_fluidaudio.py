from __future__ import annotations

import json
from pathlib import Path

import pytest

from transcribator.backends import fluidaudio
from transcribator.backends.errors import BackendError
from transcribator.backends.fluidaudio import (
    FluidAudioDiarizer,
    FluidAudioTranscriber,
    parse_asr_json,
    parse_diarization_json,
)
from transcribator.models import SpeakerTurn, Word

ASR_JSON = {
    "audioFile": "work/audio.wav",
    "text": "Привет всем. Начнём.",
    "wordTimings": [
        {"word": " Привет", "startTime": 0.2, "endTime": 0.6, "confidence": 0.9},
        {"word": "всем.", "startTime": 0.7, "endTime": 1.0, "confidence": 0.8},
        {"word": "  ", "startTime": 1.0, "endTime": 1.1, "confidence": 0.1},
        {"word": "Начнём.", "startTime": 1.4, "endTime": 1.9, "confidence": 0.9},
    ],
}

DIAR_JSON = {
    "audioFile": "work/audio.wav",
    "segments": [
        {"speakerId": "S2", "startTimeSeconds": 3.0, "endTimeSeconds": 4.5, "embedding": []},
        {"speakerId": "S1", "startTimeSeconds": 0.0, "endTimeSeconds": 2.8, "embedding": []},
        {"speakerId": "S1", "startTimeSeconds": 5.0, "endTimeSeconds": 5.0, "embedding": []},
    ],
}


class FakeRunner:
    """Fake fluidaudiocli: writes the given JSON where it was asked to."""

    def __init__(self, data: object, out_flag: str) -> None:
        self.data = data
        self.out_flag = out_flag
        self.calls: list[list[str]] = []
        self.out_paths: list[Path] = []

    def __call__(self, args: list[str]) -> None:
        self.calls.append(args)
        out = Path(args[args.index(self.out_flag) + 1])
        self.out_paths.append(out)
        out.write_text(json.dumps(self.data), encoding="utf-8")


def test_asr_words_are_stripped_and_empty_ones_dropped() -> None:
    assert parse_asr_json(ASR_JSON) == [
        Word(0.2, 0.6, "Привет"),
        Word(0.7, 1.0, "всем."),
        Word(1.4, 1.9, "Начнём."),
    ]


def test_diarization_segments_are_sorted_and_empty_dropped() -> None:
    assert parse_diarization_json(DIAR_JSON) == [
        SpeakerTurn(0.0, 2.8, "S1"),
        SpeakerTurn(3.0, 4.5, "S2"),
    ]


def test_asr_negative_and_inverted_times_are_clamped_not_dropped() -> None:
    data = {
        "wordTimings": [
            {"word": "минус", "startTime": -0.2, "endTime": 0.3},
            {"word": "весь", "startTime": -0.5, "endTime": -0.1},
            {"word": "назад", "startTime": 1.0, "endTime": 0.8},
            {"word": "", "startTime": 1.1, "endTime": 1.2},
        ]
    }
    assert parse_asr_json(data) == [
        Word(0.0, 0.3, "минус"),
        Word(0.0, 0.0, "весь"),
        Word(1.0, 1.0, "назад"),
    ]


def test_no_speech_marker_constant_is_separate_from_code() -> None:
    assert fluidaudio._NO_SPEECH_MARKER == b"noSpeechDetected"
    assert fluidaudio.NO_SPEECH == "fluidaudio_no_speech"


@pytest.mark.parametrize(
    "bad", [[1, 2], {"wordTimings": [{"word": "x"}]}, None, {"text": "формат поменялся"}]
)
def test_bad_asr_json_raises_code_without_content(bad: object) -> None:
    with pytest.raises(BackendError) as info:
        parse_asr_json(bad)
    assert info.value.code == "fluidaudio_bad_output"


def test_transcriber_passes_language_and_cleans_up_json(tmp_path: Path) -> None:
    wav = tmp_path / "audio.wav"
    runner = FakeRunner(ASR_JSON, "--output-json")

    words = FluidAudioTranscriber(run=runner).transcribe(wav, [])

    assert [w.text for w in words] == ["Привет", "всем.", "Начнём."]
    args = runner.calls[0]
    assert args[:2] == ["transcribe", str(wav)]
    assert args[args.index("--language") + 1] == "en"  # default
    assert args[args.index("--model-version") + 1] == "v3"
    assert "--word-timestamps" in args
    assert args[args.index("--model-dir") + 1] == str(fluidaudio.DEFAULT_ASR_MODEL_DIR)
    assert not runner.out_paths[0].exists()
    assert list(tmp_path.iterdir()) == []


def test_transcriber_uses_own_model_dir_and_precision(tmp_path: Path) -> None:
    runner = FakeRunner(ASR_JSON, "--output-json")
    FluidAudioTranscriber(model_dir=tmp_path / "m", run=runner).transcribe(tmp_path / "a.wav", [])
    args = runner.calls[0]
    assert args[args.index("--model-dir") + 1] == str(tmp_path / "m")
    assert args[args.index("--encoder-precision") + 1] == "int8-v2"


def test_diarizer_runs_offline_mode_with_speaker_bounds(tmp_path: Path) -> None:
    runner = FakeRunner(DIAR_JSON, "--output")

    turns = FluidAudioDiarizer(min_speakers=2, max_speakers=5, run=runner).diarize(
        tmp_path / "audio.wav"
    )

    assert len(turns) == 2
    args = runner.calls[0]
    assert args[args.index("--mode") + 1] == "offline"
    assert args[args.index("--min-speakers") + 1] == "2"
    assert args[args.index("--max-speakers") + 1] == "5"
    assert list(tmp_path.iterdir()) == []


def test_missing_output_is_bad_output(tmp_path: Path) -> None:
    with pytest.raises(BackendError, match="fluidaudio_bad_output"):
        FluidAudioDiarizer(run=lambda args: None).diarize(tmp_path / "audio.wav")


def test_missing_cli_raises_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(fluidaudio.CLI_ENV, str(tmp_path / "Личная встреча" / "nope"))
    with pytest.raises(BackendError) as info:
        fluidaudio.run_cli(["--help"])
    assert info.value.code == "fluidaudio_missing"
    assert "Личная" not in str(info.value)


def test_nonzero_exit_raises_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fluidaudio.CLI_ENV, "/usr/bin/false")
    with pytest.raises(BackendError, match="fluidaudio_exit_1"):
        fluidaudio.run_cli([])


def test_diarizer_returns_no_turns_when_no_speech(tmp_path: Path) -> None:
    def no_speech(args: list[str]) -> None:
        raise BackendError(fluidaudio.NO_SPEECH)

    assert FluidAudioDiarizer(run=no_speech).diarize(tmp_path / "audio.wav") == []


def test_cli_no_speech_marker_maps_to_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    script = tmp_path / "fake-cli"
    script.write_text("#!/bin/sh\necho 'ERROR: offline mode: noSpeechDetected' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setenv(fluidaudio.CLI_ENV, str(script))
    with pytest.raises(BackendError) as info:
        fluidaudio.run_cli(["process"])
    assert info.value.code == fluidaudio.NO_SPEECH


def test_no_speech_marker_counts_only_with_exit_code_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "fake-cli"
    script.write_text("#!/bin/sh\necho 'noSpeechDetected' >&2\nexit 2\n")
    script.chmod(0o755)
    monkeypatch.setenv(fluidaudio.CLI_ENV, str(script))
    with pytest.raises(BackendError) as info:
        fluidaudio.run_cli(["process"])
    assert info.value.code == "fluidaudio_exit_2"


def test_diarization_json_without_segments_is_an_error() -> None:
    with pytest.raises(BackendError, match="fluidaudio_bad_output"):
        parse_diarization_json({"speakerCount": 2})


def test_empty_lists_are_valid_silence() -> None:
    assert parse_asr_json({"wordTimings": []}) == []
    assert parse_diarization_json({"segments": []}) == []


def test_transcriber_uses_given_language(tmp_path: Path) -> None:
    runner = FakeRunner(ASR_JSON, "--output-json")
    FluidAudioTranscriber(language="ru", run=runner).transcribe(tmp_path / "a.wav", [])
    args = runner.calls[0]
    assert args[args.index("--language") + 1] == "ru"
