"""Playback of a meeting's audio in the meeting page: which file to play and how to serve
a part of it.

The player reads either the audio kept for an in-app recording or the original recording
at the path it was added from, if that file is still there. Nothing is copied and the
original is opened read-only. The file is always chosen from what is stored for the
meeting, never from the request, and its type comes from its first bytes, not its name.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterator
from typing import Protocol

_CHUNK = 256 * 1024
_HEAD_BYTES = 16
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
# Longer than any real position in a file; keeps int() away from absurd digit strings.
_MAX_DIGITS = 18


class _Descriptor(Protocol):
    fd: int | None

    def close(self) -> None: ...


def open_original(path: str) -> tuple[int, int] | None:
    """Open the original recording read-only. Returns `(descriptor, size)` of a regular,
    non-empty file (the caller closes the descriptor), or None when it is gone, moved,
    unreadable or not a file."""
    if not path or "\0" in path or not os.path.isabs(path):
        return None
    # O_NONBLOCK: a named pipe at that path must not hang the request.
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size < 1:
        os.close(fd)
        return None
    return fd, info.st_size


def sniff(fd: int) -> str | None:
    """The media type of an open file by its first bytes, or None for a container the
    player is not offered."""
    try:
        head = os.pread(fd, _HEAD_BYTES, 0)
    except OSError:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if head[:4] == b"fLaC":
        return "audio/flac"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "audio/aiff"
    if head[:4] == b"caff":
        return "audio/x-caf"
    return None


def parse_range(header: str | None, size: int) -> tuple[int, int]:
    """`(start, end)` inclusive for a single `Range: bytes=...` header. No header means
    the whole file. ValueError for a range that cannot be served (the caller answers
    416). Several ranges at once are not served."""
    if header is None:
        return 0, size - 1
    match = _RANGE.fullmatch(header.strip())
    if match is None:
        raise ValueError("range")
    first, last = match.group(1), match.group(2)
    if len(first) > _MAX_DIGITS or len(last) > _MAX_DIGITS or (not first and not last):
        raise ValueError("range")
    if not first:
        # The last N bytes.
        length = int(last)
        if length < 1:
            raise ValueError("range")
        return max(0, size - length), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("range")
    return start, end


def read_range(closer: _Descriptor, start: int, end: int) -> Iterator[bytes]:
    """Bytes `start..end` of the descriptor, in chunks. Reads by position, so nothing
    depends on where the descriptor stands. The descriptor is closed at the end, on an
    error and when the client leaves."""
    try:
        position = start
        while position <= end and closer.fd is not None:
            chunk = os.pread(closer.fd, min(_CHUNK, end - position + 1), position)
            if not chunk:
                break
            position += len(chunk)
            yield chunk
    finally:
        closer.close()
