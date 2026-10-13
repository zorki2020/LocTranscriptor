"""Isolated processing of a recording in a child process (benchmark and worker).

Child wrappers (macOS only): `caffeinate -i` (the Mac does not sleep), `sandbox-exec`
with a no-network profile (the network ban does not depend on whether libraries honour
environment variables), `/usr/bin/time -l` (peak memory), and for the worker also
`nice -n 10` (the Mac stays responsive). The child lives in its own process group: a
stop reaches the whole group. stdout is discarded; stderr is returned to the caller only
to parse the `time -l` lines and is never printed: it may contain meeting text.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from transcribator.offline import OFFLINE_ENV
from transcribator.pipeline.audio import SANDBOXED_ENV
from transcribator.pipeline.tempfiles import cleanup_stale

CAFFEINATE = "/usr/bin/caffeinate"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
TIME = "/usr/bin/time"
NICE = "/usr/bin/nice"
PGREP = "/usr/bin/pgrep"
# Everything is allowed except the network: CoreML, the Neural Engine and Metal work
# through Mach and IOKit.
NO_NETWORK_PROFILE = "(version 1)(allow default)(deny network*)"


# Variables the child inherits from the parent. Everything else (tokens, proxies,
# library debug flags) is not passed on.
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE")
_ENV_PREFIXES = ("TRANSCRIBATOR_",)


def child_env(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    """Clean child environment: basic variables, our own settings and the network ban."""
    source = os.environ if parent is None else parent
    env = {k: v for k, v in source.items() if k in _ENV_KEEP or k.startswith(_ENV_PREFIXES)}
    # Only run_isolated may say "already sandboxed", and only when it really wraps the child.
    env.pop(SANDBOXED_ENV, None)
    env.update(OFFLINE_ENV)
    return env


@dataclass(frozen=True, slots=True)
class ChildResult:
    returncode: int
    stderr: bytes
    timed_out: bool


def wrappers(*, low_priority: bool = False) -> list[str]:
    """Child process wrappers. None outside macOS (the project targets the Mac)."""
    if sys.platform != "darwin":
        return []
    prefix = [CAFFEINATE, "-i", SANDBOX_EXEC, "-p", NO_NETWORK_PROFILE, TIME, "-l"]
    if low_priority:
        prefix += [NICE, "-n", "10"]
    return prefix


def _group_alive(proc: subprocess.Popen[bytes]) -> bool:
    proc.poll()  # reap the leader: a group of one zombie does not count as alive
    try:
        os.killpg(proc.pid, 0)
    except (ProcessLookupError, PermissionError):
        # macOS answers EPERM when only zombies are left in the group.
        return False
    return True


def stop_group(proc: subprocess.Popen[bytes], *, grace_s: float = 30.0) -> None:
    """SIGTERM to the whole group (Python cleans up temporary files), then SIGKILL.

    Wait for the whole group to empty, not just the leader: the leader is `caffeinate`,
    which exits at once, while Python inside may still be removing the WAV or hanging.
    """
    if _group_alive(proc):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while _group_alive(proc) and time.monotonic() < deadline:
        time.sleep(0.2)
    if _group_alive(proc):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)
    # After SIGKILL the child's temporary folder would otherwise stay until the next run.
    cleanup_stale()


def run_isolated(
    cmd: Sequence[str],
    *,
    timeout_s: float,
    low_priority: bool = False,
    on_start: Callable[[int], None] | None = None,
) -> ChildResult:
    """Run `cmd` under the wrappers and wait for it to finish, at most `timeout_s`."""
    prefix = wrappers(low_priority=low_priority)
    env = child_env()
    if prefix:
        # The child is already in the no-network sandbox: it must not try to nest one.
        env[SANDBOXED_ENV] = "1"
    proc = subprocess.Popen(
        [*prefix, *cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    stderr = b""
    timed_out = False
    try:
        if on_start is not None:
            on_start(proc.pid)
        try:
            _, stderr = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        stop_group(proc)
    return ChildResult(returncode=proc.returncode, stderr=stderr, timed_out=timed_out)


@contextlib.contextmanager
def keep_awake() -> Iterator[None]:
    """`caffeinate -i` while the block is open: the Mac does not sleep during a job."""
    if sys.platform != "darwin":
        yield
        return
    proc = subprocess.Popen([CAFFEINATE, "-i"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    try:
        yield
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def kill_orphan_group(pgid: int, marker: str = "transcribator process") -> bool:
    """Stop a child process group left behind by a worker crash.

    The PGID may have been reused by another program (e.g. after a reboot), so the group
    is killed only if it contains a process running our `marker` command."""
    if pgid <= 1 or pgid == os.getpgrp():
        return False
    try:
        found = subprocess.run(
            [PGREP, "-g", str(pgid), "-f", marker],
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if found.returncode != 0 or not found.stdout.strip():
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return True
            time.sleep(0.2)
    return True
