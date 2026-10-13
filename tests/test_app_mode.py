"""App mode of the service (LocTranscriptor.app): access key, first-run models, settings,
adding recordings by path, `serve --app`, the AVFoundation helper, worker gating.

Synthetic data only: recordings are fake files whose probe is stubbed.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from transcribator import i18n, serve, settings
from transcribator.backends import fluidaudio
from transcribator.cli import APP_EXIT_ALREADY_RUNNING, APP_EXIT_CONFIG, main
from transcribator.i18n import RECORDING_LANG_ENV, UI_LANG_ENV
from transcribator.interpreter import python_command
from transcribator.modelstore import MODELS_ENV, ModelStore, parse_manifest
from transcribator.paths import DATA_ENV, db_path, ensure_private_dir
from transcribator.pipeline import audio
from transcribator.pipeline.audio import AudioError
from transcribator.storage import db, repo
from transcribator.web import app as web_app
from transcribator.worker.loop import Worker

PORT = 51234
BASE = f"http://127.0.0.1:{PORT}"
KEY = "ab" * 32


class FakeStore(ModelStore):
    def __init__(self, ready: bool = False) -> None:
        super().__init__(Path("/nonexistent"), parse_manifest({"repos": []}))
        self.is_ready = ready
        self.started = 0

    def ready(self) -> bool:
        return self.is_ready

    def start(self) -> None:
        self.started += 1

    def free_bytes(self) -> int:
        return 10 * 1024**3


class AppEnv:
    def __init__(self, root: Path, store: FakeStore) -> None:
        self.root = ensure_private_dir(root)
        self.store = store
        self.app = web_app.create_app(
            db_file=db_path(self.root),
            data_root=self.root,
            port=PORT,
            app_mode=True,
            access_key=KEY,
            models=store,
        )
        self.client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        self.client.cookies.set("tb_key", KEY, domain="127.0.0.1")
        self.token: str = self.app.state.csrf_token

    def post(self, url: str, data: Any = None) -> httpx.Response:
        headers = {"Origin": BASE, "X-CSRF-Token": self.token, "HX-Request": "true"}
        return self.client.post(url, data=data, headers=headers)

    def meetings(self) -> list[tuple[str, str]]:
        conn = db.connect(db_path(self.root))
        try:
            rows = conn.execute("SELECT source_kind, source_ref FROM meeting").fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _clean_languages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(UI_LANG_ENV, raising=False)
    monkeypatch.delenv(RECORDING_LANG_ENV, raising=False)
    i18n.set_preferences(ui_lang=None, recording_lang=None)
    yield
    i18n.set_preferences(ui_lang=None, recording_lang=None)


@pytest.fixture
def ready_env(tmp_path: Path) -> Iterator[AppEnv]:
    env = AppEnv(tmp_path / "data", FakeStore(ready=True))
    yield env
    env.client.close()


@pytest.fixture
def fresh_env(tmp_path: Path) -> Iterator[AppEnv]:
    env = AppEnv(tmp_path / "data", FakeStore(ready=False))
    yield env
    env.client.close()


# --- access key ----------------------------------------------------------------------


def test_every_request_needs_the_access_key(ready_env: AppEnv) -> None:
    anonymous = TestClient(ready_env.app, base_url=BASE)
    for url in ("/", "/static/app.css", "/api/status", "/meetings", "/settings"):
        assert anonymous.get(url).status_code == 403, url
    anonymous.cookies.set("tb_key", "cd" * 32, domain="127.0.0.1")
    assert anonymous.get("/api/status").status_code == 403
    assert ready_env.client.get("/api/status").status_code == 200
    assert ready_env.client.get("/static/app.css").status_code == 200


def test_key_does_not_replace_host_origin_and_csrf(ready_env: AppEnv) -> None:
    assert ready_env.client.get("/", headers={"Host": "evil.example"}).status_code == 403
    response = ready_env.client.post(
        "/settings", data={"ui_lang": "ru", "recording_lang": "ru"}, headers={"Origin": BASE}
    )
    assert response.status_code == 403  # no CSRF token
    response = ready_env.client.post(
        "/settings",
        data={"ui_lang": "ru", "recording_lang": "ru", "csrf": ready_env.token},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_status_reports_models_and_language(ready_env: AppEnv) -> None:
    data = ready_env.client.get("/api/status").json()
    assert data == {
        "queued": 0,
        "processing": 0,
        "done": 0,
        "error": 0,
        "models_ready": True,
        "ui_lang": "en",
    }


def test_developer_mode_has_no_app_routes(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "d")
    app = web_app.create_app(db_file=db_path(root), data_root=root, port=PORT)
    client = TestClient(app, base_url=BASE)
    assert client.get("/setup").status_code == 404
    assert client.get("/settings").status_code == 404
    assert set(client.get("/api/status").json()) == {"queued", "processing", "done", "error"}
    token = app.state.csrf_token
    response = client.post(
        "/enqueue",
        data={"path": "/x.mp3", "language": "en"},
        headers={"Origin": BASE, "X-CSRF-Token": token},
    )
    assert response.status_code == 404


def test_app_mode_requires_key_and_store(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "d")
    with pytest.raises(ValueError):
        web_app.create_app(db_file=db_path(root), data_root=root, port=PORT, app_mode=True)


# --- first run -----------------------------------------------------------------------


def test_first_run_goes_to_setup(fresh_env: AppEnv) -> None:
    response = fresh_env.client.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    page = fresh_env.client.get("/setup")
    assert page.status_code == 200
    assert "huggingface.co" in page.text
    assert fresh_env.store.started == 0  # nothing downloads until the user asks


def test_setup_start_and_status(fresh_env: AppEnv) -> None:
    response = fresh_env.post("/setup/start")
    assert response.status_code == 200
    assert fresh_env.store.started == 1
    assert "HX-Redirect" not in fresh_env.client.get("/setup/status").headers
    fresh_env.store.is_ready = True
    done = fresh_env.client.get("/setup/status")
    assert done.headers["HX-Redirect"] == "/"
    assert fresh_env.client.get("/").status_code == 200


def test_setup_start_does_nothing_when_ready(ready_env: AppEnv) -> None:
    ready_env.post("/setup/start")
    assert ready_env.store.started == 0


# --- settings ------------------------------------------------------------------------


def test_settings_are_saved_and_applied(ready_env: AppEnv) -> None:
    assert ready_env.client.get("/settings").status_code == 200
    response = ready_env.post("/settings", {"ui_lang": "ru", "recording_lang": "en"})
    assert response.status_code == 200 and response.headers["HX-Refresh"] == "true"
    assert i18n.ui_lang() == "ru" and i18n.default_recording_lang() == "en"
    path = settings.settings_path(ready_env.root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert settings.load(ready_env.root) == settings.Settings("ru", "en")
    assert ready_env.client.get("/api/status").json()["ui_lang"] == "ru"


def test_bad_settings_are_rejected(ready_env: AppEnv) -> None:
    response = ready_env.post("/settings", {"ui_lang": "de", "recording_lang": "en"})
    assert response.status_code == 400
    assert not settings.settings_path(ready_env.root).exists()


def test_saved_settings_win_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(UI_LANG_ENV, "en")
    root = ensure_private_dir(tmp_path / "d")
    settings.save(root, settings.Settings("ru", "ru"))
    web_app.create_app(
        db_file=db_path(root),
        data_root=root,
        port=PORT,
        app_mode=True,
        access_key=KEY,
        models=FakeStore(True),
    )
    assert i18n.ui_lang() == "ru"


def test_broken_settings_file_falls_back(tmp_path: Path) -> None:
    settings.settings_path(tmp_path).write_text("{not json")
    assert settings.load(tmp_path) == settings.Settings()
    settings.settings_path(tmp_path).write_text('{"ui_lang": "xx", "recording_lang": "ru"}')
    assert settings.load(tmp_path) == settings.Settings(None, "ru")


def test_concurrent_settings_updates_keep_every_field(tmp_path: Path) -> None:
    def tour() -> None:
        for _ in range(50):
            settings.update(tmp_path, tour_seen=True)

    def langs() -> None:
        for i in range(50):
            settings.update(tmp_path, ui_lang="ru" if i % 2 else "en", recording_lang="ru")

    threads = [threading.Thread(target=f) for f in (tour, langs, tour, langs)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    final = settings.load(tmp_path)
    assert final.tour_seen and final.recording_lang == "ru" and final.ui_lang in {"ru", "en"}
    assert stat.S_IMODE(settings.settings_path(tmp_path).stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_failed_settings_write_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(settings.os, "replace", boom)
    with pytest.raises(OSError):
        settings.save(tmp_path, settings.Settings("ru", "ru"))
    assert list(tmp_path.iterdir()) == []


# --- adding by path ------------------------------------------------------------------


def test_enqueue_by_path_makes_no_copy(
    ready_env: AppEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = tmp_path / "Синк по релизу.mp4"
    rec.write_bytes(b"synthetic")
    other = tmp_path / "notes.txt"
    other.write_bytes(b"x")

    def probe(path: Path) -> float:
        if path.suffix == ".txt":
            raise AudioError("probe_failed")
        return 12.0

    monkeypatch.setattr(web_app, "probe_duration", probe)
    response = ready_env.post(
        "/enqueue",
        {"path": [str(rec), str(other), "relative.mp3"], "language": "ru"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["ok"] and results[0]["title"] == "Синк по релизу"
    assert results[1] == {"ok": False, "code": "probe_failed", "error": results[1]["error"]}
    assert results[2]["code"] == "bad_path"
    assert response.headers["HX-Trigger"] == "queue-changed"
    assert ready_env.meetings() == [(repo.SOURCE_PATH, str(rec))]
    uploads = ready_env.root / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir())


def test_enqueue_rejects_bad_language_and_empty(ready_env: AppEnv) -> None:
    assert ready_env.post("/enqueue", {"path": "/a.mp3", "language": "de"}).status_code == 400
    assert ready_env.post("/enqueue", {"language": "en"}).status_code == 400


def test_enqueue_reports_unreadable(ready_env: AppEnv, tmp_path: Path) -> None:
    response = ready_env.post("/enqueue", {"path": str(tmp_path / "gone.mp3"), "language": "en"})
    assert response.json()["results"][0]["code"] == "file_unreadable"
    assert "HX-Trigger" not in response.headers


# --- export name ---------------------------------------------------------------------


def _done_meeting(root: Path, title: str) -> int:
    from transcribator.models import Segment, Transcript

    conn = db.connect(db_path(root))
    try:
        meeting_id = repo.enqueue(
            conn, title=title, recorded_at=None, source_kind="path", source_ref="/s/a.mp4"
        )
        job = repo.claim_next(conn)
        assert job is not None
        repo.complete(conn, job, Transcript([Segment(0, 1, "S1", "Привет")], duration_s=1.0))
        return meeting_id
    finally:
        conn.close()


def test_markdown_is_named_after_the_meeting_in_the_app(ready_env: AppEnv) -> None:
    meeting_id = _done_meeting(ready_env.root, 'Синк: релиз/v2 "итог"')
    response = ready_env.client.get(f"/meetings/{meeting_id}.md")
    disposition = response.headers["content-disposition"]
    assert f'filename="meeting-{meeting_id}.md"' in disposition
    assert "filename*=UTF-8''" in disposition
    from urllib.parse import unquote

    assert unquote(disposition.split("UTF-8''")[1]) == "Синк релиз v2 итог.md"


def test_download_name_cleans_titles() -> None:
    assert web_app.download_name("a/b\\c:d\x00e", 3) == "a b c d e.md"
    assert web_app.download_name(" .. ", 4) == "meeting-4.md"
    assert len(web_app.download_name("x" * 500, 1)) == 123


# --- serve --app ---------------------------------------------------------------------


def test_access_key_must_be_hex() -> None:
    assert serve.read_access_key(io.StringIO(KEY + "\n")) == KEY
    for bad in ("", "short\n", KEY.upper(), "zz" * 32):
        with pytest.raises(serve.BadAccessKey):
            serve.read_access_key(io.StringIO(bad))


def test_listen_socket_port_zero_is_local() -> None:
    sock = serve.listen_socket(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0
    finally:
        sock.close()


class _FakeKeeper:
    instances: list[_FakeKeeper] = []

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = cmd
        self.stopped = False
        _FakeKeeper.instances.append(self)

    def stop(self) -> None:
        self.stopped = True


def test_app_server_prints_one_ready_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    monkeypatch.setenv(DATA_ENV, str(tmp_path / "data"))
    # The service sets this for its children; monkeypatch puts the old value back after.
    monkeypatch.setenv(MODELS_ENV, "placeholder")
    monkeypatch.setattr(serve, "_WorkerKeeper", _FakeKeeper)
    seen: dict[str, Any] = {}

    def fake_run(self: uvicorn.Server, sockets: list[socket.socket]) -> None:
        seen["port"] = self.config.port
        seen["host"] = sockets[0].getsockname()[0]
        seen["access_log"] = self.config.access_log
        seen["app"] = self.config.app

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
    _FakeKeeper.instances.clear()

    assert main(["serve", "--app", "--port", "0"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1
    ready = json.loads(out[0])
    assert ready == {"ready": True, "port": seen["port"]} and seen["port"] > 0
    assert seen["host"] == "127.0.0.1" and seen["access_log"] is False
    assert os.environ[MODELS_ENV] == str(tmp_path / "data/models")
    keeper = _FakeKeeper.instances[0]
    assert keeper.stopped and keeper.cmd[-3:-1] == ["worker", "--parent-pid"]


def test_app_server_without_key_is_a_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DATA_ENV, str(tmp_path / "data"))
    monkeypatch.setenv(MODELS_ENV, "placeholder")
    monkeypatch.setattr(serve, "_WorkerKeeper", _FakeKeeper)
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
    assert main(["serve", "--app", "--port", "0"]) == APP_EXIT_CONFIG


def test_second_app_server_reports_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = ensure_private_dir(tmp_path / "data")
    monkeypatch.setenv(DATA_ENV, str(data))
    monkeypatch.setenv(MODELS_ENV, "placeholder")
    held = serve._lock(data)
    try:
        monkeypatch.setattr(sys, "stdin", io.StringIO(KEY + "\n"))
        assert main(["serve", "--app", "--port", "0"]) == APP_EXIT_ALREADY_RUNNING
    finally:
        held.close()


def test_port_zero_only_in_app_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DATA_ENV, str(tmp_path / "data"))
    assert main(["serve", "--port", "0"]) == 2


def test_parent_watch_sends_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    raised = threading.Event()
    monkeypatch.setattr(serve.signal, "raise_signal", lambda sig: raised.set())
    serve.watch_parent(-1, poll_s=0.01)  # getppid() is never -1: the parent is "gone"
    assert raised.wait(5)


def test_children_get_the_same_interpreter_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    class Flags:
        isolated = 1
        dont_write_bytecode = 1

    monkeypatch.setattr(sys, "flags", Flags)
    assert python_command("worker") == [sys.executable, "-I", "-B", "-m", "transcribator", "worker"]
    assert serve.worker_command()[1:3] == ["-I", "-B"]


# --- AVFoundation helper -------------------------------------------------------------


def _fake_tool(tmp_path: Path, stdout: str, code: int = 0) -> Path:
    tool = tmp_path / "transcribator-audio"
    tool.write_text(f"#!/bin/sh\necho '{stdout}'\nexit {code}\n")
    tool.chmod(0o755)
    return tool


def test_audio_tool_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(audio.SANDBOXED_ENV, "1")
    monkeypatch.setenv(audio.AUDIO_TOOL_ENV, str(_fake_tool(tmp_path, '{"duration": 35.7}')))
    assert audio.probe_duration(tmp_path / "x") == 35.7


@pytest.mark.parametrize(
    ("stdout", "code", "expected"),
    [
        ('{"error": "no_audio_stream"}', 1, "no_audio_stream"),
        ('{"error": "something/with/a/path"}', 1, "probe_failed"),
        ("garbage", 1, "probe_failed"),
        ('{"duration": 0}', 0, "empty_audio"),
        ('{"other": 1}', 0, "probe_bad_output"),
    ],
)
def test_audio_tool_probe_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, code: int, expected: str
) -> None:
    monkeypatch.setenv(audio.SANDBOXED_ENV, "1")
    monkeypatch.setenv(audio.AUDIO_TOOL_ENV, str(_fake_tool(tmp_path, stdout, code)))
    with pytest.raises(AudioError) as exc:
        audio.probe_duration(tmp_path / "x")
    assert exc.value.code == expected


def test_audio_tool_convert_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(audio.SANDBOXED_ENV, "1")
    monkeypatch.setenv(audio.AUDIO_TOOL_ENV, str(_fake_tool(tmp_path, '{"error": "x"}', 1)))
    with pytest.raises(AudioError) as exc:
        audio.normalize_to_wav(tmp_path / "a", tmp_path / "b.wav")
    assert exc.value.code == "normalize_failed"
    monkeypatch.setenv(audio.AUDIO_TOOL_ENV, str(tmp_path / "missing"))
    with pytest.raises(AudioError) as exc:
        audio.probe_duration(tmp_path / "a")
    assert exc.value.code == "audio_tool_missing"


def test_audio_tool_runs_without_network_unless_already_sandboxed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio.sys, "platform", "darwin")
    monkeypatch.delenv(audio.SANDBOXED_ENV, raising=False)
    cmd = audio._tool_command("/t", "probe", "/f")
    assert cmd[:2] == [audio.SANDBOX_EXEC, "-p"]
    profile = cmd[2]
    assert profile.startswith(audio.NO_NETWORK_PROFILE) and cmd[3:] == ["/t", "probe", "/f"]
    assert '(allow process-exec (literal "/t"))' in profile
    assert "(deny file-write*)" in profile and "nsurlsessiond" in profile
    tricky = audio.helper_profile('/a "b"\\c', Path("/w"))
    assert '(literal "/a \\"b\\"\\\\c")' in tricky
    monkeypatch.setenv(audio.SANDBOXED_ENV, "1")
    assert audio._tool_command("/t", "probe", "/f") == ["/t", "probe", "/f"]


def test_isolated_child_is_marked_sandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    from transcribator.pipeline import isolated

    seen: dict[str, Any] = {}

    class FakePopen:
        pid = 0
        returncode = 0

        def __init__(self, cmd: list[str], **kw: Any) -> None:
            seen.update(kw["env"])

        def communicate(self, timeout: float) -> tuple[bytes, bytes]:
            return b"", b""

    monkeypatch.setattr(isolated, "wrappers", lambda **kw: ["/usr/bin/true"])
    monkeypatch.setattr(isolated.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(isolated, "stop_group", lambda proc: None)
    isolated.run_isolated(["x"], timeout_s=1)
    assert seen[audio.SANDBOXED_ENV] == "1"


# --- models in the app's data directory ----------------------------------------------


def test_fluidaudio_uses_app_models_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MODELS_ENV, raising=False)
    assert fluidaudio.asr_model_dir() == fluidaudio.DEFAULT_ASR_MODEL_DIR
    assert fluidaudio.cli_env() is None
    monkeypatch.setenv(MODELS_ENV, str(tmp_path / "models"))
    assert fluidaudio.asr_model_dir() == tmp_path / "models/parakeet-tdt-0.6b-v3"
    env = fluidaudio.cli_env()
    assert env is not None and env["CFFIXED_USER_HOME"] == str(tmp_path / "models/home")
    seen: list[str] = []
    transcriber = fluidaudio.FluidAudioTranscriber(run=lambda args: seen.extend(args))
    with pytest.raises(Exception):  # noqa: B017 - no JSON written by the fake runner
        transcriber.transcribe(tmp_path / "audio.wav", [])
    assert seen[seen.index("--model-dir") + 1] == str(tmp_path / "models/parakeet-tdt-0.6b-v3")


# --- worker --------------------------------------------------------------------------


def test_worker_waits_for_models(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "data")
    conn = db.connect(db_path(root))
    repo.enqueue(conn, title="t", recorded_at=None, source_kind="path", source_ref="/s/a")
    conn.close()
    ready = {"value": False}
    worker = Worker(
        root, keep_awake=False, models_ready=lambda: ready["value"], recognition_lock=None
    )
    try:
        assert worker.run_once() is False
        ready["value"] = True
        assert worker.run_once() is True  # claimed (fails: the source is missing)
    finally:
        worker.close()


def test_recognition_lock_is_shared(tmp_path: Path) -> None:
    import fcntl

    root = ensure_private_dir(tmp_path / "data")
    conn = db.connect(db_path(root))
    repo.enqueue(conn, title="t", recorded_at=None, source_kind="path", source_ref="/s/a")
    conn.close()
    lock = tmp_path / "locks/recognition.lock"
    lock.parent.mkdir()
    worker = Worker(root, keep_awake=False, recognition_lock=lock)
    other = lock.open("a")
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # "another installation" works
        assert worker.run_once() is False
        conn = db.connect(db_path(root))
        assert queries_status(conn) == "queued"  # not claimed while waiting
        conn.close()
        fcntl.flock(other, fcntl.LOCK_UN)
        assert worker.run_once() is True
    finally:
        other.close()
        worker.close()


def queries_status(conn: Any) -> str:
    return str(conn.execute("SELECT status FROM meeting").fetchone()[0])


def test_worker_has_no_shared_lock_by_default(tmp_path: Path) -> None:
    worker = Worker(ensure_private_dir(tmp_path / "d"), keep_awake=False)
    try:
        assert worker._recognition_lock is None  # tests never touch the user's real lock
    finally:
        worker.close()


def test_data_dir_with_only_models_is_ours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from transcribator.paths import data_dir

    root = tmp_path / "LocTranscriptor"
    (root / "models").mkdir(parents=True)
    settings.save(root, settings.Settings("ru", "ru"))
    monkeypatch.setenv(DATA_ENV, str(root))
    assert data_dir() == root


def test_enqueue_path_with_nul_byte_is_an_error_not_a_crash(ready_env: AppEnv) -> None:
    response = ready_env.post("/enqueue", {"path": "/a\x00b.mp3", "language": "en"})
    assert response.status_code == 200
    assert response.json()["results"][0]["code"] == "file_unreadable"


def test_sandbox_marker_is_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    from transcribator.pipeline import isolated

    env = isolated.child_env({"PATH": "/bin", audio.SANDBOXED_ENV: "1"})
    assert audio.SANDBOXED_ENV not in env


def test_download_name_drops_format_characters() -> None:
    assert web_app.download_name("abc‮gpj.exe", 1) == "abcgpj.exe.md"


def test_osascript_picker_is_off_in_the_app(ready_env: AppEnv) -> None:
    assert ready_env.post("/pick").status_code == 404


# --- first-run tips ------------------------------------------------------------------


def test_tour_seen_setting_roundtrip_and_old_files(tmp_path: Path) -> None:
    settings.save(tmp_path, settings.Settings("ru", "en", tour_seen=True))
    assert settings.load(tmp_path) == settings.Settings("ru", "en", True)
    stored = json.loads(settings.settings_path(tmp_path).read_text())
    assert stored == {"ui_lang": "ru", "recording_lang": "en", "tour_seen": True}
    # A file written before the flag existed: the tips haven't been seen yet.
    settings.settings_path(tmp_path).write_text('{"ui_lang": "ru", "recording_lang": "ru"}')
    assert settings.load(tmp_path) == settings.Settings("ru", "ru", tour_seen=False)
    # Only a real true counts.
    settings.settings_path(tmp_path).write_text('{"tour_seen": "yes"}')
    assert settings.load(tmp_path).tour_seen is False


def test_saving_settings_keeps_tour_seen(ready_env: AppEnv) -> None:
    assert ready_env.post("/tour/seen").status_code == 204
    response = ready_env.post("/settings", {"ui_lang": "ru", "recording_lang": "en"})
    assert response.status_code == 200
    assert settings.load(ready_env.root) == settings.Settings("ru", "en", tour_seen=True)


def test_tour_seen_is_remembered_and_stops_autostart(ready_env: AppEnv) -> None:
    page = ready_env.client.get("/").text
    assert 'id="tour"' in page and "data-autostart" in page and "data-remember" in page
    response = ready_env.post("/tour/seen")
    assert response.status_code == 204 and response.content == b""
    assert settings.load(ready_env.root).tour_seen is True
    path = settings.settings_path(ready_env.root)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    page = ready_env.client.get("/").text
    assert 'id="tour"' in page and "data-autostart" not in page
    # Repeating it changes nothing; languages chosen earlier are kept.
    assert ready_env.post("/tour/seen").status_code == 204
    assert settings.load(ready_env.root) == settings.Settings(tour_seen=True)


def test_tour_seen_keeps_language_choice(ready_env: AppEnv) -> None:
    ready_env.post("/settings", {"ui_lang": "ru", "recording_lang": "en"})
    ready_env.post("/tour/seen")
    assert settings.load(ready_env.root) == settings.Settings("ru", "en", tour_seen=True)


def test_tour_seen_needs_origin_and_csrf(ready_env: AppEnv) -> None:
    client = ready_env.client
    assert client.post("/tour/seen", headers={"Origin": BASE}).status_code == 403
    response = client.post(
        "/tour/seen", headers={"Origin": "http://evil.example", "X-CSRF-Token": ready_env.token}
    )
    assert response.status_code == 403
    assert client.post("/tour/seen", headers={"X-CSRF-Token": ready_env.token}).status_code == 403
    assert client.get("/tour/seen").status_code == 405
    anonymous = TestClient(ready_env.app, base_url=BASE)
    headers = {"Origin": BASE, "X-CSRF-Token": ready_env.token}
    assert anonymous.post("/tour/seen", headers=headers).status_code == 403
    assert settings.load(ready_env.root).tour_seen is False


def test_tour_seen_only_in_app_mode(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "d")
    app = web_app.create_app(db_file=db_path(root), data_root=root, port=PORT)
    client = TestClient(app, base_url=BASE)
    headers = {"Origin": BASE, "X-CSRF-Token": app.state.csrf_token}
    assert client.post("/tour/seen", headers=headers).status_code == 404
    assert not settings.settings_path(root).exists()
    # The browser version has the tips (Help-style replay) but never starts them itself.
    page = client.get("/").text
    assert 'id="tour"' in page
    assert "data-autostart" not in page and "data-remember" not in page
