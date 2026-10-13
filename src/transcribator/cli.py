"""Command-line interface.

    transcribator serve [--port 8765]                  (web UI on 127.0.0.1)
    transcribator serve --app --port 0 --parent-pid N  (started by LocTranscriptor.app)

    transcribator models download --stack fluidaudio     (the only command that uses the network)
    transcribator process meeting.mp4 --stack fluidaudio
    transcribator process meeting.mp3 --engine fake        (format check without models)
    transcribator bench recording_or_dir --stack fluidaudio
    transcribator wer reference.txt transcript.md
    transcribator autostart install|remove|status   (start at macOS login)

Every command except `models download` denies models network access
(`transcribator.offline`). Output holds only timings, metrics and error codes: no file
names, no text.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import tempfile
import wave
from math import pi, sin
from pathlib import Path

from transcribator.backends.errors import BackendError
from transcribator.eval import transcript_text, word_error_rate
from transcribator.i18n import RECORDING_LANGS, default_recording_lang
from transcribator.models import RawResult
from transcribator.offline import allow_download, enforce_offline
from transcribator.paths import DataDirError
from transcribator.pipeline.audio import SANDBOXED_ENV, AudioError
from transcribator.pipeline.format_md import render_markdown
from transcribator.pipeline.interfaces import Engine
from transcribator.pipeline.merge import merge_result
from transcribator.pipeline.run import ProcessResult, process_audio
from transcribator.pipeline.tempfiles import cleanup_stale, job_prefix
from transcribator.stacks import DEFAULT_STACK, STACKS


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.handler is _cmd_download:
        allow_download()
    else:
        enforce_offline()
    if args.handler is not _cmd_process:
        # Set by run_isolated for its sandboxed child only; anywhere else it would turn
        # off the audio helper's sandbox.
        os.environ.pop(SANDBOXED_ENV, None)
    # Results and temporary files contain meeting text: user-only.
    os.umask(0o077)
    _exit_cleanly_on_signals()
    cleanup_stale()
    try:
        return int(args.handler(args))
    except AudioError as exc:
        # Error code only: the file name and ffmpeg output may contain the meeting title.
        print(f"Audio error: {exc.code}", file=sys.stderr)
        return 2
    except BackendError as exc:
        print(f"Model error: {exc.code}", file=sys.stderr)
        return 3
    except DataDirError:
        print(
            "Error: the data directory is not empty and does not look like a service"
            " data directory (check TRANSCRIBATOR_DATA_DIR)",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        # OSError text contains a path, and the path derives from the meeting title.
        print(f"File error: {type(exc).__name__}", file=sys.stderr)
        return 2
    except Exception as exc:
        # A model or library exception message may contain anything: class name only.
        print(f"Failure: {type(exc).__name__}", file=sys.stderr)
        return 1


def _exit_cleanly_on_signals() -> None:
    """Turn SIGTERM and SIGHUP into a normal exit so `finally` blocks and
    `TemporaryDirectory` run and the meeting's temporary WAV does not stay on disk."""

    def handler(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def _positive(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribator",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    process = sub.add_parser("process", help="audio to a Markdown transcript")
    process.add_argument("input", type=Path)
    process.add_argument("--engine", choices=["local", "fake"], default="local")
    process.add_argument("--stack", choices=STACKS, default=DEFAULT_STACK)
    process.add_argument(
        "--language",
        choices=RECORDING_LANGS,
        default=default_recording_lang(),
        help="language of the recording (default: TRANSCRIBATOR_LANGUAGE or en)",
    )
    process.add_argument("--out", type=Path, help="default: next to the input file")
    process.add_argument("--title", default="")
    process.add_argument("--recorded-at", default=None)
    process.add_argument("--min-speakers", type=int, default=None)
    process.add_argument("--max-speakers", type=int, default=None)
    process.add_argument("--metrics-json", type=Path, help="where to write metrics (for bench)")
    process.add_argument(
        "--json",
        type=Path,
        help="transcript as JSON (for the worker); without --out no Markdown is written",
    )
    process.set_defaults(handler=_cmd_process)

    bench = sub.add_parser("bench", help="measure speed and memory; output has no names or text")
    bench.add_argument("input", type=Path, help="a recording or a directory of recordings")
    bench.add_argument("--stack", nargs="+", choices=STACKS, default=[DEFAULT_STACK])
    bench.add_argument("--language", choices=RECORDING_LANGS, default=default_recording_lang())
    bench.add_argument("--engine", choices=["local", "fake"], default="local")
    bench.add_argument("--out-dir", type=Path, help="default: next to the recordings")
    bench.add_argument("--check-network", action="store_true", help="count network sockets")
    bench.add_argument(
        "--interval", type=_positive, default=2.0, help="memory sampling period, seconds"
    )
    bench.add_argument(
        "--timeout", type=_positive, default=4 * 3600.0, help="limit per recording, seconds"
    )
    bench.set_defaults(handler=_cmd_bench)

    models = sub.add_parser("models", help="models")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    download = models_sub.add_parser("download", help="download a stack's models (needs network)")
    download.add_argument("--stack", nargs="+", choices=STACKS, default=[DEFAULT_STACK])
    download.set_defaults(handler=_cmd_download)

    render = sub.add_parser("render", help="build Markdown from a saved .raw.json")
    render.add_argument("raw_json", type=Path)
    render.add_argument("--out", type=Path, help="default: alongside, with a .md extension")
    render.add_argument("--title", default="")
    render.set_defaults(handler=_cmd_render)

    wer = sub.add_parser("wer", help="WER between a reference text and a Markdown transcript")
    wer.add_argument("reference", type=Path)
    wer.add_argument("transcript", type=Path)
    wer.add_argument("--from-s", type=float, default=None, help="window start, seconds")
    wer.add_argument("--to-s", type=float, default=None, help="window end, seconds")
    wer.set_defaults(handler=_cmd_wer)

    serve = sub.add_parser("serve", help="web UI on 127.0.0.1 and the queue worker")
    serve.add_argument("--port", type=_port, default=8765, help="0 picks a free port (--app)")
    serve.add_argument(
        "--app",
        action="store_true",
        help="mode of LocTranscriptor.app: access key on stdin, ready line on stdout",
    )
    serve.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    serve.set_defaults(handler=_cmd_serve)

    autostart = sub.add_parser("autostart", help="start at macOS login and the menu bar icon")
    autostart.add_argument("action", choices=["install", "remove", "status"])
    autostart.set_defaults(handler=_cmd_autostart)

    worker = sub.add_parser("worker", help="queue worker only (started by serve)")
    worker.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    worker.set_defaults(handler=_cmd_worker)
    return parser


def _cmd_autostart(args: argparse.Namespace) -> int:
    from transcribator import autostart  # noqa: PLC0415

    action = {"install": autostart.install, "remove": autostart.remove}.get(
        args.action, autostart.status
    )
    try:
        lines = action()
    except FileNotFoundError:
        print(
            "Project environment not found: run `uv sync` in the project directory",
            file=sys.stderr,
        )
        return 2
    for line in lines:
        print(line)
    return 0


def _port(value: str) -> int:
    number = int(value)
    if number != 0 and not 1024 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be 0 or between 1024 and 65535")
    return number


# Exit codes of `serve --app` that the app tells apart from a crash.
APP_EXIT_ALREADY_RUNNING = 75
APP_EXIT_CONFIG = 2


def _cmd_serve(args: argparse.Namespace) -> int:
    from transcribator.autostart import trim_log, under_launchd  # noqa: PLC0415
    from transcribator.serve import (  # noqa: PLC0415
        AlreadyRunning,
        BadAccessKey,
        run_app_server,
        run_server,
    )

    if args.app:
        try:
            return run_app_server(args.port, parent_pid=args.parent_pid, key_stream=sys.stdin)
        except AlreadyRunning:
            print("The service is already running with this data directory", file=sys.stderr)
            return APP_EXIT_ALREADY_RUNNING
        except BadAccessKey:
            print("No valid access key on stdin", file=sys.stderr)
            return APP_EXIT_CONFIG
    if args.port == 0:
        print("Port 0 is only for --app", file=sys.stderr)
        return 2
    if under_launchd():
        trim_log()
    try:
        return run_server(args.port)
    except AlreadyRunning:
        print("The service is already running with this data directory", file=sys.stderr)
        # Exit code 0: for launchd this is not a crash. With a non-zero code the agent
        # would restart itself forever while a manually started instance is running.
        return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from transcribator.serve import run_worker  # noqa: PLC0415
    from transcribator.worker.loop import WorkerBusy  # noqa: PLC0415

    try:
        return run_worker(parent_pid=args.parent_pid)
    except WorkerBusy:
        print("The worker is already running with this data directory", file=sys.stderr)
        return 4


def _make_engine(args: argparse.Namespace) -> Engine:
    if args.engine == "fake":
        from transcribator.backends.fake import FakeEngine, demo_raw_result  # noqa: PLC0415

        return FakeEngine(demo_raw_result())
    from transcribator.stacks import build_local_engine  # noqa: PLC0415

    return build_local_engine(
        args.stack,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        language=args.language,
    )


def _cmd_process(args: argparse.Namespace) -> int:
    suffix = "fake" if args.engine == "fake" else args.stack
    out: Path | None = args.out
    if out is None and args.json is None:
        out = args.input.with_suffix(f".{suffix}.md")
    for target in (out, args.json):
        if target is None:
            continue
        if target.resolve() == args.input.resolve():
            # The source recording is never modified, whatever the arguments.
            print("Error: the output path is the recording itself", file=sys.stderr)
            return 2
        if not target.parent.is_dir():
            # Check before processing, not after the models have run for minutes.
            _write_metrics(args.metrics_json, {"error": "out_dir_missing"})
            print("Error: the output directory does not exist", file=sys.stderr)
            return 2
    try:
        result = process_audio(args.input, _make_engine(args))
    except (AudioError, BackendError) as exc:
        _write_metrics(args.metrics_json, {"error": exc.code})
        raise
    except Exception as exc:
        _write_metrics(args.metrics_json, {"error": f"crash_{type(exc).__name__}"})
        raise
    if out is not None:
        title = args.title or args.input.stem
        out.write_text(_render(result, title, args.recorded_at), encoding="utf-8")
    if args.json is not None:
        args.json.write_text(
            json.dumps(result.transcript.to_dict(), ensure_ascii=False), encoding="utf-8"
        )
    _write_metrics(
        args.metrics_json,
        {
            "duration_s": result.duration_s,
            "timings": result.timings,
            "speakers": len(result.transcript.speakers),
            "segments": len(result.transcript.segments),
        },
    )
    print_timings(result.duration_s, result.timings)
    # The path is not printed: it derives from the recording's name.
    print("Done")
    return 0


def _write_metrics(path: Path | None, data: dict[str, object]) -> None:
    # A failure to write metrics must not mask the original error in the traceback.
    if path is not None:
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(data), encoding="utf-8")


def _render(result: ProcessResult, title: str, recorded_at: str | None) -> str:
    return render_markdown(
        result.transcript, title=title, recorded_at=recorded_at, summary=result.summary
    )


def print_timings(duration_s: float, timings: dict[str, float]) -> None:
    """Stage timings and how many times faster than real time processing ran. No content."""
    print(f"Recording duration: {duration_s / 60:.1f} min")
    for name, seconds in timings.items():
        print(f"  {name}: {seconds:.1f} s")
    total = timings.get("engine_total")
    if total and total > 0.05:  # below this the measurement is meaningless (stubs)
        print(f"  speed: {duration_s / total:.1f}x real time")


def _cmd_bench(args: argparse.Namespace) -> int:
    from transcribator import bench  # noqa: PLC0415

    inputs = bench.find_inputs(args.input)
    if not inputs:
        print("No files with an audio track", file=sys.stderr)
        return 2
    print(bench.HEADER, flush=True)
    failed = False
    for stack in args.stack:
        for index, src in enumerate(inputs, start=1):
            row = bench.run_one(
                src,
                bench.output_path(src, stack, args.out_dir),
                stack=stack,
                index=index,
                engine=args.engine,
                language=args.language,
                check_network=args.check_network,
                interval_s=args.interval,
                timeout_s=args.timeout,
            )
            failed = failed or row.returncode != 0
            print(bench.format_row(row), flush=True)
    return 1 if failed else 0


def _cmd_download(args: argparse.Namespace) -> int:
    """Run each stack on a synthetic tone: models are downloaded on first use."""
    from transcribator.stacks import build_local_engine  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix=job_prefix("download")) as tmp:
        tone = Path(tmp) / "tone.wav"
        write_tone(tone)
        for stack in args.stack:
            build_local_engine(stack).analyze(tone)
            print(f"Models for stack {stack} are in place")
    return 0


def write_tone(path: Path, seconds: float = 3.0, rate: int = 16000) -> None:
    """440 Hz sine, mono 16 kHz 16-bit: a warm-up input with no real data."""
    frames = bytearray()
    for i in range(int(seconds * rate)):
        sample = int(0.3 * 32767 * sin(2 * pi * 440 * i / rate))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(bytes(frames))


def _cmd_render(args: argparse.Namespace) -> int:
    data = json.loads(args.raw_json.read_text(encoding="utf-8"))
    raw = RawResult.from_dict(data)
    transcript = merge_result(raw, float(data["duration_s"]))
    title = args.title or args.raw_json.name.split(".")[0]
    out: Path = args.out or args.raw_json.with_suffix(".md")
    out.write_text(render_markdown(transcript, title=title), encoding="utf-8")
    print("Done")
    return 0


def _cmd_wer(args: argparse.Namespace) -> int:
    reference = args.reference.read_text(encoding="utf-8")
    hypothesis = transcript_text(
        args.transcript.read_text(encoding="utf-8"), args.from_s, args.to_s
    )
    print(f"WER: {word_error_rate(reference, hypothesis) * 100:.1f}%")
    return 0
