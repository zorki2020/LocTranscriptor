"""macOS system file dialog (the recording is read in place, without a copy).

The browser doesn't give the page a file path, so the "Choose file" button asks the
server to show a Finder dialog through `osascript`. The output (a path) is not logged:
the file name counts as content.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from transcribator.web.strings import t

PICK_TIMEOUT_S = 600


def _script() -> str:
    # The prompt comes from our own table and has no quotes or backslashes.
    prompt = t("pick_prompt").replace('"', "").replace("\\", "")
    return (
        f'POSIX path of (choose file with prompt "{prompt}"'
        ' of type {"public.audio", "public.movie"})'
    )


def pick_file() -> Path | None:
    """Path to the chosen file, or None if the user closed the dialog."""
    try:
        proc = subprocess.run(  # noqa: S603 - argument list, no shell
            ["/usr/bin/osascript", "-e", _script()],
            capture_output=True,
            check=False,
            timeout=PICK_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        raw = proc.stdout.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError:
        return None
    return Path(raw) if raw.startswith("/") else None
