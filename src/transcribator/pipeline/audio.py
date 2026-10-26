"""Audio handling via ffmpeg and ffprobe, or via the app's AVFoundation helper.

Two tools with the same contract (duration with an audio track check, conversion to mono
16 kHz 16-bit WAV, splitting a two-channel recording into two such files, the same error
codes):
- ffmpeg and ffprobe from PATH (installation from source);
- `transcribator-audio` (Swift, AVFoundation, shipped inside LocTranscriptor.app) when
  `TRANSCRIBATOR_AUDIO_TOOL` points to it. It identifies the container by the file's
  first bytes and refuses references to other files or URLs. It runs under `sandbox-exec`
  without network, except inside the isolated recognition process, which is already in
  such a sandbox (sandboxes can't be nested; `TRANSCRIBATOR_SANDBOXED` tells them apart).

All subprocess calls live here. Errors carry only a code: the file name and ffmpeg
output may contain the meeting title, so they are not passed on, and the original
exception is not chained (`from None`), otherwise it would end up in the traceback
(meeting content and file names never go to logs or errors).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import subprocess
import sys
import wave
from array import array
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_S = 60
CONVERT_TIMEOUT_S = 3600

# Read local files only: an uploaded "mp3" must not turn out to be a playlist
# that pulls in other files or URLs.
_LOCAL_ONLY = ["-protocol_whitelist", "file"]

AUDIO_TOOL_ENV = "TRANSCRIBATOR_AUDIO_TOOL"
SANDBOXED_ENV = "TRANSCRIBATOR_SANDBOXED"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
NO_NETWORK_PROFILE = "(version 1)(allow default)(deny network*)"


class AudioError(Exception):
    """An audio processing error. Code only, no file names or ffmpeg output."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _audio_tool() -> str | None:
    return os.environ.get(AUDIO_TOOL_ENV) or None


def _sbpl(value: str) -> str:
    """A string literal for a sandbox profile."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def helper_profile(tool: str, writable: Path | None = None) -> str:
    """Sandbox for the helper that parses untrusted files: no network, no other programs,
    no download daemon, writes only to the system temp/cache area (CoreMedia needs it) and
    to the output's directory. Paths are resolved: the sandbox matches real paths."""
    real_tool = os.path.realpath(tool)
    targets = ['(literal "/dev/null")', '(subpath "/private/var/folders")']
    if writable is not None:
        targets.append(f"(subpath {_sbpl(os.path.realpath(writable))})")
    return (
        f"{NO_NETWORK_PROFILE}"
        f"(deny process-exec*)(allow process-exec (literal {_sbpl(real_tool)}))"
        '(deny mach-lookup (global-name "com.apple.nsurlsessiond"))'
        f"(deny file-write*)(allow file-write* {' '.join(targets)})"
    )


def _tool_command(tool: str, *args: str, writable: Path | None = None) -> list[str]:
    cmd = [tool, *args]
    if sys.platform == "darwin" and os.environ.get(SANDBOXED_ENV) != "1":
        cmd = [SANDBOX_EXEC, "-p", helper_profile(tool, writable), *cmd]
    return cmd


def _run_tool(
    tool: str, args: list[str], timeout: int, fallback: str, writable: Path | None = None
) -> dict[str, Any]:
    """Run the AVFoundation helper. Its stdout is one JSON line without names or paths."""
    if not os.path.isfile(tool):
        # Checked here: under sandbox-exec a missing program is just "exit 71".
        raise AudioError("audio_tool_missing")
    try:
        proc = _run(_tool_command(tool, *args, writable=writable), timeout)
    except AudioError as exc:
        raise AudioError(
            "audio_tool_missing" if exc.code == "ffmpeg_missing" else exc.code
        ) from None
    try:
        data = json.loads(proc.stdout or b"{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if proc.returncode != 0:
        code = data.get("error")
        known = {
            "probe_failed",
            "no_audio_stream",
            "empty_audio",
            "normalize_failed",
            "split_failed",
            "not_two_channels",
        }
        raise AudioError(code if code in known else fallback)
    return data


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
    tool = _audio_tool()
    if tool is not None:
        data = _run_tool(tool, ["probe", str(path.resolve())], PROBE_TIMEOUT_S, "probe_failed")
        try:
            duration = float(data["duration"])
        except (KeyError, TypeError, ValueError):
            raise AudioError("probe_bad_output") from None
        if not duration > 0:
            raise AudioError("empty_audio")
        return duration
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
    tool = _audio_tool()
    if tool is not None:
        args = ["convert", str(src.resolve()), str(dst)]
        _run_tool(tool, args, CONVERT_TIMEOUT_S, "normalize_failed", writable=dst.parent)
        return
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


def split_channels(src: Path, out_ch0: Path, out_ch1: Path) -> None:
    """A two-channel recording -> two mono 16 kHz 16-bit WAV files, one per channel, on the
    same timeline as the source. Anything but exactly two channels is refused with
    `not_two_channels`. Both outputs must be in one directory (the helper's sandbox may
    write only there). A partial output never stays behind on failure."""
    try:
        if out_ch0.parent != out_ch1.parent or out_ch0 == out_ch1:
            raise AudioError("split_failed")
        tool = _audio_tool()
        if tool is not None:
            args = ["split", str(src.resolve()), str(out_ch0), str(out_ch1)]
            _run_tool(tool, args, CONVERT_TIMEOUT_S, "split_failed", writable=out_ch0.parent)
            return
        _split_ffmpeg(src, out_ch0, out_ch1)
    except BaseException:
        for path in (out_ch0, out_ch1):
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def _channel_count(src: Path) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        *_LOCAL_ONLY,
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=channels",
        "-of",
        "json",
        str(src.resolve()),
    ]
    proc = _run(cmd, PROBE_TIMEOUT_S)
    if proc.returncode != 0:
        raise AudioError("probe_failed")
    try:
        streams = json.loads(proc.stdout).get("streams", [])
    except (ValueError, AttributeError):
        raise AudioError("probe_bad_output") from None
    if not streams:
        raise AudioError("no_audio_stream")
    try:
        return int(streams[0]["channels"])
    except (KeyError, TypeError, ValueError):
        raise AudioError("probe_bad_output") from None


def _split_ffmpeg(src: Path, out_ch0: Path, out_ch1: Path) -> None:
    if _channel_count(src) != 2:
        raise AudioError("not_two_channels")
    graph = "[0:a:0]asplit=2[a][b];[a]pan=mono|c0=c0[c0];[b]pan=mono|c0=c1[c1]"
    pcm = ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-v",
        "error",
        *_LOCAL_ONLY,
        "-i",
        str(src.resolve()),
        "-filter_complex",
        graph,
        "-map",
        "[c0]",
        *pcm,
        str(out_ch0),
        "-map",
        "[c1]",
        *pcm,
        str(out_ch1),
    ]
    if _run(cmd, CONVERT_TIMEOUT_S).returncode != 0:
        raise AudioError("split_failed")


def count_loud_blocks(path: Path, block_s: float, rms_threshold: float) -> tuple[int, int]:
    """(blocks whose RMS exceeds `rms_threshold`, all blocks) of a mono 16-bit PCM WAV cut
    into blocks of `block_s` seconds (the last one may be shorter). The file is streamed
    block by block, without subprocesses. `rms_threshold` is in 16-bit sample units."""
    loud = total = 0
    limit = rms_threshold * rms_threshold
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getsampwidth() != 2 or reader.getnchannels() != 1:
                raise AudioError("level_failed")
            block = max(int(reader.getframerate() * block_s), 1)
            while frames := reader.readframes(block):
                samples = array("h")
                samples.frombytes(frames[: len(frames) - len(frames) % 2])
                if not samples:
                    break
                if sys.byteorder != "little":
                    samples.byteswap()
                total += 1
                if math.sumprod(samples, samples) / len(samples) > limit:
                    loud += 1
    except (wave.Error, OSError, EOFError):
        raise AudioError("level_failed") from None
    return loud, total


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
