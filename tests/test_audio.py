from __future__ import annotations

import subprocess
import traceback
import wave
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator.pipeline import audio
from transcribator.pipeline.audio import AudioError, cut_wav, normalize_to_wav, probe_duration

PRIVATE_TITLE = "Секретная встреча про увольнение"


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


@requires_ffmpeg
def test_probe_duration_of_generated_tone(sine_audio: Path) -> None:
    assert probe_duration(sine_audio) == pytest.approx(2.0, abs=0.2)


@requires_ffmpeg
def test_normalize_gives_mono_16k_wav(sine_audio: Path, tmp_path: Path) -> None:
    wav_path = tmp_path / "out.wav"
    normalize_to_wav(sine_audio, wav_path)
    with wave.open(str(wav_path)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2


@requires_ffmpeg
def test_cut_wav_has_requested_length_and_works_without_ffmpeg(
    sine_audio: Path, tmp_path: Path
) -> None:
    wav_path = tmp_path / "full.wav"
    chunk_path = tmp_path / "chunk.wav"
    normalize_to_wav(sine_audio, wav_path)
    cut_wav(wav_path, chunk_path, 0.5, 1.0)
    with wave.open(str(chunk_path)) as chunk:
        assert chunk.getnframes() / chunk.getframerate() == pytest.approx(1.0, abs=0.001)


@requires_ffmpeg
def test_cut_past_the_end_gives_empty_chunk_not_error(sine_audio: Path, tmp_path: Path) -> None:
    wav_path = tmp_path / "full.wav"
    chunk_path = tmp_path / "chunk.wav"
    normalize_to_wav(sine_audio, wav_path)
    cut_wav(wav_path, chunk_path, 100.0, 1.0)
    with wave.open(str(chunk_path)) as chunk:
        assert chunk.getnframes() == 0


def test_cut_of_broken_wav_gives_code(tmp_path: Path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav")
    with pytest.raises(AudioError) as excinfo:
        cut_wav(broken, tmp_path / "out.wav", 0, 1)
    assert excinfo.value.code == "cut_failed"


@requires_ffmpeg
def test_not_an_audio_file_gives_code_without_filename(tmp_path: Path) -> None:
    path = tmp_path / f"{PRIVATE_TITLE}.txt"
    path.write_text("это не аудио", encoding="utf-8")
    with pytest.raises(AudioError) as excinfo:
        probe_duration(path)
    assert excinfo.value.code == "probe_failed"
    assert PRIVATE_TITLE not in _formatted(excinfo.value)


@requires_ffmpeg
def test_missing_file_gives_code(tmp_path: Path) -> None:
    with pytest.raises(AudioError) as excinfo:
        probe_duration(tmp_path / "нет-такого.mp3")
    assert excinfo.value.code == "probe_failed"


def test_timeout_does_not_leak_command_through_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow(cmd: list[str], **_: object) -> None:
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(audio.subprocess, "run", slow)
    with pytest.raises(AudioError) as excinfo:
        probe_duration(tmp_path / f"{PRIVATE_TITLE}.mp3")
    assert excinfo.value.code == "timeout"
    assert PRIVATE_TITLE not in _formatted(excinfo.value)


def test_missing_ffmpeg_gives_code_without_leaking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def missing(cmd: list[str], **_: object) -> None:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(audio.subprocess, "run", missing)
    with pytest.raises(AudioError) as excinfo:
        normalize_to_wav(tmp_path / f"{PRIVATE_TITLE}.mp3", tmp_path / "out.wav")
    assert excinfo.value.code == "ffmpeg_missing"
    assert PRIVATE_TITLE not in _formatted(excinfo.value)


def test_ffmpeg_gets_no_stdin_and_local_protocols_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(audio.subprocess, "run", fake_run)
    normalize_to_wav(tmp_path / "in.mp3", tmp_path / "out.wav")

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert "-nostdin" in cmd
    assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL  # type: ignore[index]
