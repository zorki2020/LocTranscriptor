"""Benchmark of stacks on this Mac: speed, memory, swap, network.

Each recording is processed in a separate child process under the wrappers from
`pipeline/isolated.py`: `caffeinate -i`, network-less `sandbox-exec`, `/usr/bin/time -l`.
All child output is discarded: only the `time -l` lines are taken from its stderr, the
rest arrives as a metrics JSON. This way library logs cannot leak meeting text or a file
path. The output has no names: recordings are numbered `#1..N` in sorted order.

Memory is measured three ways, because none is complete on its own:
- peak total RSS of the whole process tree (`ps`, every `interval_s`), including
  external programs such as `fluidaudiocli`;
- peak footprint of the Python process from `time -l` (sees Metal memory, not children);
- peak used system memory (`vm_stat`: active + wired + compressed) relative to the
  start. Only this one sees Neural Engine memory, but other apps also count towards it.
Swap is the peak during the run relative to the start.

The network check (`--check-network`) polls `lsof -i` over the process tree. It is a
sample, not proof: a short connection between polls goes unseen. The sandbox is the
proof; Little Snitch or `nettop` is an independent check.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from transcribator.pipeline.audio import AudioError, probe_duration
from transcribator.pipeline.isolated import run_isolated
from transcribator.pipeline.tempfiles import job_prefix

VM_STAT = "/usr/bin/vm_stat"
SYSCTL = "/usr/sbin/sysctl"
PMSET = "/usr/bin/pmset"
PGREP = "/usr/bin/pgrep"
PS = "/bin/ps"
LSOF = "/usr/sbin/lsof"
DEFAULT_TIMEOUT_S = 4 * 3600

_RSS = re.compile(r"^\s*(\d+)\s+maximum resident set size", re.MULTILINE)
_FOOTPRINT = re.compile(r"^\s*(\d+)\s+peak memory footprint", re.MULTILINE)
_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_PAGES = re.compile(r"^(Pages [^:]+):\s+(\d+)\.?\s*$", re.MULTILINE)
_SWAP_USED = re.compile(r"used\s*=\s*([\d.]+)M")
_SPEED_LIMIT = re.compile(r"CPU_Speed_Limit\s*=\s*(\d+)")
_USED_PAGES = ("Pages active", "Pages wired down", "Pages occupied by compressor")
_GB = 1024**3


@dataclass(slots=True)
class BenchRow:
    stack: str
    index: int
    returncode: int
    error: str | None = None
    duration_s: float | None = None
    timings: dict[str, float] = field(default_factory=dict)
    speakers: int | None = None
    segments: int | None = None
    tree_rss: int | None = None
    peak_footprint: int | None = None
    memory_delta: int | None = None
    swap_delta_mb: float | None = None
    net_sockets: int | None = None
    cpu_speed_limit: int | None = None


def parse_time_l(text: str) -> tuple[int | None, int | None]:
    """From `/usr/bin/time -l` output: maximum RSS and peak footprint, bytes."""
    rss = _RSS.search(text)
    footprint = _FOOTPRINT.search(text)
    return (
        int(rss[1]) if rss else None,
        int(footprint[1]) if footprint else None,
    )


def parse_vm_stat(text: str) -> int | None:
    """Used system memory from `vm_stat`, bytes: active + wired + compressed."""
    page = _PAGE_SIZE.search(text)
    if not page:
        return None
    pages = {name: int(count) for name, count in _PAGES.findall(text)}
    if not all(name in pages for name in _USED_PAGES):
        return None
    return sum(pages[name] for name in _USED_PAGES) * int(page[1])


def parse_swap_used_mb(text: str) -> float | None:
    """`sysctl vm.swapusage` -> used swap, MB."""
    match = _SWAP_USED.search(text)
    return float(match[1]) if match else None


def parse_speed_limit(text: str) -> int | None:
    """`pmset -g therm` -> CPU speed limit in percent, if macOS reports it."""
    match = _SPEED_LIMIT.search(text)
    return int(match[1]) if match else None


def parse_rss_kb_sum(text: str) -> int | None:
    """`ps -o rss=` over several PIDs -> total RSS, bytes."""
    values = [int(v) for v in text.split() if v.isdigit()]
    return sum(values) * 1024 if values else None


def count_inet_lines(lsof_output: str) -> int:
    """Number of network sockets in `lsof -i` output (the first line is a header)."""
    lines = [line for line in lsof_output.splitlines() if line.strip()]
    return max(len(lines) - 1, 0)


def _read(cmd: list[str]) -> str | None:
    """stdout of a system utility, or None if it could not be run."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout


def _descendants(pid: int) -> list[int]:
    found: list[int] = []
    queue = [pid]
    while queue:
        current = queue.pop()
        found.append(current)
        children = _read([PGREP, "-P", str(current)]) or ""
        queue += [int(p) for p in children.split() if p.isdigit()]
    return found


def _max[N: (int, float)](current: N | None, new: N | None) -> N | None:
    if new is None:
        return current
    return new if current is None else max(current, new)


class _Sampler:
    """Background polling of memory, swap, network and throttling while the child runs."""

    def __init__(self, interval_s: float, check_network: bool) -> None:
        self._interval_s = interval_s
        self._check_network = check_network
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.baseline = parse_vm_stat(_read([VM_STAT]) or "")
        self.peak = self.baseline
        self.swap_baseline = parse_swap_used_mb(_read([SYSCTL, "vm.swapusage"]) or "")
        self.swap_peak = self.swap_baseline
        self.tree_rss: int | None = None
        # None until lsof has run once: a failure must not read as "0 sockets".
        self.sockets: int | None = None
        self.speed_limit = parse_speed_limit(_read([PMSET, "-g", "therm"]) or "")

    def start(self, pid: int) -> None:
        self._thread = threading.Thread(target=self._loop, args=(pid,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._tick_therm()

    def _loop(self, pid: int) -> None:
        ticks = 0
        while not self._stop.wait(self._interval_s):
            self.peak = _max(self.peak, parse_vm_stat(_read([VM_STAT]) or ""))
            swap = parse_swap_used_mb(_read([SYSCTL, "vm.swapusage"]) or "")
            self.swap_peak = _max(self.swap_peak, swap)
            pids = ",".join(str(p) for p in _descendants(pid))
            rss = parse_rss_kb_sum(_read([PS, "-o", "rss=", "-p", pids]) or "")
            self.tree_rss = _max(self.tree_rss, rss)
            if self._check_network:
                lsof = _read([LSOF, "-nP", "-a", "-i", "-p", pids])
                if lsof is not None:
                    self.sockets = max(self.sockets or 0, count_inet_lines(lsof))
            ticks += 1
            if ticks % 15 == 0:
                self._tick_therm()

    def _tick_therm(self) -> None:
        limit = parse_speed_limit(_read([PMSET, "-g", "therm"]) or "")
        if limit is not None:
            self.speed_limit = limit if self.speed_limit is None else min(self.speed_limit, limit)

    @property
    def memory_delta(self) -> int | None:
        if self.baseline is None or self.peak is None:
            return None
        return self.peak - self.baseline

    @property
    def swap_delta_mb(self) -> float | None:
        if self.swap_baseline is None or self.swap_peak is None:
            return None
        return self.swap_peak - self.swap_baseline


def find_inputs(path: Path) -> list[Path]:
    """The file itself, or every file with an audio track in the directory (not recursive),
    checked with ffprobe."""
    if path.is_file():
        return [path]
    candidates = sorted(p for p in path.iterdir() if p.is_file() and not p.name.startswith("."))
    inputs: list[Path] = []
    for candidate in candidates:
        try:
            probe_duration(candidate)
        except AudioError:
            continue
        inputs.append(candidate)
    return inputs


def child_command(
    src: Path, out: Path, metrics: Path, *, stack: str, engine: str, language: str = "en"
) -> list[str]:
    """Command that processes one recording. Separate so a test can substitute the child."""
    return [
        sys.executable,
        "-m",
        "transcribator",
        "process",
        str(src),
        "--engine",
        engine,
        "--stack",
        stack,
        "--language",
        language,
        "--out",
        str(out),
        "--metrics-json",
        str(metrics),
    ]


def run_one(
    src: Path,
    out: Path,
    *,
    stack: str,
    index: int,
    engine: str = "local",
    language: str = "en",
    check_network: bool = False,
    interval_s: float = 2.0,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> BenchRow:
    with tempfile.TemporaryDirectory(prefix=job_prefix("bench")) as tmp:
        metrics = Path(tmp) / "metrics.json"
        sampler = _Sampler(interval_s, check_network)
        try:
            child = run_isolated(
                child_command(src, out, metrics, stack=stack, engine=engine, language=language),
                timeout_s=timeout_s,
                on_start=sampler.start,
            )
        finally:
            sampler.stop()
        try:
            data = json.loads(metrics.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

    _rss, footprint = parse_time_l(child.stderr.decode("utf-8", errors="replace"))
    error = "timeout" if child.timed_out else data.get("error")
    if child.returncode != 0 and not error:
        error = "no_metrics"
    return BenchRow(
        stack=stack,
        index=index,
        returncode=child.returncode,
        error=str(error) if error else None,
        duration_s=data.get("duration_s"),
        timings={str(k): float(v) for k, v in data.get("timings", {}).items()},
        speakers=data.get("speakers"),
        segments=data.get("segments"),
        tree_rss=sampler.tree_rss,
        peak_footprint=footprint,
        memory_delta=sampler.memory_delta,
        swap_delta_mb=sampler.swap_delta_mb,
        net_sockets=sampler.sockets,
        cpu_speed_limit=sampler.speed_limit,
    )


def _num(value: float | None, fmt: str, scale: float | None = None) -> str:
    if value is None:
        return "-"
    return format(value / scale if scale else value, fmt)


HEADER = (
    "stack           #      min    norm    diar      asr    total     ×RT    treeRSS,GB  "
    "footprPy,GB     Δmem,GB peakSwap,MB speakers   net  CPU% code"
)


def format_row(row: BenchRow) -> str:
    """A table row. Numbers and codes only, no names or text."""
    total = row.timings.get("engine_total")
    speed = row.duration_s / total if row.duration_s and total and total > 0.05 else None
    cells = [
        f"{row.stack:<15}",
        f"#{row.index:<2}",
        _num(row.duration_s, "6.1f", 60),
        _num(row.timings.get("normalize"), "7.1f"),
        _num(row.timings.get("diarize"), "7.1f"),
        _num(row.timings.get("transcribe"), "8.1f"),
        _num(total, "8.1f"),
        _num(speed, "7.1f"),
        _num(row.tree_rss, "13.2f", _GB),
        _num(row.peak_footprint, "12.2f", _GB),
        _num(row.memory_delta, "11.2f", _GB),
        _num(row.swap_delta_mb, "11.0f"),
        _num(row.speakers, "8d"),
        _num(row.net_sockets, "5d"),
        _num(row.cpu_speed_limit, "5d"),
        str(row.returncode) + (f" {row.error}" if row.error else ""),
    ]
    return " ".join(cells)


def output_path(src: Path, stack: str, out_dir: Path | None) -> Path:
    # Full name, not stem: `a.mp3` and `a.mp4` must not produce the same output file.
    return (out_dir or src.parent) / f"{src.name}.{stack}.md"
