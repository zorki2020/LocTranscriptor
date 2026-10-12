"""The service data directory.

`~/Library/Application Support/transcribator/` with mode 700; the path can be changed with
`TRANSCRIBATOR_DATA_DIR` (tests and synthetic checks). Inside:
- `transcribator.sqlite3`: the database (600);
- `uploads/`: temporary copies of recordings dropped into the browser, kept until
  processing finishes;
- `work/`: temporary job files with the recognition result.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_ENV = "TRANSCRIBATOR_DATA_DIR"
_DEFAULT = Path.home() / "Library/Application Support/transcribator"
# What the service itself puts into the data directory: such a directory is ours, even
# without a database.
_OWN_ENTRIES = frozenset(
    {
        "transcribator.sqlite3",
        "transcribator.sqlite3-wal",
        "transcribator.sqlite3-shm",
        "uploads",
        "work",
        "worker.lock",
        "serve.lock",
    }
)


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


class DataDirError(Exception):
    """The given data directory does not look like a service data directory."""


def data_dir() -> Path:
    """The data directory. A foreign non-empty directory (e.g. `~` set by mistake in
    `TRANSCRIBATOR_DATA_DIR`) is left alone: otherwise it would get mode 700."""
    root = Path(os.environ.get(DATA_ENV) or _DEFAULT)
    foreign = root.is_dir() and any(e.name not in _OWN_ENTRIES for e in root.iterdir())
    if foreign and not db_path(root).exists():
        raise DataDirError("data dir is not empty and has no database")
    return ensure_private_dir(root)


def db_path(root: Path) -> Path:
    return root / "transcribator.sqlite3"


def uploads_dir(root: Path) -> Path:
    return ensure_private_dir(root / "uploads")


def work_dir(root: Path) -> Path:
    return ensure_private_dir(root / "work")
