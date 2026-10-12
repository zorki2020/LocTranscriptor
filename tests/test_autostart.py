from __future__ import annotations

import json
import os
import plistlib
import stat
import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from transcribator import autostart
from transcribator.autostart import LABEL, MENUBAR_LABEL, Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    exe = tmp_path / "repo/.venv/bin/transcribator"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    return Paths(
        repo=tmp_path / "repo",
        executable=exe,
        agents_dir=tmp_path / "LaunchAgents",
        log_dir=tmp_path / "Logs/transcribator",
        menubar_app=tmp_path / "Applications/Transcribator.app",
    )


class FakeLaunchctl:
    def __init__(self, codes: dict[str, int] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._codes = codes or {}

    def __call__(self, args: Sequence[str]) -> int:
        self.calls.append(list(args))
        return self._codes.get(args[0], 0)


def test_serve_agent_restarts_on_crash_and_starts_at_login(paths: Paths) -> None:
    agent = autostart.serve_agent(paths)
    assert agent["Label"] == LABEL
    assert agent["ProgramArguments"] == [str(paths.executable), "serve"]
    assert agent["RunAtLoad"] is True
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    assert "/opt/homebrew/bin" in agent["EnvironmentVariables"]["PATH"]  # ffmpeg
    assert agent["StandardErrorPath"].endswith("serve.log")


def test_install_writes_plist_private_log_and_bootstraps(paths: Paths) -> None:
    run = FakeLaunchctl()

    report = autostart.install(paths, run=run)

    with paths.serve_plist.open("rb") as fh:
        assert plistlib.load(fh)["Label"] == LABEL
    assert stat.S_IMODE(paths.serve_plist.stat().st_mode) == 0o644
    assert stat.S_IMODE(paths.log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.log_dir / "serve.log").stat().st_mode) == 0o600
    domain = f"gui/{os.getuid()}"
    assert ["bootstrap", domain, str(paths.serve_plist)] in run.calls
    assert ["enable", f"{domain}/{LABEL}"] in run.calls
    assert report[0] == "Service: start at login enabled"
    assert "Menu bar icon not built" in report[1]
    assert not paths.menubar_plist.exists()


def test_install_adds_menubar_agent_when_app_is_built(paths: Paths) -> None:
    paths.menubar_binary.parent.mkdir(parents=True)
    paths.menubar_binary.write_bytes(b"")
    run = FakeLaunchctl()

    report = autostart.install(paths, run=run)

    with paths.menubar_plist.open("rb") as fh:
        agent = plistlib.load(fh)
    assert agent["Label"] == MENUBAR_LABEL
    assert agent["ProgramArguments"] == [str(paths.menubar_binary)]
    assert report[1] == "Menu bar icon: enabled"


def test_install_retries_bootstrap_once_then_reports_code(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(autostart, "RETRY_DELAY_S", 0)
    run = FakeLaunchctl({"bootstrap": 5})
    report = autostart.install(paths, run=run)
    assert [c[0] for c in run.calls].count("bootstrap") == 2
    assert report[0] == "Service: launchctl code 5"


def test_reinstall_without_changes_keeps_running_service(paths: Paths) -> None:
    autostart.install(paths, run=FakeLaunchctl())
    run = FakeLaunchctl()  # print -> 0: the agent is loaded
    report = autostart.install(paths, run=run)
    assert report[0] == "Service: already installed"
    assert not any(c[0] in ("bootout", "bootstrap", "enable") for c in run.calls)


def test_reinstall_does_not_reset_start_at_login(paths: Paths) -> None:
    autostart.install(paths, run=FakeLaunchctl())
    paths.serve_plist.write_bytes(b"")  # changed: the agent must be reloaded
    run = FakeLaunchctl()
    autostart.install(paths, run=run)
    assert not any(c[0] == "enable" for c in run.calls)  # the user's toggle is left alone
    assert any(c[0] == "bootstrap" for c in run.calls)


def test_agent_keeps_custom_data_dir_and_gives_worker_time_to_stop(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBATOR_DATA_DIR", "/Users/x/data")
    agent = autostart.serve_agent(paths)
    assert agent["EnvironmentVariables"]["TRANSCRIBATOR_DATA_DIR"] == "/Users/x/data"
    assert agent["ExitTimeOut"] >= 90 and agent["ThrottleInterval"] >= 30


def test_trim_log_truncates_only_large_log(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.log_dir.mkdir(parents=True)
    log = paths.log_dir / "serve.log"
    log.write_text("x" * 10, encoding="utf-8")
    autostart.trim_log(paths)
    assert log.stat().st_size == 10
    monkeypatch.setattr(autostart, "LOG_LIMIT_BYTES", 5)
    autostart.trim_log(paths)
    assert log.stat().st_size == 0


def test_install_requires_project_environment(paths: Paths) -> None:
    paths.executable.unlink()
    with pytest.raises(FileNotFoundError):
        autostart.install(paths, run=FakeLaunchctl())


def test_remove_unloads_and_deletes_both_agents(paths: Paths) -> None:
    run = FakeLaunchctl()
    autostart.install(paths, run=run)
    paths.menubar_plist.write_bytes(b"")

    autostart.remove(paths, run=run)

    assert not paths.serve_plist.exists() and not paths.menubar_plist.exists()
    domain = f"gui/{os.getuid()}"
    assert ["bootout", f"{domain}/{LABEL}"] in run.calls
    assert ["bootout", f"{domain}/{MENUBAR_LABEL}"] in run.calls


@pytest.fixture
def status_server() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"queued": 2, "processing": 1, "done": 5, "error": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/status"
    server.shutdown()


def test_service_status_reads_counts(status_server: str) -> None:
    assert autostart.service_status(status_server) == {
        "queued": 2,
        "processing": 1,
        "done": 5,
        "error": 0,
    }


def test_service_status_none_when_not_running() -> None:
    assert autostart.service_status("http://127.0.0.1:9/api/status") is None


def test_status_lines(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(autostart, "service_status", lambda url=None: None)
    lines = autostart.status(paths, run=FakeLaunchctl({"print": 113}))
    assert lines == [
        "Start at login: not installed",
        "Service in launchd: not loaded",
        "Service is not responding on 127.0.0.1:8765",
    ]


def test_status_lines_with_counts(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    counts = {"queued": 2, "processing": 1, "done": 5, "error": 0}
    monkeypatch.setattr(autostart, "service_status", lambda url=None: counts)
    lines = autostart.status(paths, run=FakeLaunchctl())
    assert lines[1] == "Service in launchd: loaded"
    assert lines[2] == "Running: 2 queued, 1 processing, 5 done, 0 failed"


def test_cli_install_without_project_env_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from transcribator.cli import main  # noqa: PLC0415

    def missing() -> list[str]:
        raise FileNotFoundError("transcribator executable")

    monkeypatch.setattr(autostart, "install", missing)
    assert main(["autostart", "install"]) == 2
    assert "uv sync" in capsys.readouterr().err


def test_agent_keeps_interface_and_recording_language(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRANSCRIBATOR_UI_LANG", "ru")
    monkeypatch.setenv("TRANSCRIBATOR_LANGUAGE", "ru")
    env = autostart.serve_agent(paths)["EnvironmentVariables"]
    assert env["TRANSCRIBATOR_UI_LANG"] == "ru" and env["TRANSCRIBATOR_LANGUAGE"] == "ru"
