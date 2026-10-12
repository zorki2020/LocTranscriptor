"""Network ban for models: meeting content never leaves this computer.

The network is needed only once, to download models (`transcribator models download`).
All other commands set these variables before ML libraries are imported.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping

# An address where nothing listens: the connection is refused immediately.
_BLACKHOLE = "http://127.0.0.1:9"

OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "DO_NOT_TRACK": "1",
    # The FluidAudio CLI has no offline flag and re-downloads models when loading fails.
    "REGISTRY_URL": _BLACKHOLE,
    "MODEL_REGISTRY_URL": _BLACKHOLE,
}

# For model downloads: telemetry is off then too.
DOWNLOAD_ENV = {"HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}


def enforce_offline(env: MutableMapping[str, str] | None = None) -> None:
    target = os.environ if env is None else env
    target.update(OFFLINE_ENV)


def allow_download(env: MutableMapping[str, str] | None = None) -> None:
    target = os.environ if env is None else env
    for key in OFFLINE_ENV:
        target.pop(key, None)
    target.update(DOWNLOAD_ENV)
