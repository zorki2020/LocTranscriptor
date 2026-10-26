"""Recordings made in the app: the migration, `layout` in the queue, `GET /api/csrf` and
`POST /api/recordings`.

Synthetic data only: the "recordings" are a few bytes of header plus zeros.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_app_mode import BASE, PORT, AppEnv, FakeStore
from transcribator import i18n
from transcribator.i18n import RECORDING_LANG_ENV, UI_LANG_ENV
from transcribator.paths import data_dir, db_path, ensure_private_dir, uploads_dir
from transcribator.storage import db, repo
from transcribator.web import app as web_app
from transcribator.web import recordings

REF = "rec" + "0123456789abcdef01234567" + ".wav"
OTHER_REF = "rec" + "fedcba9876543210fedcba98" + ".wav"


@pytest.fixture(autouse=True)
def _clean_languages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(UI_LANG_ENV, raising=False)
    monkeypatch.delenv(RECORDING_LANG_ENV, raising=False)
    i18n.set_preferences(ui_lang=None, recording_lang=None)
    yield
    i18n.set_preferences(ui_lang=None, recording_lang=None)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[AppEnv]:
    app_env = AppEnv(tmp_path / "data", FakeStore(ready=True))
    yield app_env
    app_env.client.close()


def make_wav(env: AppEnv, name: str = REF, size: int = 1000) -> Path:
    path = uploads_dir(env.root) / name
    path.write_bytes(b"RIFF" + bytes(size - 4))
    path.chmod(0o600)
    return path


def body(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ref": REF,
        "title": None,
        "started_at": "2026-10-23T10:00:00Z",
        "language": "ru",
        "layout": "mono",
        "duration_s": 12.5,
    }
    data.update(changes)
    return {k: v for k, v in data.items() if v is not _MISSING}


_MISSING = object()


def send(
    env: AppEnv, payload: Any, *, raw: bytes | None = None, headers: dict[str, str] | None = None
) -> httpx.Response:
    sent = {
        "Origin": BASE,
        "X-CSRF-Token": env.token,
        "Content-Type": "application/json",
    }
    sent.update(headers or {})
    content = raw if raw is not None else json.dumps(payload).encode()
    return env.client.post("/api/recordings", content=content, headers=sent)


def set_status(env: AppEnv, status: str) -> None:
    conn = db.connect(db_path(env.root))
    try:
        conn.execute("UPDATE meeting SET status = ?", (status,))
    finally:
        conn.close()


def rows(env: AppEnv) -> list[sqlite3.Row]:
    conn = db.connect(db_path(env.root))
    try:
        return conn.execute("SELECT * FROM meeting").fetchall()
    finally:
        conn.close()


# --- storage -------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "t.sqlite3")
    yield connection
    connection.close()


def test_enqueue_defaults_to_mono_and_claim_returns_layout(conn: sqlite3.Connection) -> None:
    repo.enqueue(conn, title="a", recorded_at=None, source_kind="upload", source_ref="u1")
    repo.enqueue(
        conn,
        title="b",
        recorded_at=None,
        source_kind="upload",
        source_ref="u2",
        layout="mic_system",
    )
    first, second = repo.claim_next(conn), repo.claim_next(conn)
    assert first is not None and second is not None
    assert (first.layout, second.layout) == ("mono", "mic_system")


def test_enqueue_rejects_unknown_layout(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        repo.enqueue(
            conn, title="a", recorded_at=None, source_kind="upload", source_ref="u", layout="stereo"
        )
    assert conn.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 0


def test_job_layout_defaults_to_mono() -> None:
    assert repo.Job(id=1, meeting_id=1, source_kind="path", source_ref="x", attempts=1).layout == (
        "mono"
    )


def test_database_without_layout_migrates_on_open(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path, isolation_level=None)
    for script in db._MIGRATIONS[:2]:  # the schema before `layout` existed
        for statement in db._split(script):
            old.execute(statement)
    old.execute("PRAGMA user_version = 2")
    old.execute(
        "INSERT INTO meeting (title, added_at, status, source_kind, source_ref)"
        " VALUES ('old', '2026-01-01T00:00:00+00:00', 'queued', 'upload', 'u')"
    )
    old.execute(
        "INSERT INTO job (meeting_id, state, created_at, updated_at)"
        " VALUES (1, 'queued', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    old.close()

    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT layout FROM meeting").fetchone()[0] == "mono"
        job = repo.claim_next(conn)
        assert job is not None and job.layout == "mono"
        repo.enqueue(
            conn,
            title="new",
            recorded_at=None,
            source_kind="upload",
            source_ref="v",
            layout="mic_system",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE meeting SET layout = 'bogus'")
    finally:
        conn.close()


# --- GET /api/csrf -------------------------------------------------------------------


def test_csrf_endpoint_returns_the_token(env: AppEnv) -> None:
    response = env.client.get("/api/csrf")
    assert response.status_code == 200
    assert response.json() == {"csrf": env.token}
    assert response.headers["cache-control"] == "no-store"


def test_csrf_endpoint_needs_the_key_cookie(env: AppEnv) -> None:
    anonymous = TestClient(env.app, base_url=BASE)
    assert anonymous.get("/api/csrf").status_code == 403
    anonymous.cookies.set("tb_key", "cd" * 32, domain="127.0.0.1")
    assert anonymous.get("/api/csrf").status_code == 403


def test_csrf_endpoint_rejects_foreign_host(env: AppEnv) -> None:
    assert env.client.get("/api/csrf", headers={"Host": "evil.example"}).status_code == 403


def test_csrf_endpoint_is_hidden_outside_app_mode(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "data")
    app = web_app.create_app(db_file=db_path(root), data_root=root, port=PORT)
    client = TestClient(app, base_url=BASE)
    assert client.get("/api/csrf").status_code == 404


# --- POST /api/recordings: security --------------------------------------------------


def test_missing_key_cookie_is_refused(env: AppEnv) -> None:
    make_wav(env)
    anonymous = TestClient(env.app, base_url=BASE)
    response = anonymous.post(
        "/api/recordings",
        content=json.dumps(body()).encode(),
        headers={"Origin": BASE, "X-CSRF-Token": env.token, "Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert rows(env) == []


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:1"])
def test_wrong_origin_is_refused(env: AppEnv, origin: str) -> None:
    make_wav(env)
    assert send(env, body(), headers={"Origin": origin}).status_code == 403
    assert rows(env) == []


def test_missing_origin_is_refused(env: AppEnv) -> None:
    make_wav(env)
    response = env.client.post(
        "/api/recordings",
        content=json.dumps(body()).encode(),
        headers={"X-CSRF-Token": env.token, "Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert rows(env) == []


def test_missing_or_wrong_csrf_is_refused(env: AppEnv) -> None:
    make_wav(env)
    content = json.dumps(body()).encode()
    base = {"Origin": BASE, "Content-Type": "application/json"}
    assert env.client.post("/api/recordings", content=content, headers=base).status_code == 403
    wrong = {**base, "X-CSRF-Token": "x" * 43}
    assert env.client.post("/api/recordings", content=content, headers=wrong).status_code == 403
    assert rows(env) == []


def test_not_app_mode(tmp_path: Path) -> None:
    root = ensure_private_dir(tmp_path / "data")
    app = web_app.create_app(db_file=db_path(root), data_root=root, port=PORT)
    client = TestClient(app, base_url=BASE)
    (uploads_dir(root) / REF).write_bytes(bytes(100))
    response = client.post(
        "/api/recordings",
        json=body(),
        headers={"Origin": BASE, "X-CSRF-Token": app.state.csrf_token},
    )
    assert response.status_code == 404
    assert response.json() == {"error": "not_app_mode"}
    conn = db.connect(db_path(root))
    assert conn.execute("SELECT COUNT(*) FROM meeting").fetchone()[0] == 0
    conn.close()


# --- POST /api/recordings: validation ------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "../" + REF,
        "sub/" + REF,
        "/etc/" + REF,
        "..\\" + REF,
        REF.replace("rec", "REC"),
        "rec" + "0123456789ABCDEF01234567" + ".wav",
        "rec" + "0123456789abcdef0123456" + ".wav",  # 23 characters
        "rec" + "0123456789abcdef012345678" + ".wav",  # 25 characters
        "rec" + "0123456789abcdef01234567" + ".mp3",
        "rec" + "0123456789abcdef01234567",
        "rec" + "0123456789abcdef01234567" + ".wav\n",
        "rec" + "0123456789abcdef01234567" + ".wav\x00",
        "0123456789abcdef0123456789abcdef",  # the browser upload shape
        "..",
        "",
        " " + REF,
        None,
        7,
        ["a"],
    ],
)
def test_bad_ref_is_refused(env: AppEnv, ref: object) -> None:
    make_wav(env)
    response = send(env, body(ref=ref))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_ref"}
    assert rows(env) == []


def test_missing_ref_is_refused(env: AppEnv) -> None:
    assert send(env, body(ref=_MISSING)).json() == {"error": "bad_ref"}


def test_symlink_is_refused(env: AppEnv, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(bytes(1000))
    os.symlink(target, uploads_dir(env.root) / REF)
    response = send(env, body())
    assert response.status_code == 400
    assert response.json() == {"error": "bad_ref"}
    assert rows(env) == []


def test_dangling_symlink_is_refused(env: AppEnv, tmp_path: Path) -> None:
    os.symlink(tmp_path / "nowhere", uploads_dir(env.root) / REF)
    assert send(env, body()).status_code in (400, 404)
    assert rows(env) == []


def test_directory_is_refused(env: AppEnv) -> None:
    (uploads_dir(env.root) / REF).mkdir()
    response = send(env, body())
    assert response.status_code == 400
    assert response.json() == {"error": "bad_ref"}


def test_named_pipe_is_refused_without_hanging(env: AppEnv) -> None:
    os.mkfifo(uploads_dir(env.root) / REF)
    assert send(env, body()).json() == {"error": "bad_ref"}


def test_missing_file(env: AppEnv) -> None:
    response = send(env, body())
    assert response.status_code == 404
    assert response.json() == {"error": "file_missing"}
    assert rows(env) == []


@pytest.mark.parametrize("size", [0, 10, 44])
def test_file_without_audio_is_refused(env: AppEnv, size: int) -> None:
    path = uploads_dir(env.root) / REF
    path.write_bytes(bytes(size))
    assert send(env, body()).json() == {"error": "file_missing"}
    assert path.exists()  # the file is not ours to delete here
    assert rows(env) == []


def test_smallest_accepted_file(env: AppEnv) -> None:
    make_wav(env, size=45)
    assert send(env, body()).status_code == 200


@pytest.mark.parametrize("language", ["de", "RU", " ru", "", None, 5])
def test_bad_language(env: AppEnv, language: object) -> None:
    make_wav(env)
    response = send(env, body(language=language))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_language"}
    assert rows(env) == []


def test_missing_language(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(language=_MISSING)).json() == {"error": "bad_language"}


@pytest.mark.parametrize("layout", ["stereo", "MONO", "", None, 1])
def test_bad_layout(env: AppEnv, layout: object) -> None:
    make_wav(env)
    response = send(env, body(layout=layout))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_layout"}
    assert rows(env) == []


def test_missing_layout(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(layout=_MISSING)).json() == {"error": "bad_layout"}


@pytest.mark.parametrize(
    "changes",
    [
        {"title": 5},
        {"title": ["x"]},
        {"started_at": "yesterday"},
        {"started_at": "2026-10-23T10:00:00"},  # no UTC offset
        {"started_at": 1761213600},
        {"started_at": "2026-10-23T10:00:00Z" + "0" * 50},
    ],
)
def test_bad_body_fields(env: AppEnv, changes: dict[str, Any]) -> None:
    make_wav(env)
    response = send(env, body(**changes))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_body"}
    assert rows(env) == []


def test_nan_in_the_body_is_refused(env: AppEnv) -> None:
    make_wav(env)
    raw = f'{{"ref": "{REF}", "language": "ru", "layout": "mono", "duration_s": NaN}}'
    assert send(env, None, raw=raw.encode()).json() == {"error": "bad_body"}


@pytest.mark.parametrize("duration", [-1, "x", None, True, _MISSING])
def test_duration_is_accepted_and_ignored(env: AppEnv, duration: object) -> None:
    make_wav(env)
    assert send(env, body(duration_s=duration)).status_code == 200
    assert rows(env)[0]["duration_s"] is None


@pytest.mark.parametrize(
    "raw",
    [b"", b"{", b"null", b"[]", b'"x"', b"7", b"\xff\xfe", b"[" * 3000],
)
def test_empty_and_malformed_bodies(env: AppEnv, raw: bytes) -> None:
    make_wav(env)
    response = send(env, None, raw=raw)
    assert response.status_code == 400
    assert response.json() == {"error": "bad_body"}
    assert rows(env) == []


def test_wrong_content_type(env: AppEnv) -> None:
    make_wav(env)
    response = send(env, body(), headers={"Content-Type": "text/plain"})
    assert response.status_code == 400
    assert response.json() == {"error": "bad_body"}
    assert rows(env) == []


def test_huge_body_is_refused(env: AppEnv) -> None:
    make_wav(env)
    raw = json.dumps(body(title="x" * (recordings.MAX_BODY_BYTES * 4))).encode()
    response = send(env, None, raw=raw)
    assert response.status_code == 413
    assert response.json() == {"error": "bad_body"}
    assert rows(env) == []


def test_huge_body_without_length_is_refused(env: AppEnv) -> None:
    make_wav(env)

    def chunks() -> Iterator[bytes]:
        for _ in range(100):
            yield b" " * 1024

    response = env.client.post(
        "/api/recordings",
        content=chunks(),
        headers={
            "Origin": BASE,
            "X-CSRF-Token": env.token,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert rows(env) == []


def test_errors_do_not_echo_input(env: AppEnv) -> None:
    marker = "Секретная встреча"
    response = send(env, body(ref="../" + marker, title=marker))
    assert marker not in response.text
    assert response.json() == {"error": "bad_ref"}


# --- POST /api/recordings: success ---------------------------------------------------


def test_success_queues_a_mono_meeting(env: AppEnv) -> None:
    path = make_wav(env)
    response = send(env, body(title="  Планёрка   команды \n"))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    meeting_id = response.json()["meeting_id"]
    [row] = rows(env)
    assert row["id"] == meeting_id
    assert row["title"] == "Планёрка команды"
    assert row["status"] == "queued"
    assert row["source_kind"] == "upload"
    assert row["source_ref"] == REF
    assert row["language"] == "ru"
    assert row["layout"] == "mono"
    assert row["recorded_at"] == "2026-10-23T10:00:00+00:00"
    assert path.exists()  # not copied, not moved: the worker deletes it after success
    conn = db.connect(db_path(env.root))
    try:
        job = repo.claim_next(conn)
    finally:
        conn.close()
    assert job is not None
    assert (job.source_ref, job.layout, job.language) == (REF, "mono", "ru")


def test_success_with_mic_system_layout(env: AppEnv) -> None:
    make_wav(env)
    response = send(env, body(layout="mic_system", language="en", started_at=None))
    assert response.status_code == 200
    [row] = rows(env)
    assert row["layout"] == "mic_system"
    assert row["language"] == "en"
    assert row["recorded_at"] is None
    conn = db.connect(db_path(env.root))
    try:
        job = repo.claim_next(conn)
    finally:
        conn.close()
    assert job is not None and job.layout == "mic_system"


def test_started_at_with_offset_is_stored_in_utc(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(started_at="2026-10-23T13:00:00+03:00")).status_code == 200
    assert rows(env)[0]["recorded_at"] == "2026-10-23T10:00:00+00:00"


@pytest.mark.parametrize("title", [None, "", "   \n\t "])
def test_empty_title_gets_a_default_in_the_ui_language(env: AppEnv, title: str | None) -> None:
    make_wav(env)
    i18n.set_preferences(ui_lang="ru", recording_lang=None)
    assert send(env, body(title=title)).status_code == 200
    assert re.fullmatch(r"Запись \d\d\.\d\d\.\d{4} \d\d:\d\d", rows(env)[0]["title"])


def test_default_title_in_english_without_started_at(env: AppEnv) -> None:
    make_wav(env)
    i18n.set_preferences(ui_lang="en", recording_lang=None)
    assert send(env, body(started_at=None)).status_code == 200
    assert re.fullmatch(r"Recording \d\d\.\d\d\.\d{4} \d\d:\d\d", rows(env)[0]["title"])


def test_long_title_is_cut_and_control_characters_dropped(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(title="a\x00b\u202e" + "x" * 1000)).status_code == 200
    title = rows(env)[0]["title"]
    assert len(title) == recordings.TITLE_LIMIT
    assert title.startswith("abxxx")


def test_repeated_request_queues_the_file_once(env: AppEnv) -> None:
    make_wav(env)
    first = send(env, body())
    second = send(env, body())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(rows(env)) == 1


def test_retry_after_the_worker_consumed_the_file_returns_the_meeting(env: AppEnv) -> None:
    path = make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    path.unlink()  # the worker finished and deleted the file
    set_status(env, "done")
    response = send(env, body())
    assert response.status_code == 200
    assert response.json() == {"meeting_id": meeting_id}
    assert len(rows(env)) == 1


@pytest.mark.parametrize("status", ["queued", "processing"])
def test_repeat_while_active_keeps_the_file(env: AppEnv, status: str) -> None:
    path = make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    set_status(env, status)
    assert send(env, body()).json() == {"meeting_id": meeting_id}
    assert path.exists()


def test_repeat_of_a_processed_recording_removes_the_surplus_file(env: AppEnv) -> None:
    path = make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    set_status(env, "done")
    assert path.exists()
    assert send(env, body()).json() == {"meeting_id": meeting_id}
    assert not path.exists()
    assert len(rows(env)) == 1


def test_failed_meeting_keeps_its_file_for_retry(env: AppEnv) -> None:
    path = make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    set_status(env, "error")
    assert send(env, body()).json() == {"meeting_id": meeting_id}
    assert path.exists()


def test_surplus_symlink_is_removed_without_touching_its_target(
    env: AppEnv, tmp_path: Path
) -> None:
    make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    set_status(env, "done")
    link = uploads_dir(env.root) / REF
    link.unlink()
    target = tmp_path / "elsewhere"
    target.write_bytes(bytes(100))
    os.symlink(target, link)
    assert send(env, body()).json() == {"meeting_id": meeting_id}
    assert not link.is_symlink()
    assert target.exists()


def test_parallel_requests_for_one_file_make_one_meeting(env: AppEnv) -> None:
    make_wav(env)
    results: list[int] = []

    def call() -> None:
        results.append(send(env, body()).status_code)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [200] * 8
    assert len(rows(env)) == 1


def test_enqueue_upload_once_is_atomic_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "t.sqlite3"
    ids: list[int] = []

    def call() -> None:
        connection = db.connect(path)
        try:
            ids.append(
                repo.enqueue_upload_once(
                    connection,
                    title="a",
                    recorded_at=None,
                    source_ref=REF,
                    language="en",
                    layout="mono",
                )
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(ids)) == 1 and len(ids) == 8


def test_recordings_folder_is_an_own_entry_of_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    (root / "recordings").mkdir(parents=True)
    monkeypatch.setenv("TRANSCRIBATOR_DATA_DIR", str(root))
    assert data_dir() == root  # not "foreign": no DataDirError without a database


def test_audio_folder_is_an_own_entry_of_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    (root / "audio").mkdir(parents=True)
    monkeypatch.setenv("TRANSCRIBATOR_DATA_DIR", str(root))
    assert data_dir() == root  # not "foreign": no DataDirError without a database


def test_two_recordings_make_two_meetings(env: AppEnv) -> None:
    make_wav(env)
    make_wav(env, OTHER_REF)
    ids = {send(env, body(ref=ref)).json()["meeting_id"] for ref in (REF, OTHER_REF)}
    assert len(ids) == 2


def test_unknown_fields_are_ignored(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(extra="x", duration_s=0)).status_code == 200


def test_deleting_a_recording_meeting_removes_its_file(env: AppEnv) -> None:
    path = make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    response = env.post(f"/meetings/{meeting_id}/delete")
    assert response.status_code == 200
    assert not path.exists()
    assert rows(env) == []
