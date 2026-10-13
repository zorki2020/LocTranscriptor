"""User settings of the app: interface language, default recording language, and
whether the first-run tips have been shown.

Stored in `settings.json` in the data directory (600). Only the app mode uses it; the
developer service keeps reading `TRANSCRIBATOR_UI_LANG` and `TRANSCRIBATOR_LANGUAGE`.
A missing or broken file means "no choice yet": the environment defaults apply. Fields
missing from an older file take their defaults.

The tips flag lives here rather than in the browser: the app window keeps no web storage
between launches.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from transcribator.i18n import RECORDING_LANGS, UI_LANGS

FILE_NAME = "settings.json"

# Requests run in a thread pool: load-modify-save must not interleave.
_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class Settings:
    ui_lang: str | None = None
    recording_lang: str | None = None
    tour_seen: bool = False


def settings_path(root: Path) -> Path:
    return root / FILE_NAME


def load(root: Path) -> Settings:
    try:
        data = json.loads(settings_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    if not isinstance(data, dict):
        return Settings()
    ui = data.get("ui_lang")
    rec = data.get("recording_lang")
    return Settings(
        ui_lang=ui if ui in UI_LANGS else None,
        recording_lang=rec if rec in RECORDING_LANGS else None,
        tour_seen=data.get("tour_seen") is True,
    )


def save(root: Path, settings: Settings) -> None:
    """Atomic write with mode 600 (mkstemp creates the file 600 with a unique name)."""
    data = {
        "ui_lang": settings.ui_lang,
        "recording_lang": settings.recording_lang,
        "tour_seen": settings.tour_seen,
    }
    fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, settings_path(root))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(root: Path, **changes: Any) -> Settings:
    """Load, change the given fields and save, serialized within the process."""
    with _LOCK:
        current = load(root)
        changed = replace(current, **changes)
        if changed != current:
            save(root, changed)
        return changed
