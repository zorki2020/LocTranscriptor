"""Job temporary folders and cleanup of leftovers after a crash.

The folder name contains the process PID: `transcribator-<pid>-...`. If the process is
killed without `finally` (SIGKILL, out of memory), the folder keeps the WAV of the whole
meeting. At startup `cleanup_stale` deletes such folders, but only once their process
is gone: folders of live processes (e.g. a parent benchmark) are left alone.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

PREFIX = "transcribator-"
_OWNED = re.compile("^" + re.escape(PREFIX) + r"(?:[a-z]+-)?(\d{1,9})-")


def job_prefix(kind: str = "") -> str:
    """Temporary folder prefix of the current process; `kind` is for service folders."""
    return f"{PREFIX}{kind + '-' if kind else ''}{os.getpid()}-"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the process exists but belongs to someone else
    return True


def cleanup_stale(tmp_root: Path | None = None) -> int:
    """Delete job folders of dead processes. Return how many were deleted."""
    root = tmp_root or Path(tempfile.gettempdir())
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        match = _OWNED.match(entry.name)
        if not match or not entry.is_dir() or entry.is_symlink():
            continue
        if entry.lstat().st_uid != os.getuid():
            continue  # a foreign folder (e.g. in the shared /tmp): not our business
        pid = int(match[1])
        if pid == os.getpid() or _alive(pid):
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
    return removed
