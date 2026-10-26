"""Recordings made inside the app: validation of the request that queues a finished file.

The app writes the final WAV into the `uploads` folder itself and then asks the service to
queue it (`POST /api/recordings`). Nothing is copied: the meeting points at the file, and
the worker deletes it after success like any other upload.

Everything here is strict and quiet: the request is untrusted input, and neither the file
name, the title nor a path ever goes into an error body or a log. Errors are codes only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from transcribator.i18n import RECORDING_LANGS, pick
from transcribator.storage import repo

# `rec` + 24 random hex characters + `.wav`, written by the app (never by this service).
REC_REF = re.compile(r"rec[0-9a-f]{24}\.wav")
# The kept audio of a recording: the same 24 hex characters as the WAV, an m4a container.
AUDIO_REF = re.compile(r"rec[0-9a-f]{24}\.m4a")
# Any name the service puts in `uploads` for a meeting: a browser upload or an app recording.
UPLOAD_REF = re.compile(r"[0-9a-f]{32}|rec[0-9a-f]{24}\.wav")

MAX_BODY_BYTES = 4096
# A WAV header alone is 44 bytes: anything this small holds no audio.
MIN_FILE_BYTES = 45
TITLE_LIMIT = 300
_TIMESTAMP_LIMIT = 40

_TITLES = {
    "default": {"en": "Recording {when}", "ru": "Запись {when}"},
}


class RecordingError(Exception):
    """A rejected request. `code` goes to the client, nothing else does."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class RecordingRequest:
    ref: str
    title: str | None
    started_at: datetime | None
    language: str
    layout: str
    audio_ref: str | None = None


def _reject_constant(_: str) -> Any:
    raise ValueError("NaN and Infinity are not valid JSON")


def parse_body(raw: bytes) -> RecordingRequest:
    """Check the JSON body. Codes: bad_body, bad_ref, bad_audio_ref, bad_language,
    bad_layout. The
    client also sends `duration_s`: it is accepted and ignored (the worker measures it)."""
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RecordingError("bad_body") from None
    if not isinstance(body, dict):
        raise RecordingError("bad_body")

    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise RecordingError("bad_body")
    started_at = _parse_started_at(body.get("started_at"))
    ref = body.get("ref")
    if not isinstance(ref, str) or REC_REF.fullmatch(ref) is None:
        raise RecordingError("bad_ref")
    audio_ref = _parse_audio_ref(body.get("audio_ref"), ref)
    language = body.get("language")
    if not isinstance(language, str) or language not in RECORDING_LANGS:
        raise RecordingError("bad_language")
    layout = body.get("layout")
    if not isinstance(layout, str) or layout not in repo.LAYOUTS:
        raise RecordingError("bad_layout")
    return RecordingRequest(ref, title, started_at, language, layout, audio_ref)


def _parse_audio_ref(value: object, ref: str) -> str | None:
    """The optional name of the kept audio. It must belong to this very recording (same
    hex characters), so two meetings can never share a file that one of them deletes."""
    if value is None:
        return None
    if not isinstance(value, str) or AUDIO_REF.fullmatch(value) is None or value[:27] != ref[:27]:
        raise RecordingError("bad_audio_ref")
    return value


def _parse_started_at(value: object) -> datetime | None:
    """An ISO 8601 timestamp with a UTC offset (`2026-10-23T10:00:00Z`), or None."""
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _TIMESTAMP_LIMIT:
        raise RecordingError("bad_body")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("no offset")
        return stamp.astimezone(UTC)
    except (ValueError, OverflowError):
        raise RecordingError("bad_body") from None


def _open_regular(folder: Path, name: str) -> tuple[int, int] | None:
    """Open `folder/name` read-only without following links and inspect it through the
    descriptor, so the answer cannot change between the check and the use. Returns
    `(descriptor, size)` of a regular file (the caller closes the descriptor), or None for
    a link, a folder, a pipe or anything unreadable. FileNotFoundError is passed on."""
    # O_NONBLOCK: opening a named pipe must not hang the request.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(folder / name, flags)
    except FileNotFoundError:
        raise
    except OSError:  # ELOOP: a symbolic link; anything else unreadable is not our file
        return None
    try:
        info = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return fd, info.st_size


def check_file(folder: Path, ref: str) -> None:
    """The file must be a regular file directly in `folder`, with audio in it. Codes:
    bad_ref (a link, a folder or anything but a regular file), file_missing (absent or
    too small)."""
    if REC_REF.fullmatch(ref) is None:
        raise RecordingError("bad_ref")
    try:
        opened = _open_regular(folder, ref)
    except FileNotFoundError:
        raise RecordingError("file_missing", 404) from None
    if opened is None:
        raise RecordingError("bad_ref")
    fd, size = opened
    os.close(fd)
    if size < MIN_FILE_BYTES:
        raise RecordingError("file_missing", 404)


def open_audio(folder: Path, audio_ref: str) -> tuple[int, int]:
    """Open the kept audio: a regular file directly in `folder`, not empty. Returns
    `(descriptor, size)`; the caller closes the descriptor. Codes: bad_audio_ref (a bad
    name, a link, a folder or anything but a regular file), audio_missing (absent or
    empty)."""
    if AUDIO_REF.fullmatch(audio_ref) is None:
        raise RecordingError("bad_audio_ref")
    try:
        opened = _open_regular(folder, audio_ref)
    except FileNotFoundError:
        raise RecordingError("audio_missing") from None
    if opened is None:
        raise RecordingError("bad_audio_ref")
    if opened[1] < 1:
        os.close(opened[0])
        raise RecordingError("audio_missing")
    return opened


def check_audio(folder: Path, audio_ref: str) -> None:
    """`open_audio` for a caller that only needs the verdict."""
    os.close(open_audio(folder, audio_ref)[0])


def open_meeting_audio(folder: Path, audio_ref: str | None) -> tuple[int, int] | None:
    """The kept audio of a meeting as `(descriptor, size)`, found only by the stored
    reference after checking its shape; None when there is no usable file (no audio, a bad
    or poisoned reference, a link, a folder, an empty or missing file)."""
    if audio_ref is None:
        return None
    try:
        return open_audio(folder, audio_ref)
    except RecordingError:
        return None


def has_audio(folder: Path, audio_ref: str | None) -> bool:
    opened = open_meeting_audio(folder, audio_ref)
    if opened is None:
        return False
    os.close(opened[0])
    return True


def remove_audio(folder: Path, audio_ref: str | None) -> None:
    """Delete a meeting's kept audio. Only a name of the strict shape is ever joined to
    `folder`, so a poisoned reference cannot reach anything outside it. A missing file
    is fine; a link is removed itself, never what it points to."""
    if audio_ref is None or AUDIO_REF.fullmatch(audio_ref) is None:
        return
    # A folder or a file we may not remove stays: the meeting is gone anyway.
    with contextlib.suppress(OSError):
        (folder / audio_ref).unlink(missing_ok=True)


def clean_title(raw: str | None) -> str:
    """The title as the upload endpoint keeps titles: whitespace collapsed, length limited.
    Control and format characters are dropped (they could disguise the title)."""
    visible = "".join(ch for ch in (raw or "") if unicodedata.category(ch) not in ("Cc", "Cf"))
    return " ".join(visible.split())[:TITLE_LIMIT]


def default_title(when: datetime | None) -> str:
    """ "Recording 23.10.2026 14:05" in the interface language, in the Mac's local time."""
    local = (when or datetime.now(UTC)).astimezone()
    return pick(_TITLES, "default", when=local.strftime("%d.%m.%Y %H:%M"))


def recorded_at(started_at: datetime | None) -> str | None:
    """`recorded_at` in the format the upload endpoint stores."""
    return None if started_at is None else started_at.isoformat(timespec="seconds")
