"""Audio handling via ffmpeg and ffprobe.

All subprocess calls live here. Errors carry only a code: the file name and ffmpeg
output may contain the meeting title, so they are not passed on, and the original
exception is not chained (`from None`), otherwise it would end up in the traceback
(meeting content and file names never go to logs or errors).
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

PROBE_TIMEOUT_S = 60
CONVERT_TIMEOUT_S = 3600

# Read local files only: an uploaded "mp3" must not turn out to be a playlist
# that pulls in other files or URLs.
_LOCAL_ONLY = ["-protocol_whitelist", "file"]


class AudioError(Exception):
    """An audio processing error. Code only, no file names or ffmpeg output."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise AudioError("ffmpeg_missing") from None
    except subprocess.TimeoutExpired:
        raise AudioError("timeout") from None


def probe_duration(path: Path) -> float:
    """Duration in seconds. Checks that the file has an audio track."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        *_LOCAL_ONLY,
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(path.resolve()),
    ]
    proc = _run(cmd, PROBE_TIMEOUT_S)
    if proc.returncode != 0:
        raise AudioError("probe_failed")
    try:
        info = json.loads(proc.stdout)
        streams = info.get("streams", [])
        duration = float(info["format"]["duration"])
    except (ValueError, KeyError, TypeError):
        raise AudioError("probe_bad_output") from None
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise AudioError("no_audio_stream")
    if duration <= 0:
        raise AudioError("empty_audio")
    return duration


def normalize_to_wav(src: Path, dst: Path) -> None:
    """Mono, 16 kHz, 16-bit WAV: the common input for diarization and recognition."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        *_LOCAL_ONLY,
        "-i",
        str(src.resolve()),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(dst),
    ]
    if _run(cmd, CONVERT_TIMEOUT_S).returncode != 0:
        raise AudioError("normalize_failed")


def cut_wav(src: Path, dst: Path, start_s: float, duration_s: float) -> None:
    """Cut a WAV chunk exactly by samples, without subprocesses."""
    try:
        with wave.open(str(src), "rb") as reader:
            rate = reader.getframerate()
            first = min(max(int(start_s * rate), 0), reader.getnframes())
            reader.setpos(first)
            frames = reader.readframes(max(int(duration_s * rate), 0))
            with wave.open(str(dst), "wb") as writer:
                writer.setnchannels(reader.getnchannels())
                writer.setsampwidth(reader.getsampwidth())
                writer.setframerate(rate)
                writer.writeframes(frames)
    except (wave.Error, OSError, EOFError):
        raise AudioError("cut_failed") from None
