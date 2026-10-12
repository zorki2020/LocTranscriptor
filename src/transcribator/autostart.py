"""Start at macOS login (LaunchAgent) and the menu bar icon.

Two per-user agents (not system-wide, no password needed):
- `local.transcribator`: `transcribator serve`. Starts at login; launchd brings it back
  if it crashes (`KeepAlive` on a non-zero exit code). A stop from the menu bar icon or
  `launchctl bootout` is not restarted until "Start" or the next login.
- `local.transcribator.menubar`: the Transcribator menu bar icon (`macos/MenuBar`).

Service output goes to `~/Library/Logs/transcribator/serve.log` (600): it holds only the
address and error codes, no file names or meeting text. `launchctl` is called by its
absolute path with an argument list.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from transcribator.i18n import RECORDING_LANG_ENV, UI_LANG_ENV
from transcribator.paths import DATA_ENV

LABEL = "local.transcribator"
MENUBAR_LABEL = "local.transcribator.menubar"
LAUNCHCTL = "/bin/launchctl"
STATUS_URL = "http://127.0.0.1:8765/api/status"
# launchd starts agents with a minimal PATH, while ffmpeg and ffprobe live in Homebrew.
AGENT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# The service log is truncated at startup under launchd if it grew larger than this.
LOG_LIMIT_BYTES = 1024 * 1024
# bootstrap right after bootout sometimes answers "I/O error": retry once.
RETRY_DELAY_S = 2.0

Runner = Callable[[Sequence[str]], int]


@dataclass(frozen=True, slots=True)
class Paths:
    repo: Path
    executable: Path
    agents_dir: Path
    log_dir: Path
    menubar_app: Path

    @property
    def serve_plist(self) -> Path:
        return self.agents_dir / f"{LABEL}.plist"

    @property
    def menubar_plist(self) -> Path:
        return self.agents_dir / f"{MENUBAR_LABEL}.plist"

    @property
    def menubar_binary(self) -> Path:
        return self.menubar_app / "Contents/MacOS/Transcribator"


def default_paths() -> Paths:
    home = Path.home()
    repo = Path(__file__).resolve().parents[2]
    return Paths(
        repo=repo,
        executable=repo / ".venv/bin/transcribator",
        agents_dir=home / "Library/LaunchAgents",
        log_dir=home / "Library/Logs/transcribator",
        menubar_app=home / "Applications/Transcribator.app",
    )


def serve_agent(paths: Paths) -> dict[str, Any]:
    log = str(paths.log_dir / "serve.log")
    env = {"PATH": AGENT_PATH, "LANG": "en_US.UTF-8", "PYTHONUTF8": "1"}
    # Same data dir and languages as when started by hand.
    for key in (DATA_ENV, UI_LANG_ENV, RECORDING_LANG_ENV):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return {
        "Label": LABEL,
        "ProgramArguments": [str(paths.executable), "serve"],
        "WorkingDirectory": str(paths.repo),
        "RunAtLoad": True,
        # Restart only after a crash: a normal exit (code 0) is not restarted.
        "KeepAlive": {"SuccessfulExit": False},
        # At most once every 30 s if the service dies right away (e.g. the port is taken).
        "ThrottleInterval": 30,
        # The worker needs up to a minute to stop recognition and clean up its files.
        "ExitTimeOut": 90,
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def menubar_agent(paths: Paths) -> dict[str, Any]:
    return {
        "Label": MENUBAR_LABEL,
        "ProgramArguments": [str(paths.menubar_binary)],
        "RunAtLoad": True,
        "KeepAlive": False,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }


def _write_plist(path: Path, data: dict[str, Any]) -> bool:
    """Write a plist. Return False if the content did not change."""
    if path.exists():
        try:
            with path.open("rb") as fh:
                if plistlib.load(fh) == data:
                    return False
        except (OSError, plistlib.InvalidFileException):
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        plistlib.dump(data, fh)
    tmp.chmod(0o644)  # launchd rejects a plist that others can write to
    tmp.replace(path)
    return True


def _private_log(paths: Paths) -> None:
    paths.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.log_dir.chmod(0o700)
    log = paths.log_dir / "serve.log"
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)


def launchctl(args: Sequence[str]) -> int:
    try:
        proc = subprocess.run(
            [LAUNCHCTL, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,  # bootout waits for the worker to stop (ExitTimeOut 90 s)
        )
    except subprocess.TimeoutExpired:
        return 124
    return proc.returncode


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _install_agent(run: Runner, label: str, plist: Path, data: dict[str, Any]) -> int | None:
    """Install one agent. None means it is already installed and unchanged, so no restart
    is needed (a running job is not interrupted). The "Start at login" toggle
    (`enable`/`disable`) is touched only on first install: a reinstall must not reset it."""
    first = not plist.exists()
    changed = _write_plist(plist, data)
    target = f"{_domain()}/{label}"
    loaded = run(["print", target]) == 0
    if first:
        run(["enable", target])
    if not changed and loaded:
        return None
    if loaded:
        run(["bootout", target])
    code = run(["bootstrap", _domain(), str(plist)])
    if code != 0:
        time.sleep(RETRY_DELAY_S)
        code = run(["bootstrap", _domain(), str(plist)])
    return code


def _line(name: str, code: int | None, ok: str) -> str:
    if code is None:
        return f"{name}: already installed"
    return f"{name}: {ok}" if code == 0 else f"{name}: launchctl code {code}"


def install(paths: Paths | None = None, *, run: Runner = launchctl) -> list[str]:
    """Install and immediately start the agents. Return report lines (no data paths)."""
    p = paths or default_paths()
    if not p.executable.is_file():
        raise FileNotFoundError("transcribator executable")
    _private_log(p)
    code = _install_agent(run, LABEL, p.serve_plist, serve_agent(p))
    report = [_line("Service", code, "start at login enabled")]
    if p.menubar_binary.is_file():
        code = _install_agent(run, MENUBAR_LABEL, p.menubar_plist, menubar_agent(p))
        report.append(_line("Menu bar icon", code, "enabled"))
    else:
        report.append(
            "Menu bar icon not built: run scripts/build_menubar.sh, then autostart install again"
        )
    return report


def under_launchd() -> bool:
    """Whether this process was started by the `local.transcribator` agent (launchd sets
    the variable)."""
    return os.environ.get("XPC_SERVICE_NAME") == LABEL


def trim_log(paths: Paths | None = None) -> None:
    """The service log has no rotation and keeps growing: truncate a large one at startup
    under launchd."""
    log = (paths or default_paths()).log_dir / "serve.log"
    try:
        if log.stat().st_size > LOG_LIMIT_BYTES:
            os.truncate(log, 0)
    except OSError:
        pass


def remove(paths: Paths | None = None, *, run: Runner = launchctl) -> list[str]:
    p = paths or default_paths()
    for label, plist in ((MENUBAR_LABEL, p.menubar_plist), (LABEL, p.serve_plist)):
        run(["bootout", f"{_domain()}/{label}"])
        plist.unlink(missing_ok=True)
    return ["Start at login disabled, service and menu bar icon stopped"]


def service_status(url: str = STATUS_URL) -> dict[str, int] | None:
    """Queue counters from the running service, or None if it does not respond."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            data = json.loads(response.read())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): int(v) for k, v in data.items() if isinstance(v, int)}


def status(paths: Paths | None = None, *, run: Runner = launchctl) -> list[str]:
    p = paths or default_paths()
    loaded = run(["print", f"{_domain()}/{LABEL}"]) == 0
    counts = service_status()
    lines = [
        f"Start at login: {'installed' if p.serve_plist.exists() else 'not installed'}",
        f"Service in launchd: {'loaded' if loaded else 'not loaded'}",
    ]
    if counts is None:
        lines.append("Service is not responding on 127.0.0.1:8765")
    else:
        lines.append(
            "Running: {queued} queued, {processing} processing, {done} done, {error} failed".format(
                **{k: counts.get(k, 0) for k in _COUNTS}
            )
        )
    return lines


_COUNTS = ("queued", "processing", "done", "error")
