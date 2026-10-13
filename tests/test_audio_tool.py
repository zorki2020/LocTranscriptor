"""The AVFoundation helper (`macos/AudioTool`), built with swiftc and run on synthetic audio.

Skipped where it can't run: not macOS or no Swift compiler (CI on Linux).
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest

from transcribator.pipeline import audio
from transcribator.pipeline.audio import AudioError

SOURCE = Path(__file__).resolve().parents[1] / "macos/AudioTool/main.swift"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="needs macOS and swiftc",
)


@pytest.fixture(scope="module")
def tool(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("tool")
    binary = out / "transcribator-audio"
    subprocess.run(
        [
            "swiftc",
            "-Onone",
            "-swift-version",
            "6",
            "-module-cache-path",
            str(out / "cache"),
            "-o",
            str(binary),
            str(SOURCE),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return binary


@pytest.fixture
def use_tool(tool: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(audio.AUDIO_TOOL_ENV, str(tool))
    # Tests may already run inside a sandbox; the wrapping itself is tested in test_app_mode.
    monkeypatch.setenv(audio.SANDBOXED_ENV, "1")
    yield


def _stereo_wav(path: Path, seconds: float = 2.0, rate: int = 44100) -> None:
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(0.3 * 32767 * math.sin(2 * math.pi * 440 * i / rate))
        frames += sample.to_bytes(2, "little", signed=True) * 2
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))


def _check_normalized(path: Path, seconds: float) -> None:
    with wave.open(str(path), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getframerate() == 16000
        assert reader.getsampwidth() == 2
        assert abs(reader.getnframes() / 16000 - seconds) < 0.1


@pytest.mark.usefixtures("use_tool")
def test_wav_without_extension(tmp_path: Path) -> None:
    src = tmp_path / "0123456789abcdef0123456789abcdef"  # like an upload copy
    _stereo_wav(src)
    assert abs(audio.probe_duration(src) - 2.0) < 0.05
    out = tmp_path / "out.wav"
    audio.normalize_to_wav(src, out)
    _check_normalized(out, 2.0)


@pytest.mark.usefixtures("use_tool")
def test_garbage_and_missing_files(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"\x00" * 4000)
    for path in (junk, tmp_path / "missing.mp3"):
        with pytest.raises(AudioError) as exc:
            audio.probe_duration(path)
        assert exc.value.code == "probe_failed"
    out = tmp_path / "out.wav"
    with pytest.raises(AudioError) as exc:
        audio.normalize_to_wav(junk, out)
    assert exc.value.code == "probe_failed"
    assert not out.exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make the samples")
@pytest.mark.usefixtures("use_tool")
def test_video_mp4_and_mp3_without_extension(tmp_path: Path) -> None:
    mp4 = tmp_path / "video"
    mp3 = tmp_path / "voice"
    silent = tmp_path / "silent-video"
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    picture = ["-f", "lavfi", "-i", "color=c=gray:s=160x120:r=5:d=3"]
    subprocess.run(
        [*base, *picture, *tone, "-c:v", "libx264", "-c:a", "aac", "-f", "mp4", str(mp4)],
        check=True,
    )
    subprocess.run([*base, *tone, "-f", "mp3", str(mp3)], check=True)
    subprocess.run([*base, *picture, "-c:v", "libx264", "-f", "mp4", str(silent)], check=True)

    for src in (mp4, mp3):
        assert abs(audio.probe_duration(src) - 3.0) < 0.2
        out = tmp_path / f"{src.name}.wav"
        audio.normalize_to_wav(src, out)
        _check_normalized(out, 3.0)
    with pytest.raises(AudioError) as exc:
        audio.probe_duration(silent)
    assert exc.value.code == "no_audio_stream"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make the samples")
@pytest.mark.usefixtures("use_tool")
def test_mp3_without_id3_after_padding_and_random_bytes(tmp_path: Path) -> None:
    raw = tmp_path / "raw.mp3"
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
            "-i", "sine=frequency=440:duration=2", "-id3v2_version", "0",
            "-write_xing", "0", str(raw),
        ],
        check=True,
    )  # fmt: skip
    padded = tmp_path / "padded"
    padded.write_bytes(b"\x00" * 300 + raw.read_bytes())
    assert abs(audio.probe_duration(padded) - 2.0) < 0.2
    for i in range(5):
        noise = tmp_path / f"noise{i}"
        noise.write_bytes(os.urandom(20_000))
        with pytest.raises(AudioError):
            audio.probe_duration(noise)
