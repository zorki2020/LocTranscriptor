from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg
from transcribator.bench import (
    BenchRow,
    count_inet_lines,
    find_inputs,
    format_row,
    output_path,
    parse_rss_kb_sum,
    parse_speed_limit,
    parse_swap_used_mb,
    parse_time_l,
    parse_vm_stat,
)
from transcribator.cli import main

TIME_L = """\
       12.34 real         8.00 user         1.00 sys
          1234567890  maximum resident set size
                   0  average shared memory size
               98765  page reclaims
          2345678901  peak memory footprint
"""

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            100000.
Pages inactive:                           90000.
Pages wired down:                         50000.
Pages occupied by compressor:             25000.
"""


def test_parse_time_l() -> None:
    assert parse_time_l(TIME_L) == (1234567890, 2345678901)
    assert parse_time_l("мусор") == (None, None)


def test_parse_vm_stat_counts_active_wired_and_compressed() -> None:
    assert parse_vm_stat(VM_STAT) == (100000 + 50000 + 25000) * 16384
    assert parse_vm_stat("Pages active: 1.") is None


def test_parse_swap_and_speed_limit() -> None:
    swap = "vm.swapusage: total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)"
    assert parse_swap_used_mb(swap) == pytest.approx(1024.5)
    assert parse_speed_limit("CPU_Scheduler_Limit = 100\nCPU_Speed_Limit = 87\n") == 87
    assert parse_speed_limit("Note: No thermal warning level has been recorded") is None


def test_parse_rss_kb_sum() -> None:
    assert parse_rss_kb_sum(" 1024\n 2048\n") == 3072 * 1024
    assert parse_rss_kb_sum("") is None


def test_count_inet_lines() -> None:
    header = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"
    assert count_inet_lines("") == 0
    assert count_inet_lines(header) == 0
    assert count_inet_lines(header + "python 1 u 3u IPv4 0x1 0t0 TCP a->b\n") == 1


def test_format_row_has_only_numbers_and_codes() -> None:
    row = BenchRow(
        stack="fluidaudio",
        index=1,
        returncode=0,
        duration_s=7200.0,
        timings={"normalize": 10.0, "diarize": 60.0, "transcribe": 230.0, "engine_total": 300.0},
        speakers=3,
        tree_rss=2 * 1024**3,
        net_sockets=0,
    )
    line = format_row(row)
    assert line.startswith("fluidaudio")
    assert "24.0" in line  # ×RT
    assert "120.0" in line  # minutes
    assert "2.00" in line  # RSS, GB


def test_output_path_goes_next_to_input_or_to_out_dir(tmp_path: Path) -> None:
    src = tmp_path / "a" / "rec.mp4"
    assert output_path(src, "fluidaudio", None) == tmp_path / "a" / "rec.mp4.fluidaudio.md"
    assert output_path(src, "fluidaudio", tmp_path) == tmp_path / "rec.mp4.fluidaudio.md"
    # Recordings with the same name and different extensions do not overwrite each other.
    assert output_path(src.with_suffix(".mp3"), "fluidaudio", None) != output_path(
        src, "fluidaudio", None
    )


@requires_ffmpeg
def test_find_inputs_keeps_only_audio_sorted(sine_audio: Path, tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("не аудио", encoding="utf-8")
    (tmp_path / ".hidden.mp3").write_bytes(b"")
    second = tmp_path / "z.mp3"
    second.write_bytes(sine_audio.read_bytes())
    assert find_inputs(tmp_path) == [sine_audio, second]
    assert find_inputs(second) == [second]


@requires_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate and time -l are macOS-only")
def test_bench_end_to_end_prints_no_names(
    sine_audio: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "Личные записи"
    folder.mkdir()
    secret = folder / "Встреча с Иваном.mp3"
    secret.write_bytes(sine_audio.read_bytes())

    code = main(["bench", str(folder), "--engine", "fake", "--interval", "0.2"])

    out = capsys.readouterr()
    assert code == 0
    assert "Иван" not in out.out + out.err
    assert "Личные" not in out.out + out.err
    row = out.out.splitlines()[1]
    assert row.startswith("fluidaudio")
    assert "#1" in row
    assert (folder / "Встреча с Иваном.mp3.fluidaudio.md").exists()


@requires_ffmpeg
@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate and time -l are macOS-only")
def test_bench_reports_error_code_of_failed_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from transcribator.bench import run_one  # noqa: PLC0415

    bad = tmp_path / "Личное.mp3"
    bad.write_bytes(b"not audio")
    row = run_one(bad, tmp_path / "o.md", stack="fluidaudio", index=1, engine="fake")
    assert row.returncode == 2
    assert row.error == "probe_failed"
    assert "Личное" not in format_row(row)


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate and time -l are macOS-only")
def test_child_output_never_reaches_bench_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The child prints the recording name and "text" to stdout and stderr: bench hides them."""
    from transcribator import bench  # noqa: PLC0415

    script = tmp_path / "child.py"
    script.write_text(
        "import json, sys\n"
        "print('Встреча с Иваном: секретный текст')\n"
        "print('Встреча с Иваном: секретный текст', file=sys.stderr)\n"
        "json.dump({'duration_s': 60.0, 'timings': {'engine_total': 2.0}, 'speakers': 2},"
        " open(sys.argv[1], 'w'))\n",
        encoding="utf-8",
    )

    def fake_child(src: Path, out: Path, metrics: Path, **kw: str) -> list[str]:
        return [sys.executable, str(script), str(metrics)]

    monkeypatch.setattr(bench, "child_command", fake_child)

    row = bench.run_one(tmp_path / "x.mp3", tmp_path / "o.md", stack="fluidaudio", index=1)
    line = bench.format_row(row)

    assert row.returncode == 0
    assert row.duration_s == 60.0
    assert "Иван" not in line + capsys.readouterr().out
    assert row.peak_footprint is not None  # the time -l lines were parsed


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
def test_child_has_no_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under the benchmark sandbox even a connection to localhost is denied."""
    from transcribator import bench  # noqa: PLC0415

    script = tmp_path / "net.py"
    script.write_text(
        "import json, socket, sys\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=2)\n"
        "    result = 'connected'\n"
        "except PermissionError:\n"
        "    result = 'denied'\n"
        "except OSError:\n"
        "    result = 'refused'\n"
        "json.dump({'error': result}, open(sys.argv[1], 'w'))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bench,
        "child_command",
        lambda src, out, metrics, **kw: [sys.executable, str(script), str(metrics)],
    )

    row = bench.run_one(tmp_path / "x.mp3", tmp_path / "o.md", stack="fluidaudio", index=1)

    assert row.error == "denied"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_hanging_child_is_stopped_by_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from transcribator import bench  # noqa: PLC0415

    monkeypatch.setattr(
        bench,
        "child_command",
        lambda src, out, metrics, **kw: [sys.executable, "-c", "import time; time.sleep(60)"],
    )

    row = bench.run_one(
        tmp_path / "x.mp3", tmp_path / "o.md", stack="fluidaudio", index=1, timeout_s=1.0
    )

    assert row.error == "timeout"
    assert row.returncode != 0


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_sampler_sees_process_tree_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child starts a ~200 MB grandchild: the tree RSS peak sees it."""
    from transcribator import bench  # noqa: PLC0415

    # Written pages, not a zero bytearray: macOS does not count untouched zero pages in RSS.
    grandchild = "b = b'x' * (200 * 1024 * 1024); import time; time.sleep(2)"
    script = tmp_path / "parent.py"
    script.write_text(
        "import json, subprocess, sys\n"
        f"subprocess.run([sys.executable, '-c', {grandchild!r}])\n"
        "json.dump({'duration_s': 1.0, 'timings': {}}, open(sys.argv[1], 'w'))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        bench,
        "child_command",
        lambda src, out, metrics, **kw: [sys.executable, str(script), str(metrics)],
    )

    row = bench.run_one(
        tmp_path / "x.mp3",
        tmp_path / "o.md",
        stack="fluidaudio",
        index=1,
        interval_s=0.2,
        check_network=True,
    )

    assert row.returncode == 0
    assert row.tree_rss is not None and row.tree_rss > 150 * 1024 * 1024
    assert row.net_sockets == 0


def test_child_env_drops_tokens_and_proxies() -> None:
    from transcribator.pipeline.isolated import child_env  # noqa: PLC0415

    env = child_env(
        {
            "PATH": "/bin",
            "HOME": "/Users/x",
            "HF_TOKEN": "secret",
            "HTTPS_PROXY": "http://proxy",
            "DUMP_VAD": "1",
            "TRANSCRIBATOR_FLUIDAUDIO_CLI": "/opt/fa",
        }
    )
    assert env["PATH"] == "/bin" and env["TRANSCRIBATOR_FLUIDAUDIO_CLI"] == "/opt/fa"
    assert "HF_TOKEN" not in env and "HTTPS_PROXY" not in env and "DUMP_VAD" not in env
    assert env["HF_HUB_OFFLINE"] == "1"


def test_child_command_passes_recording_language() -> None:
    from transcribator import bench  # noqa: PLC0415

    cmd = bench.child_command(
        Path("a.mp3"),
        Path("a.md"),
        Path("m.json"),
        stack="fluidaudio",
        engine="local",
        language="ru",
    )
    assert cmd[cmd.index("--language") + 1] == "ru"
