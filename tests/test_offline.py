from __future__ import annotations

from transcribator.offline import OFFLINE_ENV, allow_download, enforce_offline


def test_enforce_offline_overrides_existing_values() -> None:
    env = {"HF_HUB_OFFLINE": "0", "PATH": "/bin"}
    enforce_offline(env)
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
    assert env["REGISTRY_URL"].startswith("http://127.0.0.1:")
    assert env["PATH"] == "/bin"


def test_allow_download_keeps_telemetry_off() -> None:
    env = dict(OFFLINE_ENV)
    allow_download(env)
    assert "HF_HUB_OFFLINE" not in env
    assert "REGISTRY_URL" not in env
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"
