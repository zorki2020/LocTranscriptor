"""Web interface on synthetic data: a temporary database, a temporary data folder, a
generated tone instead of a recording. The interface is English by default; Russian is
checked with `TRANSCRIBATOR_UI_LANG=ru`."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import requires_ffmpeg
from transcribator.i18n import RECORDING_LANG_ENV, UI_LANG_ENV
from transcribator.models import Segment, Transcript
from transcribator.paths import db_path, ensure_private_dir, uploads_dir
from transcribator.storage import db, repo
from transcribator.web import app as web_app
from transcribator.web import display, strings
from transcribator.web.display import from_local_input
from transcribator.web.security import CSP

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


class Env:
    def __init__(self, root: Path, picker: Callable[[], Path | None] | None = None) -> None:
        self.root = ensure_private_dir(root)
        self.db_file = db_path(self.root)
        self.app = web_app.create_app(
            db_file=self.db_file, data_root=self.root, port=PORT, picker=picker
        )
        self.client = TestClient(self.app, base_url=BASE, follow_redirects=False)
        self.token: str = self.app.state.csrf_token

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Origin": BASE, "X-CSRF-Token": self.token, **extra}

    def post(self, url: str, data: dict[str, str] | None = None, **extra: str) -> httpx.Response:
        return self.client.post(url, data=data, headers=self.headers(**extra))

    def conn(self) -> sqlite3.Connection:
        return db.connect(self.db_file)

    def add_done(
        self,
        title: str = "Синк по релизу",
        segments: list[Segment] | None = None,
        *,
        source_kind: str = "path",
        source_ref: str = "/synthetic/a.mp4",
        recorded_at: str | None = "2026-10-09T12:00:00+00:00",
        duration_s: float = 6.0,
        language: str = "ru",
    ) -> int:
        segments = segments or [
            Segment(0.0, 2.0, "S1", "Привет, давайте обсудим Ёлку и миграцию."),
            Segment(2.5, 4.0, "S2", "Да, МИГРАЦИЯ в среду."),
            Segment(4.5, 6.0, "S1", "Отлично."),
        ]
        conn = self.conn()
        try:
            meeting_id = repo.enqueue(
                conn,
                title=title,
                recorded_at=recorded_at,
                source_kind=source_kind,
                source_ref=source_ref,
                language=language,
            )
            job = repo.claim_next(conn)
            assert job is not None and job.meeting_id == meeting_id
            repo.complete(conn, job, Transcript(segments, duration_s=duration_s))
        finally:
            conn.close()
        return meeting_id

    def add_error(self, code: str = "asr_failed") -> int:
        conn = self.conn()
        try:
            meeting_id = repo.enqueue(
                conn, title="Сломанная", recorded_at=None, source_kind="path", source_ref="/s/b"
            )
            job = repo.claim_next(conn)
            assert job is not None
            repo.fail(conn, job, code)
        finally:
            conn.close()
        return meeting_id

    def language(self, meeting_id: int) -> str:
        conn = self.conn()
        try:
            row = conn.execute("SELECT language FROM meeting WHERE id = ?", (meeting_id,))
            return str(row.fetchone()["language"])
        finally:
            conn.close()

    def set_legacy_summary(self, meeting_id: int, lines: list[str], error: str) -> None:
        """Old summary columns are still in the schema. The web layer doesn't show them."""
        conn = self.conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE meeting SET summary = ?, summary_error = ? WHERE id = ?",
                    (json.dumps(lines, ensure_ascii=False), error, meeting_id),
                )
        finally:
            conn.close()

    def meeting(self, meeting_id: int) -> repo.Meeting | None:
        conn = self.conn()
        try:
            return repo.get_meeting(conn, meeting_id)
        finally:
            conn.close()

    def count(self, table: str) -> int:
        conn = self.conn()
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _english_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests don't depend on the user's shell: both languages start at the default."""
    monkeypatch.delenv(UI_LANG_ENV, raising=False)
    monkeypatch.delenv(RECORDING_LANG_ENV, raising=False)


@pytest.fixture
def ru(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(UI_LANG_ENV, "ru")


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path / "data", picker=lambda: None)
    yield environment
    environment.client.close()


# --- protection -----------------------------------------------------------------------


def test_host_header_must_be_local(env: Env) -> None:
    for host in ("evil.example", f"evil.example:{PORT}", "127.0.0.1:9999", "127.0.0.1"):
        response = env.client.get("/", headers={"Host": host})
        assert response.status_code == 403
        assert "evil" not in response.text
    assert env.client.get("/", headers={"Host": f"localhost:{PORT}"}).status_code == 200
    assert env.client.get("/").status_code == 200


def test_post_requires_local_origin(env: Env) -> None:
    token = {"X-CSRF-Token": env.token}
    assert env.client.post("/pick", headers=token).status_code == 403
    for origin in (
        "http://evil.example",
        "null",
        f"http://127.0.0.1:{PORT + 1}",
        "https://127.0.0.1:8765",
    ):
        assert env.client.post("/pick", headers={**token, "Origin": origin}).status_code == 403
    ok = env.client.post("/pick", headers={**token, "Origin": f"http://localhost:{PORT}"})
    assert ok.status_code == 200


def test_post_requires_csrf_token(env: Env) -> None:
    meeting_id = env.add_done()
    url = f"/meetings/{meeting_id}/meta"
    form = {"title": "Новое", "recorded_at": ""}
    assert env.client.post(url, data=form, headers=ORIGIN).status_code == 403
    wrong = {**ORIGIN, "X-CSRF-Token": "x" * 43}
    assert env.client.post(url, data=form, headers=wrong).status_code == 403
    assert env.client.post(url, data={**form, "csrf": "wrong"}, headers=ORIGIN).status_code == 403
    # Without the token neither uploads nor non-form requests get through.
    assert env.client.post("/upload", content=b"x", headers=ORIGIN).status_code == 403
    meeting = env.meeting(meeting_id)
    assert meeting is not None and meeting.title == "Синк по релизу"

    # The token in a form field works the same as in the header.
    ok = env.client.post(url, data={**form, "csrf": env.token}, headers=ORIGIN)
    assert ok.status_code == 303
    meeting = env.meeting(meeting_id)
    assert meeting is not None and meeting.title == "Новое"


def test_form_body_is_capped(env: Env) -> None:
    meeting_id = env.add_done()
    big = {"title": "x" * (70 * 1024), "csrf": env.token}
    response = env.client.post(f"/meetings/{meeting_id}/meta", data=big, headers=ORIGIN)
    assert response.status_code == 413
    response = env.post(f"/meetings/{meeting_id}/meta", {"title": "x" * (70 * 1024)})
    assert response.status_code == 413


def test_security_headers_everywhere(env: Env) -> None:
    meeting_id = env.add_done()
    responses = [
        env.client.get("/"),
        env.client.get("/meetings"),
        env.client.get(f"/meetings/{meeting_id}"),
        env.client.get(f"/meetings/{meeting_id}.md"),
        env.client.get("/static/app.js"),
        env.client.get("/no-such-page"),
        env.client.get("/", headers={"Host": "evil.example"}),
        env.client.post("/pick"),
    ]
    for response in responses:
        assert response.headers["content-security-policy"] == CSP
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert "access-control-allow-origin" not in response.headers
    for response in responses[:4]:
        assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in CSP and "script-src 'self'" in CSP


def test_no_cors_preflight(env: Env) -> None:
    response = env.client.options(
        "/upload",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_docs_and_openapi_disabled(env: Env) -> None:
    for url in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert env.client.get(url).status_code == 404


def test_pages_have_no_inline_scripts_or_styles(env: Env) -> None:
    meeting_id = env.add_done()
    for url in ("/", "/meetings", f"/meetings/{meeting_id}"):
        html = env.client.get(url).text
        assert "<style" not in html and "style=" not in html
        assert "hx-on" not in html and "onclick" not in html
        scripts = html.count("<script")
        assert scripts == html.count('<script src="/static/') == 2
        assert '<meta name="csrf-token" content="' + env.token + '">' in html
        assert '"allowEval":false' in html and '"allowScriptTags":false' in html
        assert "http://" not in html and "https://" not in html


def test_unhandled_error_is_generic(tmp_path: Path) -> None:
    def broken() -> Path | None:
        raise RuntimeError("/Users/secret/Совещание с клиентом.mp3")

    env = Env(tmp_path / "data", picker=broken)
    response = env.post("/pick")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert "Совещание" not in response.text
    assert response.headers["content-security-policy"] == CSP


def test_bad_path_params_are_generic(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    for url in ("/meetings/999", "/meetings/999.md", "/meetings/abc", "/meetings/" + "9" * 30):
        response = env.client.get(url)
        assert response.status_code == 404
        assert "Not found." in response.text
        assert "Traceback" not in response.text
    monkeypatch.setenv(UI_LANG_ENV, "ru")
    assert "Не найдено." in env.client.get("/meetings/999").text


def test_middleware_refusals_follow_ui_language(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert env.client.get("/", headers={"Host": "evil.example"}).text == "Request refused."
    monkeypatch.setenv(UI_LANG_ENV, "ru")
    assert env.client.get("/", headers={"Host": "evil.example"}).text == "Запрос отклонён."


# --- escaping -------------------------------------------------------------------------


def test_untrusted_text_is_escaped(env: Env) -> None:
    payload = "<script>alert(1)</script>"
    meeting_id = env.add_done(
        title='<img src=x onerror="alert(2)">',
        segments=[
            Segment(0.0, 1.0, "S1", f"Скажи {payload} и alert снова"),
            Segment(1.0, 2.0, "<b>S2</b>", "ответ"),
        ],
    )
    conn = env.conn()
    repo.rename_speaker(conn, meeting_id, "S1", "<i>Иван</i>")
    conn.close()

    pages = [
        env.client.get(f"/meetings/{meeting_id}").text,
        env.client.get("/meetings").text,
        env.client.get("/queue").text,
        env.client.get("/").text,
        env.client.get("/meetings", params={"q": "alert"}).text,
        env.client.get("/meetings", params={"q": "<script>"}).text,
    ]
    for html in pages:
        assert "<script>alert" not in html
        assert "<img src=x" not in html
        assert "<i>Иван" not in html and "<b>S2" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in pages[0]
    assert "&lt;i&gt;Иван&lt;/i&gt;" in pages[0]
    assert "&lt;img src=x" in pages[1]
    # Highlighting adds only <mark>, everything else is escaped.
    assert (
        "&lt;script&gt;<mark>alert</mark>(1)&lt;/script&gt; и <mark>alert</mark> снова" in pages[4]
    )
    assert "&lt;<mark>script</mark>&gt;alert(1)&lt;/<mark>script</mark>&gt;" in pages[5]


# --- upload ---------------------------------------------------------------------------


def _upload_headers(env: Env, name: str = "Синк по релизу.mp3", **extra: str) -> dict[str, str]:
    return env.headers(**{"X-Filename": quote(name, safe=""), **extra})


@requires_ffmpeg
def test_upload_streams_private_file_and_queues(env: Env, sine_audio: Path) -> None:
    mtime_ms = 1_760_000_000_000
    response = env.client.post(
        "/upload",
        content=sine_audio.read_bytes(),
        headers=_upload_headers(
            env, "../../etc/Синк по релизу.mp3", **{"X-File-Mtime": str(mtime_ms)}
        ),
    )
    assert response.status_code == 200
    meeting_id = response.json()["id"]

    files = list(uploads_dir(env.root).iterdir())
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert files[0].read_bytes() == sine_audio.read_bytes()
    assert web_app.UPLOAD_REF.fullmatch(files[0].name)

    conn = env.conn()
    row = conn.execute(
        "SELECT title, recorded_at, status, source_kind, source_ref FROM meeting WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    conn.close()
    assert row["title"] == "Синк по релизу"
    assert row["status"] == "queued"
    assert row["source_kind"] == "upload"
    assert row["source_ref"] == files[0].name
    assert row["recorded_at"] == datetime.fromtimestamp(mtime_ms / 1000, UTC).isoformat(
        timespec="seconds"
    )
    assert "Синк по релизу" in env.client.get("/queue").text
    assert "Queued" in env.client.get("/queue").text
    # No language header: the default recording language.
    assert env.language(meeting_id) == "en"


@requires_ffmpeg
def test_upload_of_non_audio_is_deleted(env: Env) -> None:
    response = env.client.post(
        "/upload", content=b"this is not audio\n" * 100, headers=_upload_headers(env, "x.mp3")
    )
    assert response.status_code == 400
    assert response.json()["code"] in {"probe_failed", "no_audio_stream", "probe_bad_output"}
    assert response.json()["code"] in response.json()["error"]
    assert list(uploads_dir(env.root).iterdir()) == []
    assert env.count("meeting") == 0


def test_upload_empty_body_is_rejected(env: Env) -> None:
    response = env.client.post("/upload", content=b"", headers=_upload_headers(env))
    assert response.status_code == 400
    assert list(uploads_dir(env.root).iterdir()) == []


def test_upload_size_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "MAX_UPLOAD_BYTES", 10)
    # With Content-Length: refused before the file is written.
    response = env.client.post("/upload", content=b"x" * 100, headers=_upload_headers(env))
    assert response.status_code == 413
    assert list(uploads_dir(env.root).iterdir()) == []

    # Without Content-Length (chunked): refused while writing, the partial file is deleted.
    def chunks() -> Iterator[bytes]:
        yield b"x" * 8
        yield b"x" * 8

    response = env.client.post("/upload", content=chunks(), headers=_upload_headers(env))
    assert response.status_code == 413
    assert list(uploads_dir(env.root).iterdir()) == []
    assert env.count("meeting") == 0


def _disk(free: int) -> SimpleNamespace:
    usage = SimpleNamespace(total=free, used=0, free=free)
    return SimpleNamespace(disk_usage=lambda path: usage)


def test_upload_refused_when_disk_is_low(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    body = b"x" * 1000
    # Less free space than the file size plus the 1 GB headroom.
    monkeypatch.setattr(web_app, "shutil", _disk(web_app.DISK_HEADROOM_BYTES + 999))
    response = env.client.post("/upload", content=body, headers=_upload_headers(env))
    assert response.status_code == 507
    assert response.json()["code"] == "no_space"
    assert "Not enough disk space" in response.json()["error"]
    assert list(uploads_dir(env.root).iterdir()) == []
    assert env.count("meeting") == 0

    monkeypatch.setattr(web_app, "shutil", _disk(web_app.DISK_HEADROOM_BYTES + 1000))
    response = env.client.post("/upload", content=body, headers=_upload_headers(env))
    assert response.status_code == 200
    files = list(uploads_dir(env.root).iterdir())
    assert len(files) == 1 and files[0].read_bytes() == body


def test_upload_writes_large_body_in_batches(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    monkeypatch.setattr(web_app, "_WRITE_BATCH", 10)

    def chunks() -> Iterator[bytes]:
        for i in range(7):
            yield bytes([65 + i]) * 4

    response = env.client.post("/upload", content=chunks(), headers=_upload_headers(env))
    assert response.status_code == 200
    (copy,) = uploads_dir(env.root).iterdir()
    assert copy.read_bytes() == b"".join(bytes([65 + i]) * 4 for i in range(7))


def test_upload_bad_metadata_headers_fall_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    response = env.client.post(
        "/upload",
        content=b"fake",
        headers=env.headers(**{"X-Filename": "%FF%FE", "X-File-Mtime": "-5"}),
    )
    assert response.status_code == 200
    meeting = env.meeting(response.json()["id"])
    assert meeting is not None
    assert meeting.title == "Untitled"
    assert meeting.recorded_at is None


# --- file picker ----------------------------------------------------------------------


@requires_ffmpeg
def test_pick_enqueues_path_with_file_date(tmp_path: Path, sine_audio: Path) -> None:
    os.utime(sine_audio, (1_700_000_000, 1_700_000_000))
    env = Env(tmp_path / "data", picker=lambda: sine_audio)
    response = env.post("/pick")
    assert response.status_code == 200
    assert "Added to the queue: tone" in response.text
    assert response.headers["hx-trigger"] == "queue-changed"

    conn = env.conn()
    row = conn.execute("SELECT title, recorded_at, source_kind, source_ref FROM meeting").fetchone()
    conn.close()
    assert row["title"] == "tone"
    assert row["source_kind"] == "path"
    assert row["source_ref"] == str(sine_audio)
    assert row["recorded_at"] == "2023-11-14T22:13:20+00:00"
    assert list(uploads_dir(env.root).iterdir()) == []  # a path, not a copy


def test_pick_cancel(env: Env) -> None:
    response = env.post("/pick")
    assert response.status_code == 200
    assert "No file chosen." in response.text
    assert env.count("meeting") == 0


@requires_ffmpeg
def test_pick_rejects_non_audio_and_missing(tmp_path: Path) -> None:
    text = tmp_path / "Секретная встреча.mp3"
    text.write_text("not audio")
    env = Env(tmp_path / "data", picker=lambda: text)
    response = env.post("/pick")
    assert response.status_code == 400
    assert "This file won&#39;t work." in response.text
    assert "Секретная" not in response.text

    for target in (tmp_path / "missing.mp3", tmp_path, Path("relative.mp3")):
        env = Env(tmp_path / "data", picker=lambda target=target: target)  # type: ignore[misc]
        response = env.post("/pick")
        assert response.status_code == 400
        assert "missing" not in response.text
    assert env.count("meeting") == 0


# --- archive, search, edits -----------------------------------------------------------


def test_list_shows_meetings(env: Env) -> None:
    first = env.add_done("Первая", recorded_at="2026-10-01T09:00:00+00:00")
    second = env.add_done("Вторая", recorded_at="2026-10-05T09:00:00+00:00")
    html = env.client.get("/meetings").text
    assert html.index("Вторая") < html.index("Первая")
    assert f'href="/meetings/{first}"' in html and f'href="/meetings/{second}"' in html
    assert "6 s" in html
    assert "2 speakers" in html
    assert "Oct 5, 2026" in html  # title of the date: full form with the year


def test_list_in_russian(env: Env, ru: None) -> None:
    env.add_done("Первая", recorded_at="2026-10-01T09:00:00+00:00")
    env.add_done("Вторая", recorded_at="2026-10-05T09:00:00+00:00")
    html = env.client.get("/meetings").text
    assert "6 с" in html
    assert "2 спикера" in html
    assert "2 встречи" in html
    assert "октября 2026" in html


def test_search_ignores_case_and_yo(env: Env) -> None:
    meeting_id = env.add_done()
    html = env.client.get("/meetings", params={"q": "елк"}).text
    assert f'href="/meetings/{meeting_id}#seg-0"' in html
    assert "<mark>Ёлку</mark>" in html
    assert '<span class="ts num">00:00</span>' in html
    assert '<span class="spk spk-1">Speaker 1</span>' in html

    html = env.client.get("/meetings", params={"q": "миграц"}).text
    assert "#seg-0" in html and "#seg-1" in html
    assert "<mark>МИГРАЦИЯ</mark>" in html
    assert '<span class="spk spk-2">Speaker 2</span> Да, <mark>МИГРАЦИЯ</mark> в среду.' in html
    assert "2 matches in 1 meeting" in html

    html = env.client.get("/meetings", params={"q": "несуществующее"}).text
    assert "Nothing found for “несуществующее”" in html
    assert env.client.get("/meetings", params={"q": 'NEAR( "x" *'}).status_code == 200


def test_meeting_page_format(env: Env) -> None:
    meeting_id = env.add_done()
    html = env.client.get(f"/meetings/{meeting_id}").text
    assert (
        '<p class="seg turn" id="seg-0"><a class="ts num" href="#seg-0">00:00</a>'
        '<span class="seg-text"><span class="spk spk-1">Speaker 1'
        '<span class="visually-hidden">:</span></span>'
        " Привет, давайте обсудим Ёлку и миграцию.</span></p>" in html
    )
    assert '<p class="seg turn" id="seg-2"><a class="ts num" href="#seg-2">00:04</a>' in html
    assert f'href="/meetings/{meeting_id}.md"' in html
    # Deletion is confirmed in its own dialog; the request goes through htmx with the token.
    assert '<dialog id="delete-dialog"' in html and "hx-confirm" not in html
    assert f'hx-post="/meetings/{meeting_id}/delete"' in html
    assert '<button type="button" class="btn" data-close autofocus>Cancel</button>' in html
    # Recording language in the metadata, its name in the interface language.
    assert '<span title="Recording language">Russian</span>' in html
    assert '<h2 class="section-title" id="transcript-title">Transcript</h2>' in html


def test_no_summary_anywhere(env: Env) -> None:
    """There is no summary: not on the meeting page, in the archive or in the export, even
    if something is left in the old columns."""
    meeting_id = env.add_done()
    env.set_legacy_summary(meeting_id, ["Релиз в среду."], "summary_unavailable")
    pages = {
        "meeting": env.client.get(f"/meetings/{meeting_id}").text,
        "list": env.client.get("/meetings").text,
        "export": env.client.get(f"/meetings/{meeting_id}.md").text,
    }
    for name, text in pages.items():
        assert "Кратко" not in text and "Summary" not in text, name
        assert "Релиз в среду." not in text, name
        assert "summary" not in text, name
        assert "ыжимк" not in text, name
    assert "Отлично." in pages["meeting"] and "Отлично." in pages["export"]
    assert "Синк по релизу" in pages["list"]


def test_rename_speaker_in_page_and_export(env: Env) -> None:
    meeting_id = env.add_done()
    response = env.post(f"/meetings/{meeting_id}/speakers", {"label": "S1", "name": "  Иван  "})
    assert response.status_code == 303
    html = env.client.get(f"/meetings/{meeting_id}").text
    assert html.count('<span class="spk spk-1">Иван<') == 2
    assert html.count('<span class="spk spk-2">Speaker 2<') == 1
    assert '<span class="dot" aria-hidden="true"></span>Иван</button>' in html

    export = env.client.get(f"/meetings/{meeting_id}.md")
    assert export.status_code == 200
    assert export.headers["content-type"] == "text/markdown; charset=utf-8"
    assert (
        export.headers["content-disposition"] == f'attachment; filename="meeting-{meeting_id}.md"'
    )
    assert export.text.startswith("# Синк по релизу\n")
    assert "Speakers: Иван, Speaker 2" in export.text
    assert "## Transcript" in export.text
    assert "**[00:00:00] Иван:** Привет, давайте обсудим Ёлку и миграцию." in export.text
    assert "## Кратко" not in export.text and "## Summary" not in export.text

    # Search shows the new name.
    assert (
        '<span class="spk spk-1">Иван</span>'
        in env.client.get("/meetings", params={"q": "елку"}).text
    )

    bad = env.post(f"/meetings/{meeting_id}/speakers", {"label": "NOPE", "name": "Пётр"})
    assert bad.status_code == 400
    # An empty name brings back "Speaker N".
    env.post(f"/meetings/{meeting_id}/speakers", {"label": "S1", "name": ""})
    assert '<span class="spk spk-1">Speaker 1<' in env.client.get(f"/meetings/{meeting_id}").text


def test_export_and_speakers_in_russian(env: Env, ru: None) -> None:
    meeting_id = env.add_done()
    export = env.client.get(f"/meetings/{meeting_id}.md").text
    assert "Спикеры: Спикер 1, Спикер 2" in export
    assert "## Транскрипт" in export and "## Transcript" not in export
    assert "**[00:00:02] Спикер 2:** Да, МИГРАЦИЯ в среду." in export
    html = env.client.get(f"/meetings/{meeting_id}").text
    assert html.count('<span class="spk spk-1">Спикер 1<') == 2
    assert '<span title="Язык записи">Русский</span>' in html
    assert 'id="transcript-title">Транскрипт</h2>' in html
    assert "2 спикера" in html
    search = env.client.get("/meetings", params={"q": "миграц"}).text
    assert '<span class="spk spk-2">Спикер 2</span>' in search
    assert "2 фрагмента в 1 встрече" in search
    nothing = env.client.get("/meetings", params={"q": "несуществующее"}).text
    assert "Ничего не найдено по «несуществующее»" in nothing


def test_from_local_input_rejects_out_of_range() -> None:
    for edge in ("0001-01-01T00:00+05:00", "9999-12-31T23:59-05:00", "вчера"):
        with pytest.raises(ValueError):
            from_local_input(edge)
    assert from_local_input("  ") is None
    assert from_local_input("2026-10-09T15:00+03:00") == "2026-10-09T12:00:00+00:00"


def test_rename_rejects_duplicate_names(env: Env) -> None:
    meeting_id = env.add_done()
    url = f"/meetings/{meeting_id}/speakers"
    assert env.post(url, {"label": "S1", "name": "Иван"}).status_code == 303

    # Another speaker's name (ignoring case and spaces) and their "Speaker N".
    for name in ("Иван", "  иван ", "ИВАН"):
        response = env.post(url, {"label": "S2", "name": name})
        assert response.status_code == 400
        assert "Another speaker in this meeting already has this name." in response.text
    # "Speaker 1" is taken by S1's default label even after S1 is renamed, in either
    # interface language.
    assert env.post(url, {"label": "S2", "name": "Speaker 1"}).status_code == 400
    assert env.post(url, {"label": "S2", "name": "Спикер 1"}).status_code == 400
    meeting = env.meeting(meeting_id)
    assert meeting is not None and "S2" not in meeting.names

    # One's own name, one's own default label and a reset are allowed.
    assert env.post(url, {"label": "S1", "name": "Иван"}).status_code == 303
    assert env.post(url, {"label": "S2", "name": "Speaker 2"}).status_code == 303
    assert env.post(url, {"label": "S2", "name": "Спикер 2"}).status_code == 303
    assert env.post(url, {"label": "S2", "name": "Пётр"}).status_code == 303
    assert env.post(url, {"label": "S1", "name": ""}).status_code == 303
    # After the reset S1 is "Speaker 1" again and the name "Иван" is free.
    assert env.post(url, {"label": "S2", "name": "Иван"}).status_code == 303
    assert env.post(url, {"label": "S1", "name": "Иван"}).status_code == 400


def test_edit_meta(env: Env) -> None:
    meeting_id = env.add_done()
    response = env.post(
        f"/meetings/{meeting_id}/meta",
        {"title": "Планёрка", "recorded_at": "2026-10-09T15:00"},
        **{"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == f"/meetings/{meeting_id}"
    meeting = env.meeting(meeting_id)
    assert meeting is not None
    assert meeting.title == "Планёрка"
    assert meeting.recorded_at == from_local_input("2026-10-09T15:00")
    assert meeting.recorded_at is not None and meeting.recorded_at.endswith("+00:00")
    assert 'value="2026-10-09T15:00"' in env.client.get(f"/meetings/{meeting_id}").text

    assert env.post(f"/meetings/{meeting_id}/meta", {"title": "  "}).status_code == 400
    bad_date = env.post(f"/meetings/{meeting_id}/meta", {"title": "X", "recorded_at": "вчера"})
    assert bad_date.status_code == 400
    assert env.post("/meetings/999/meta", {"title": "X"}).status_code == 404

    # Dates at the edge of the range don't convert to UTC: 400, not 500.
    for edge in ("0001-01-01T00:00+05:00", "9999-12-31T23:59-05:00"):
        response = env.post(f"/meetings/{meeting_id}/meta", {"title": "X", "recorded_at": edge})
        assert response.status_code == 400
        assert "Couldn&#39;t read the date." in response.text
    meeting = env.meeting(meeting_id)
    assert meeting is not None and meeting.title == "Планёрка"

    cleared = env.post(f"/meetings/{meeting_id}/meta", {"title": "Планёрка", "recorded_at": ""})
    assert cleared.status_code == 303
    meeting = env.meeting(meeting_id)
    assert meeting is not None and meeting.recorded_at is None


def test_queue_statuses_and_retry(env: Env) -> None:
    done = env.add_done("Готовая")
    failed = env.add_error("asr_failed")
    conn = env.conn()
    processing = repo.enqueue(
        conn, title="Идёт", recorded_at=None, source_kind="path", source_ref="/a"
    )
    job = repo.claim_next(conn)
    assert job is not None and job.meeting_id == processing
    repo.set_stage(conn, job.id, "transcribe")
    queued = repo.enqueue(conn, title="Ждёт", recorded_at=None, source_kind="path", source_ref="/b")
    conn.close()

    html = env.client.get("/queue").text
    assert '<span class="chip chip-error">Failed</span>' in html
    assert "Code: asr_failed" in html
    assert '<span class="chip chip-processing">Recognizing speech</span>' in html
    assert "less than a minute so far" in html
    assert "Done" in html and f'href="/meetings/{done}"' in html
    assert '<abbr class="lang-code" title="Recording language: Russian">RU</abbr>' in html
    assert "6 s · <abbr" in html
    assert f'hx-post="/meetings/{failed}/retry"' in html
    assert f'hx-post="/meetings/{processing}/retry"' not in html
    page = env.client.get(f"/meetings/{processing}").text
    assert "Recognizing speech" in page and "Delete meeting" not in page

    response = env.post(f"/meetings/{failed}/retry", **{"HX-Request": "true"})
    assert response.status_code == 204
    assert response.headers["hx-trigger"] == "queue-changed"
    meeting = env.meeting(failed)
    assert meeting is not None and meeting.status == "queued"
    assert env.post(f"/meetings/{failed}/retry").status_code == 409
    assert env.post(f"/meetings/{processing}/retry").status_code == 409
    assert (
        '<span class="chip chip-queued">Queued</span>' in env.client.get(f"/meetings/{queued}").text
    )


def test_retry_from_meeting_page_redirects_back(env: Env) -> None:
    meeting_id = env.add_error("probe_failed")
    html = env.client.get(f"/meetings/{meeting_id}").text
    assert "Code: probe_failed" in html and "Retry processing" in html
    assert 'id="transcript-title"' not in html
    response = env.post(f"/meetings/{meeting_id}/retry?back=meeting")
    assert response.status_code == 303
    assert response.headers["location"] == f"/meetings/{meeting_id}"


# --- deletion -------------------------------------------------------------------------


def test_delete_removes_rows_and_upload_copy(env: Env) -> None:
    ref = "0123456789abcdef0123456789abcdef"
    copy = uploads_dir(env.root) / ref
    copy.write_bytes(b"synthetic")
    meeting_id = env.add_done(source_kind="upload", source_ref=ref)
    other = env.add_done("Другая")

    response = env.post(f"/meetings/{meeting_id}/delete", **{"HX-Request": "true"})
    assert response.status_code == 200
    assert response.headers["hx-redirect"] == "/meetings"
    assert env.meeting(meeting_id) is None
    assert not copy.exists()
    assert env.count("meeting") == 1
    assert env.count("segment") == 3  # only the other meeting's segments are left
    assert env.count("job") == 1
    hits = env.client.get("/meetings", params={"q": "елку"}).text
    assert f"/meetings/{meeting_id}#" not in hits and f"/meetings/{other}#seg-0" in hits
    assert env.client.get(f"/meetings/{meeting_id}").status_code == 404
    assert env.meeting(other) is not None

    plain = env.post(f"/meetings/{other}/delete")
    assert plain.status_code == 303
    assert plain.headers["location"] == "/meetings"
    assert env.post(f"/meetings/{other}/delete").status_code == 404


@pytest.mark.parametrize("ref", ["../victim", "/abs/victim", "0123456789ABCDEF0123456789ABCDEF"])
def test_delete_refuses_traversal_refs(env: Env, ref: str) -> None:
    victim = env.root / "victim"
    victim.write_text("keep")
    upper = uploads_dir(env.root) / "0123456789ABCDEF0123456789ABCDEF"
    upper.write_text("keep")
    meeting_id = env.add_done(source_kind="upload", source_ref=ref)
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 303
    assert env.meeting(meeting_id) is None
    assert victim.exists() and upper.exists()


def test_delete_refused_while_processing(env: Env) -> None:
    conn = env.conn()
    meeting_id = repo.enqueue(
        conn, title="Идёт", recorded_at=None, source_kind="path", source_ref="/a"
    )
    repo.claim_next(conn)
    conn.close()
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 409
    assert env.meeting(meeting_id) is not None


def test_export_only_for_done_meetings(env: Env) -> None:
    meeting_id = env.add_error()
    assert env.client.get(f"/meetings/{meeting_id}.md").status_code == 404


# --- page look ------------------------------------------------------------------------


def test_queue_polls_only_while_active(env: Env) -> None:
    html = env.client.get("/queue").text
    assert 'id="queue"' in html and 'hx-trigger="queue-changed from:body"' in html
    assert "Recordings you add and their progress will show up here." in html
    env.add_done()
    assert 'hx-trigger="queue-changed from:body"' in env.client.get("/queue").text
    conn = env.conn()
    repo.enqueue(conn, title="Ждёт", recorded_at=None, source_kind="path", source_ref="/b")
    conn.close()
    html = env.client.get("/queue").text
    assert 'hx-trigger="every 3s, queue-changed from:body"' in html
    assert 'hx-swap="outerHTML"' in html
    assert 'hx-trigger="every 3s, queue-changed from:body"' in env.client.get("/").text


def test_long_transcript_is_chunked_with_stable_speaker_colors(env: Env) -> None:
    labels = ("B", "A", "C")
    segments = [
        Segment(i * 13.0, i * 13.0 + 12.0, labels[(i // 2) % 3], f"Реплика {i}.")
        for i in range(300)
    ]
    meeting_id = env.add_done("Длинная", segments, duration_s=3900.0)
    html = env.client.get(f"/meetings/{meeting_id}").text
    assert html.count('<div class="chunk">') == 8  # 40 paragraphs each
    assert html.count('<p class="seg') == 300
    # Color by order of first appearance: B first, A second, C third.
    assert '<li class="speaker spk-1">' in html and "Speaker 1</button>" in html
    assert html.count('<span class="spk spk-1">Speaker 1<') == 100
    assert html.count('<span class="spk spk-2">Speaker 2<') == 100
    assert html.count('<span class="spk spk-3">Speaker 3<') == 100
    assert 'placeholder="Speaker 1"' in html
    # The name is large only at the start of a turn: the speaker changes every two segments.
    assert html.count('class="seg turn"') == 150
    assert 'href="#seg-299">1:04:47</a>' in html  # over an hour: H:MM:SS
    assert 'href="#seg-0">0:00:00</a>' in html


def test_archive_empty_and_search_counts(env: Env) -> None:
    html = env.client.get("/meetings").text
    assert "No meetings yet" in html and 'href="/"' in html
    assert 'id="q"' not in html
    first = env.add_done("Первая")
    env.add_done("Вторая")
    html = env.client.get("/meetings").text
    assert "2 meetings" in html and 'id="q"' in html
    assert 'hx-trigger="input changed delay:250ms, search"' in html
    assert 'hx-sync="this:replace"' in html
    html = env.client.get("/meetings", params={"q": "миграц"}).text
    assert "4 matches in 2 meetings" in html
    assert html.count('class="hit-group"') == 2
    assert f'href="/meetings/{first}#seg-1"' in html


def test_display_helpers() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert strings.plural("speakers", 1) == "1 speaker"
    assert strings.plural("speakers", 3) == "3 speakers"
    assert strings.plural("hits", 2) == "2 matches"
    assert display.human_duration(None) == ""
    assert display.human_duration(6.4) == "6 s"
    assert display.human_duration(45 * 60 + 10) == "45 min"
    assert display.human_duration(3600 + 5 * 60) == "1 h 05 min"
    assert display.clock(64.9) == "01:04"
    assert display.clock(64.9, hours=True) == "0:01:04"
    assert display.clock(3725) == "1:02:05"
    assert display.elapsed("2026-10-10T11:59:30+00:00", now) == "less than a minute"
    assert display.elapsed("2026-10-10T11:56:00+00:00", now) == "4 min"
    assert display.elapsed("2026-10-10T10:50:00+00:00", now) == "1 h 10 min"
    assert display.when(None, now) == ""
    assert display.when("2026-10-09T11:00:00+00:00", now).startswith("yesterday, ")
    assert display.short_date("2025-03-01T12:00:00+00:00", now) == "Mar 1, 2025"
    assert re.fullmatch(r"Oct 9, \d\d:\d\d", display.short_date("2026-10-09T12:00:00+00:00", now))
    assert re.fullmatch(r"Oct 9, 2026, \d\d:\d\d", display.human_date("2026-10-09T12:00:00+00:00"))
    assert display.short_date(None, now) == "no date"
    assert display.human_date(None) == "date not set"
    classes = display.speaker_classes([f"S{i}" for i in range(10)])
    assert classes["S0"] == "spk-1" and classes["S7"] == "spk-8" and classes["S8"] == "spk-1"
    assert display.status_label("processing", "transcribe") == "Recognizing speech"
    assert display.status_label("processing", None) == "Processing"
    # There is no summary stage any more: an old stage code shows as generic processing.
    assert display.status_label("processing", "summarize") == "Processing"
    assert display.status_label("queued") == "Queued"
    assert "Put the file back" in display.error_text("source_missing")
    assert display.error_text("crash_X").startswith("Processing stopped")
    assert display.file_error("probe_failed").endswith("Code: probe_failed.")
    assert display.title_from_filename("dir/ \t") == "Untitled"


def test_display_helpers_in_russian(ru: None) -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert strings.plural("speakers", 1) == "1 спикер"
    assert strings.plural("speakers", 3) == "3 спикера"
    assert strings.plural("speakers", 11) == "11 спикеров"
    assert strings.plural("meetings", 21) == "21 встреча"
    assert strings.plural("in_meetings", 1) == "1 встрече"
    assert display.human_duration(6.4) == "6 с"
    assert display.human_duration(45 * 60 + 10) == "45 мин"
    assert display.human_duration(3600 + 5 * 60) == "1 ч 05 мин"
    assert display.elapsed("2026-10-10T11:59:30+00:00", now) == "меньше минуты"
    assert display.elapsed("2026-10-10T10:50:00+00:00", now) == "1 ч 10 мин"
    assert display.when("2026-10-09T11:00:00+00:00", now).startswith("вчера, ")
    assert display.short_date("2025-03-01T12:00:00+00:00", now) == "1 марта 2025"
    assert re.fullmatch(
        r"9 октября 2026, \d\d:\d\d", display.human_date("2026-10-09T12:00:00+00:00")
    )
    assert display.short_date(None, now) == "без даты"
    assert display.status_label("processing", "transcribe") == "Распознавание речи"
    assert display.status_label("processing", None) == "Обработка"
    assert "Верните файл" in display.error_text("source_missing")
    assert display.error_text("crash_X").startswith("Обработка остановилась")
    assert display.file_error("probe_failed").startswith("Файл не подходит.")
    assert display.title_from_filename("dir/ \t") == "Без названия"


def test_every_string_has_both_languages() -> None:
    for key, variants in strings.STRINGS.items():
        assert set(variants) == {"en", "ru"}, key
        assert all(text.strip() for text in variants.values()), key
    for key, plurals in strings.PLURALS.items():
        assert len(plurals["en"]) == 2 and len(plurals["ru"]) == 3, key
    assert all(len(names) == 12 for names in strings.MONTHS.values())


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match is not None
    return match.group(1)


@pytest.mark.parametrize(
    ("lang", "titles"),
    [
        ("en", ("Upload", "Archive", "Archive", "Meeting", "Error", "Transcribator")),
        ("ru", ("Загрузка", "Архив", "Архив", "Встреча", "Ошибка", "Транскрибатор")),
    ],
)
def test_titles_and_history_hold_no_content(
    env: Env, monkeypatch: pytest.MonkeyPatch, lang: str, titles: tuple[str, ...]
) -> None:
    monkeypatch.setenv(UI_LANG_ENV, lang)
    private_title = "Секретный проект Альфа"
    meeting_id = env.add_done(private_title)
    urls = ("/", "/meetings", "/meetings?q=Ёлку", f"/meetings/{meeting_id}", "/meetings/999")
    app_name = titles[-1]
    for url, title in zip(urls, titles, strict=False):
        html = env.client.get(url).text
        assert _title(html) == f"{title} · {app_name}"
        assert private_title not in _title(html) and "Ёлку" not in _title(html)
        assert f'<html lang="{lang}" translate="no">' in html
        assert '<meta name="google" content="notranslate">' in html
        assert "hx-replace-url" not in html and "hx-push-url" not in html
    # Search works with a plain GET too; the URL doesn't change when searching via htmx.
    assert "<mark>Ёлку</mark>" in env.client.get("/meetings", params={"q": "Ёлку"}).text
    templates = Path(web_app.__file__).parent / "templates"
    for template in templates.glob("*.html"):
        text = template.read_text()
        assert "hx-replace-url" not in text and "hx-push-url" not in text, template.name


def test_meeting_status_polls_until_final(env: Env) -> None:
    conn = env.conn()
    meeting_id = repo.enqueue(
        conn, title="Ждёт", recorded_at=None, source_kind="path", source_ref="/a"
    )
    conn.close()
    status_url = f"/meetings/{meeting_id}/status"

    page = env.client.get(f"/meetings/{meeting_id}").text
    assert f'hx-get="{status_url}"' in page and "data-poll" in page
    assert 'hx-trigger="every 3s, queue-changed from:body"' in page

    fragment = env.client.get(status_url)
    assert fragment.status_code == 200
    assert "hx-refresh" not in fragment.headers
    assert '<span class="chip chip-queued">Queued</span>' in fragment.text
    assert f'hx-get="{status_url}"' in fragment.text
    assert "<title>" not in fragment.text

    conn = env.conn()
    job = repo.claim_next(conn)
    assert job is not None
    repo.set_stage(conn, job.id, "diarize")
    conn.close()
    fragment = env.client.get(status_url)
    assert "hx-refresh" not in fragment.headers
    assert "Separating speakers" in fragment.text and "less than a minute so far" in fragment.text

    conn = env.conn()
    repo.complete(conn, job, Transcript([Segment(0.0, 1.0, "S1", "Текст.")], 1.0))
    conn.close()
    fragment = env.client.get(status_url)
    assert fragment.status_code == 200
    assert fragment.headers["hx-refresh"] == "true"
    assert "data-poll" not in fragment.text
    page = env.client.get(f"/meetings/{meeting_id}").text
    assert status_url not in page and 'id="transcript-title"' in page

    failed = env.add_error("probe_failed")
    fragment = env.client.get(f"/meetings/{failed}/status")
    assert fragment.headers["hx-refresh"] == "true"
    assert f"/meetings/{failed}/status" not in env.client.get(f"/meetings/{failed}").text
    assert env.client.get("/meetings/999/status").status_code == 404


# --- /api/status ----------------------------------------------------------------------


def test_api_status_counts(env: Env) -> None:
    empty = env.client.get("/api/status")
    assert empty.status_code == 200
    assert empty.json() == {"queued": 0, "processing": 0, "done": 0, "error": 0}

    env.add_done("Готовая")
    env.add_done("Ещё готовая")
    env.add_error("asr_failed")
    conn = env.conn()
    repo.enqueue(conn, title="Идёт", recorded_at=None, source_kind="path", source_ref="/a")
    job = repo.claim_next(conn)
    assert job is not None
    for ref in ("/b", "/c", "/d"):
        repo.enqueue(conn, title="Ждёт", recorded_at=None, source_kind="path", source_ref=ref)
    conn.close()

    response = env.client.get("/api/status")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {"queued": 3, "processing": 1, "done": 2, "error": 1}
    assert set(body) == {"queued", "processing", "done", "error"}
    assert all(type(value) is int for value in body.values())
    # Counters only: no titles, paths or error codes.
    for leak in ("Готовая", "Ждёт", "Идёт", "/a", "/b", "asr_failed", "synthetic"):
        assert leak not in response.text


def test_api_status_requires_local_host(env: Env) -> None:
    env.add_done("Секретная встреча")
    for host in ("evil.example", f"evil.example:{PORT}", "127.0.0.1:9999"):
        response = env.client.get("/api/status", headers={"Host": host})
        assert response.status_code == 403
        assert "queued" not in response.text and "Секретная" not in response.text
    assert env.client.get("/api/status", headers={"Host": f"localhost:{PORT}"}).status_code == 200


# --- languages ------------------------------------------------------------------------


def _selected(html: str) -> list[str]:
    return re.findall(r'<option value="(\w+)" selected>', html)


@pytest.mark.parametrize(
    ("recording_env", "selected"), [(None, "en"), ("en", "en"), ("ru", "ru"), ("de", "en")]
)
def test_language_select_default(
    env: Env, monkeypatch: pytest.MonkeyPatch, recording_env: str | None, selected: str
) -> None:
    if recording_env is not None:
        monkeypatch.setenv(RECORDING_LANG_ENV, recording_env)
    html = env.client.get("/").text
    assert '<select id="language" class="select" name="language">' in html
    assert '<label for="language">Recording language</label>' in html
    assert _selected(html) == [selected]
    assert '<option value="en"' in html and '<option value="ru"' in html
    assert ">English</option>" in html and ">Russian</option>" in html
    # The picker button sends the select along.
    assert 'hx-post="/pick" hx-include="#language"' in html


def test_upload_page_in_russian(env: Env, ru: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RECORDING_LANG_ENV, "ru")
    html = env.client.get("/").text
    assert '<h1 class="page-title">Добавить запись</h1>' in html
    assert '<label for="language">Язык записи</label>' in html
    assert ">Английский</option>" in html and ">Русский</option>" in html
    assert _selected(html) == ["ru"]
    assert ">Выбрать файл</button>" in html
    assert 'data-msg-uploading-n="Загрузка {i} из {n}"' in html
    assert "Здесь появятся добавленные записи и ход их обработки." in html


def test_app_js_strings_come_from_data_attributes(env: Env) -> None:
    html = env.client.get("/").text
    for name in ("too-large", "queued", "untitled", "failed", "aborted", "uploading"):
        assert f"data-msg-{name}=" in html, name
    assert 'data-msg-queued="Added to the queue: {title}"' in html
    script = env.client.get("/static/app.js").text
    assert re.search(r"[А-Яа-яЁё]", script) is None
    assert 'xhr.setRequestHeader("X-Language", language.value)' in script


@pytest.mark.parametrize("language", ["en", "ru"])
def test_upload_stores_language(env: Env, monkeypatch: pytest.MonkeyPatch, language: str) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    response = env.client.post(
        "/upload", content=b"fake", headers=_upload_headers(env, **{"X-Language": language})
    )
    assert response.status_code == 200
    assert env.language(response.json()["id"]) == language


def test_upload_language_defaults_to_env(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    monkeypatch.setenv(RECORDING_LANG_ENV, "ru")
    response = env.client.post("/upload", content=b"fake", headers=_upload_headers(env))
    assert env.language(response.json()["id"]) == "ru"


@pytest.mark.parametrize("language", ["de", "english", "../ru", "ru;en"])
def test_upload_rejects_unknown_language(
    env: Env, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    response = env.client.post(
        "/upload", content=b"fake", headers=_upload_headers(env, **{"X-Language": language})
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_language"
    assert list(uploads_dir(env.root).iterdir()) == []
    assert env.count("meeting") == 0


def test_pick_stores_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(web_app, "probe_duration", lambda path: 2.0)
    source = tmp_path / "synthetic.m4a"
    source.write_bytes(b"synthetic")
    env = Env(tmp_path / "data", picker=lambda: source)
    for language in ("ru", "en"):
        response = env.post("/pick", {"language": language}, **{"HX-Request": "true"})
        assert response.status_code == 200
    # A bare POST (no form) takes the default recording language.
    monkeypatch.setenv(RECORDING_LANG_ENV, "ru")
    assert env.post("/pick").status_code == 200
    conn = env.conn()
    rows = conn.execute("SELECT language FROM meeting ORDER BY id").fetchall()
    conn.close()
    assert [row["language"] for row in rows] == ["ru", "en", "ru"]


def test_pick_rejects_unknown_language(tmp_path: Path) -> None:
    opened: list[bool] = []

    def picker() -> Path | None:
        opened.append(True)
        return None

    env = Env(tmp_path / "data", picker=picker)
    response = env.post("/pick", {"language": "de"}, **{"HX-Request": "true"})
    assert response.status_code == 400
    assert "This recording language isn&#39;t supported." in response.text
    assert opened == []  # refused before the Finder dialog opens
    assert env.count("meeting") == 0


def test_queue_shows_recording_language(env: Env) -> None:
    env.add_done("Английская", language="en")
    html = env.client.get("/queue").text
    assert '<abbr class="lang-code" title="Recording language: English">EN</abbr>' in html


_RUSSIAN_WORDS = ("Загрузка", "Архив", "Спикер", "Транскрипт", "Удалить", "Очередь", "Поиск")


def test_no_russian_ui_text_in_english_pages(env: Env) -> None:
    """Meeting content may be Russian; the interface around it may not."""
    meeting_id = env.add_done("Meeting", [Segment(0.0, 1.0, "S1", "Hello there.")])
    failed = env.add_error("probe_failed")
    urls = (
        "/",
        "/queue",
        "/meetings",
        "/meetings?q=hello",
        "/meetings?q=nothing",
        f"/meetings/{meeting_id}",
        f"/meetings/{failed}",
        f"/meetings/{failed}/status",
        "/meetings/999",
    )
    for url in urls:
        html = env.client.get(url).text.replace("Сломанная", "")
        assert re.search(r"[А-Яа-яЁё]", html) is None, url
    export = env.client.get(f"/meetings/{meeting_id}.md").text
    assert re.search(r"[А-Яа-яЁё]", export) is None
    for word in _RUSSIAN_WORDS:
        assert word not in env.client.get(f"/meetings/{meeting_id}").text


def test_every_string_has_same_placeholders_in_both_languages() -> None:
    import string  # noqa: PLC0415

    from transcribator.web.strings import STRINGS  # noqa: PLC0415

    def fields(text: str) -> set[str]:
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for key, variants in STRINGS.items():
        assert set(variants) == {"en", "ru"}, key
        assert fields(variants["en"]) == fields(variants["ru"]), key
