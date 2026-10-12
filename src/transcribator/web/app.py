"""Web interface: adding recordings, queue, archive, search, edits, export, deletion.

Listens on 127.0.0.1 only (started from `cli.py`). Protection against browser attacks
is in `security.py`. A database connection is opened per request: the worker runs as a
separate process, and WAL mode takes care of concurrent access.

State-changing requests go only through htmx or `app.js` (XMLHttpRequest): with
`Referrer-Policy: no-referrer` a plain HTML form submission arrives with `Origin: null`
and would be rejected by the Origin check.

All visible text comes from `strings.py` in the interface language
(`TRANSCRIBATOR_UI_LANG`). File names, titles, paths and search queries are not logged.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from transcribator.i18n import RECORDING_LANGS, UI_LANGS, default_recording_lang, ui_lang
from transcribator.paths import uploads_dir
from transcribator.pipeline.audio import AudioError, probe_duration
from transcribator.pipeline.format_md import render_markdown, speaker_labels
from transcribator.storage import db, repo
from transcribator.web import display, queries
from transcribator.web.picker import pick_file
from transcribator.web.security import FORM_CONTENT_TYPE, FORM_LIMIT_BYTES, LocalOnlyMiddleware
from transcribator.web.strings import language_name, plural, t

MAX_UPLOAD_BYTES = 8 * 1024**3
DISK_HEADROOM_BYTES = 1024**3
_WRITE_BATCH = 1024**2
UPLOAD_REF = re.compile(r"[0-9a-f]{32}")
_MAX_ID = 2**63 - 1
SEARCH_LIMIT = 50
_NAME_LIMIT = 300
_SPEAKER_NAME_LIMIT = 100
_HERE = Path(__file__).parent
_HTTP_STATUSES = frozenset({400, 403, 404, 405, 409, 413})
_FINAL = frozenset({"done", "error"})


class _UploadTooLarge(Exception):
    pass


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _http_text(status: int) -> str:
    return t(f"http_{status}" if status in _HTTP_STATUSES else "http_other")


def _upload_title(raw: str | None) -> str:
    if not raw:
        return t("untitled")
    try:
        name = unquote(raw[: _NAME_LIMIT * 12], errors="strict")
    except UnicodeDecodeError:
        return t("untitled")
    return display.title_from_filename(name[:_NAME_LIMIT])


def _upload_mtime(raw: str | None) -> str | None:
    if not raw or not raw.isdigit() or len(raw) > 15:
        return None
    try:
        stamp = datetime.fromtimestamp(int(raw) / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp.isoformat(timespec="seconds")


def _same_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _name_taken(meeting: repo.Meeting, label: str, name: str) -> bool:
    """The new name matches how another speaker of this meeting is shown now, or that
    speaker's default label "Speaker N" in any interface language (switching the
    language must not make two speakers look the same). An empty name (reset) is
    always allowed."""
    wanted = _same_name(" ".join(name.split())[:_SPEAKER_NAME_LIMIT])  # same as repo
    if not wanted:
        return False
    defaults = [speaker_labels(meeting.transcript, lang=lang) for lang in UI_LANGS]
    others = {label for labels in defaults for label in labels} | set(meeting.labels)
    for other in others - {label}:
        shown = [meeting.names.get(other), *(d.get(other, other) for d in defaults)]
        if any(value and _same_name(value) == wanted for value in shown):
            return True
    return False


def _recording_lang(value: str | None) -> str | None:
    """Recording language from a request: the default when absent, None when unknown."""
    if value is None or not value.strip():
        return default_recording_lang()
    value = value.strip().lower()
    return value if value in RECORDING_LANGS else None


def create_app(
    *,
    db_file: Path,
    data_root: Path,
    port: int,
    picker: Callable[[], Path | None] | None = None,
) -> FastAPI:
    choose = picker or pick_file
    csrf_token = secrets.token_urlsafe(32)
    env = Environment(
        loader=FileSystemLoader(_HERE / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["human_date"] = display.human_date
    env.filters["short_date"] = display.short_date
    env.filters["when"] = display.when
    env.filters["elapsed"] = display.elapsed
    env.filters["human_duration"] = display.human_duration
    env.filters["clock"] = display.clock
    env.filters["local_input"] = display.local_input_value
    env.globals["status_label"] = display.status_label
    env.globals["error_text"] = display.error_text
    env.globals["plural"] = plural
    env.globals["t"] = t
    env.globals["language_name"] = language_name
    env.globals["recording_langs"] = RECORDING_LANGS
    env.globals["csrf"] = csrf_token

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.csrf_token = csrf_token
    app.add_middleware(LocalOnlyMiddleware, port=port, csrf_token=csrf_token)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @contextmanager
    def open_db() -> Iterator[sqlite3.Connection]:
        conn = db.connect(db_file)
        try:
            yield conn
        finally:
            conn.close()

    def page(
        name: str, status: int = 200, headers: dict[str, str] | None = None, **ctx: Any
    ) -> HTMLResponse:
        ctx.setdefault("ui_lang", ui_lang())
        return HTMLResponse(env.get_template(name).render(**ctx), status, headers=headers)

    def message(
        request: Request, status: int, text: str, headers: dict[str, str] | None = None
    ) -> HTMLResponse:
        """A message: a fragment for htmx, otherwise a page of its own."""
        if _is_htmx(request):
            return page("_message.html", status, headers, text=text, error=status >= 400)
        return page("error.html", status, headers, text=text)

    def done(request: Request, url: str) -> Response:
        if _is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    def check_id(meeting_id: int) -> None:
        if meeting_id > _MAX_ID:
            raise StarletteHTTPException(404)

    async def read_form(request: Request) -> dict[str, str]:
        content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != FORM_CONTENT_TYPE:
            raise StarletteHTTPException(400)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > FORM_LIMIT_BYTES:
                raise StarletteHTTPException(413)
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=50)
        except (UnicodeDecodeError, ValueError):
            raise StarletteHTTPException(400) from None
        return {key: values[0] for key, values in fields.items()}

    # --- errors: generic text, no exception details ----------------------------------

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        status = exc.status_code
        return message(request, status, _http_text(status))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return message(request, 400, _http_text(400))

    # --- upload and queue ------------------------------------------------------------

    def queue_context() -> dict[str, Any]:
        """Queue for the upload page. While there are active jobs the block polls the
        server; when there are none, polling is off and it waits for `queue-changed`."""
        with open_db() as conn:
            items = repo.queue(conn)
            started = queries.stage_started(conn)
            languages = queries.meeting_languages(conn, (m.id for m in items))
        active = any(m.status in ("queued", "processing") for m in items)
        return {"items": items, "started": started, "active": active, "languages": languages}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return page(
            "index.html", nav="add", default_lang=default_recording_lang(), **queue_context()
        )

    @app.get("/queue", response_class=HTMLResponse)
    def queue_fragment() -> HTMLResponse:
        return page("_queue.html", **queue_context())

    @app.post("/pick", response_class=HTMLResponse)
    async def pick(request: Request) -> HTMLResponse:
        # htmx sends the language select as a form; a bare POST gets the default.
        form = await read_form(request) if request.headers.get("content-type") else {}
        language = _recording_lang(form.get("language"))
        if language is None:
            return page("_message.html", 400, text=t("bad_language"), error=True)
        return await run_in_threadpool(pick_and_enqueue, language)

    def pick_and_enqueue(language: str) -> HTMLResponse:
        path = choose()
        if path is None:
            return page("_message.html", text=t("no_file_chosen"), error=False)
        try:
            if not path.is_absolute():
                raise AudioError("bad_path")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise AudioError("not_a_file")
            probe_duration(path)
        except OSError:
            return page("_message.html", 400, text=t("file_unreadable"), error=True)
        except AudioError as exc:
            return page("_message.html", 400, text=display.file_error(exc.code), error=True)
        title = display.title_from_filename(path.name)
        recorded_at = datetime.fromtimestamp(info.st_mtime, UTC).isoformat(timespec="seconds")
        with open_db() as conn:
            repo.enqueue(
                conn,
                title=title,
                recorded_at=recorded_at,
                source_kind=repo.SOURCE_PATH,
                source_ref=str(path),
                language=language,
            )
        return page(
            "_message.html",
            headers={"HX-Trigger": "queue-changed"},
            text=t("queued_title", title=title),
            error=False,
        )

    @app.post("/upload")
    async def upload(request: Request) -> JSONResponse:
        """The request body is written to a file in 1 MB batches; disk writes, ffprobe
        and the database run in the thread pool so the event loop isn't blocked. The
        recording language comes in the `X-Language` header."""
        language = _recording_lang(request.headers.get("x-language"))
        if language is None:
            return JSONResponse({"error": t("bad_language"), "code": "bad_language"}, 400)
        limit = MAX_UPLOAD_BYTES
        length = request.headers.get("content-length", "")
        expected = int(length) if length.isdigit() else 0
        if expected > limit:
            return JSONResponse({"error": t("file_too_large")}, 413)
        folder = await run_in_threadpool(uploads_dir, data_root)
        free = (await run_in_threadpool(shutil.disk_usage, folder)).free
        if expected + DISK_HEADROOM_BYTES > free:
            return JSONResponse({"error": t("no_space"), "code": "no_space"}, 507)
        title = _upload_title(request.headers.get("x-filename"))
        recorded_at = _upload_mtime(request.headers.get("x-file-mtime"))
        ref = uuid.uuid4().hex
        target = folder / ref
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        kept = False
        try:
            size = 0
            with os.fdopen(fd, "wb") as out:
                pending = bytearray()
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise _UploadTooLarge
                    pending += chunk
                    if len(pending) >= _WRITE_BATCH:
                        await run_in_threadpool(out.write, bytes(pending))
                        pending.clear()
                if pending:
                    await run_in_threadpool(out.write, bytes(pending))
            if size == 0:
                return JSONResponse({"error": t("file_empty"), "code": "empty_file"}, 400)
            await run_in_threadpool(probe_duration, target)

            def save() -> int:
                with open_db() as conn:
                    return repo.enqueue(
                        conn,
                        title=title,
                        recorded_at=recorded_at,
                        source_kind=repo.SOURCE_UPLOAD,
                        source_ref=ref,
                        language=language,
                    )

            meeting_id = await run_in_threadpool(save)
            kept = True
            return JSONResponse({"id": meeting_id, "title": title})
        except _UploadTooLarge:
            return JSONResponse({"error": t("file_too_large")}, 413)
        except AudioError as exc:
            return JSONResponse({"error": display.file_error(exc.code), "code": exc.code}, 400)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                return JSONResponse({"error": t("no_space"), "code": "no_space"}, 507)
            raise
        finally:
            if not kept:
                target.unlink(missing_ok=True)

    # --- archive and search ----------------------------------------------------------

    @app.get("/meetings", response_class=HTMLResponse)
    def meetings(q: str = "") -> HTMLResponse:
        query = q.strip()[:200]
        lang = ui_lang()
        with open_db() as conn:
            if query:
                hits = repo.search(conn, query, limit=SEARCH_LIMIT)
                # Hits are grouped by meeting in order of first appearance.
                groups: dict[int, dict[str, Any]] = {}
                for hit in hits:
                    group = groups.get(hit.meeting_id)
                    if group is None:
                        names = queries.speaker_display_names(conn, hit.meeting_id, lang)
                        group = groups[hit.meeting_id] = {
                            "hit": hit,
                            "names": names,
                            "classes": display.speaker_classes(names),
                            "rows": [],
                        }
                    group["rows"].append(
                        {
                            "hit": hit,
                            "speaker": group["names"].get(hit.speaker_label, hit.speaker_label),
                            "spk": group["classes"].get(hit.speaker_label, ""),
                            "text": display.highlight(hit.text, query),
                        }
                    )
                return page(
                    "meetings.html",
                    q=query,
                    groups=list(groups.values()),
                    hit_count=len(hits),
                    hit_limit=SEARCH_LIMIT,
                    items=[],
                    nav="list",
                )
            items = repo.list_meetings(conn, limit=1000)
        return page("meetings.html", q="", groups=[], items=items, nav="list")

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        """Number of meetings per status for the menu bar icon. Counters only: no
        titles, paths or content."""
        with open_db() as conn:
            counts = queries.status_counts(conn)
        return JSONResponse(counts, headers={"Cache-Control": "no-store"})

    @app.get("/meetings/{meeting_id:int}.md")
    def export_md(meeting_id: int) -> Response:
        check_id(meeting_id)
        with open_db() as conn:
            meeting = repo.get_meeting(conn, meeting_id)
        if meeting is None or meeting.status != "done":
            raise StarletteHTTPException(404)
        stamp = display.parse_utc(meeting.recorded_at)
        text = render_markdown(
            meeting.transcript,
            title=meeting.title,
            recorded_at=stamp.astimezone().strftime("%Y-%m-%d %H:%M") if stamp else None,
            names=meeting.names,
            lang=ui_lang(),
        )
        return Response(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="meeting-{meeting_id}.md"'},
        )

    @app.get("/meetings/{meeting_id:int}/status", response_class=HTMLResponse)
    def meeting_status(meeting_id: int) -> HTMLResponse:
        """Status block polled from the meeting page. On a final status htmx reloads the
        page (HX-Refresh) to show the transcript or the error."""
        check_id(meeting_id)
        with open_db() as conn:
            meeting = repo.get_meeting(conn, meeting_id)
            stage = queries.job_stage(conn, meeting_id)
            started = queries.stage_started(conn).get(meeting_id)
        if meeting is None:
            raise StarletteHTTPException(404)
        headers = {"HX-Refresh": "true"} if meeting.status in _FINAL else None
        return page("_status.html", headers=headers, meeting=meeting, stage=stage, started=started)

    @app.get("/meetings/{meeting_id:int}", response_class=HTMLResponse)
    def meeting_page(meeting_id: int) -> HTMLResponse:
        check_id(meeting_id)
        with open_db() as conn:
            meeting = repo.get_meeting(conn, meeting_id)
            stage = queries.job_stage(conn, meeting_id)
            started = queries.stage_started(conn).get(meeting_id)
            language = queries.meeting_languages(conn, [meeting_id]).get(meeting_id)
        if meeting is None:
            raise StarletteHTTPException(404)
        labels = speaker_labels(meeting.transcript, meeting.names, lang=ui_lang())
        classes = display.speaker_classes([*labels, *meeting.labels])
        speakers = [
            {"label": label, "shown": shown, "name": meeting.names.get(label, "")}
            for label, shown in labels.items()
        ]
        for label in meeting.labels:
            if label not in labels:
                speakers.append(
                    {"label": label, "shown": label, "name": meeting.names.get(label, "")}
                )
        for index, speaker in enumerate(speakers, start=1):
            speaker["index"] = str(index)
            speaker["spk"] = classes.get(speaker["label"], "")
        segments = [
            {
                "idx": idx,
                "segment": s,
                "speaker": labels.get(s.speaker, s.speaker),
                "spk": classes.get(s.speaker, ""),
                # The name shows at the start of a turn and is visually hidden after.
                "turn": idx == 0 or meeting.segments[idx - 1].speaker != s.speaker,
            }
            for idx, s in enumerate(meeting.segments)
        ]
        return page(
            "meeting.html",
            meeting=meeting,
            stage=stage,
            started=started,
            speakers=speakers,
            segments=segments,
            language=language,
            long=(meeting.duration_s or 0) >= 3600,
            nav="list",
        )

    # --- edits -----------------------------------------------------------------------

    @app.post("/meetings/{meeting_id:int}/meta")
    async def edit_meta(meeting_id: int, request: Request) -> Response:
        check_id(meeting_id)
        form = await read_form(request)
        try:
            recorded_at = display.from_local_input(form.get("recorded_at", ""))
        except (ValueError, OverflowError):
            return message(request, 400, t("bad_date"))

        def save() -> str | None:
            with open_db() as conn:
                if repo.get_meeting(conn, meeting_id) is None:
                    raise StarletteHTTPException(404)
                if not repo.update_meta(
                    conn, meeting_id, title=form.get("title", ""), recorded_at=recorded_at
                ):
                    return t("title_empty")
            return None

        error = await run_in_threadpool(save)
        if error:
            return message(request, 400, error)
        return done(request, f"/meetings/{meeting_id}")

    @app.post("/meetings/{meeting_id:int}/speakers")
    async def rename(meeting_id: int, request: Request) -> Response:
        check_id(meeting_id)
        form = await read_form(request)
        label, name = form.get("label", ""), form.get("name", "")

        def save() -> str | None:
            with open_db() as conn:
                meeting = repo.get_meeting(conn, meeting_id)
                if meeting is None:
                    raise StarletteHTTPException(404)
                if _name_taken(meeting, label, name):
                    return t("name_taken")
                if not repo.rename_speaker(conn, meeting_id, label, name):
                    return t("no_such_speaker")
            return None

        error = await run_in_threadpool(save)
        if error:
            return message(request, 400, error)
        return done(request, f"/meetings/{meeting_id}")

    @app.post("/meetings/{meeting_id:int}/retry")
    def retry(meeting_id: int, request: Request, back: str = "") -> Response:
        check_id(meeting_id)
        with open_db() as conn:
            if not repo.retry(conn, meeting_id):
                return message(request, 409, t("nothing_to_retry"))
        if back == "meeting":
            return done(request, f"/meetings/{meeting_id}")
        if _is_htmx(request):
            return Response(status_code=204, headers={"HX-Trigger": "queue-changed"})
        return RedirectResponse("/", status_code=303)

    @app.post("/meetings/{meeting_id:int}/delete")
    def delete(meeting_id: int, request: Request) -> Response:
        check_id(meeting_id)
        with open_db() as conn:
            try:
                source = repo.delete_meeting(conn, meeting_id)
            except repo.MeetingBusy:
                return message(request, 409, t("meeting_busy"))
        if source is None:
            raise StarletteHTTPException(404)
        kind, ref = source
        # The path is built only from a reference of a strict shape: 32 hex characters.
        if kind == repo.SOURCE_UPLOAD and UPLOAD_REF.fullmatch(ref):
            (uploads_dir(data_root) / ref).unlink(missing_ok=True)
        return done(request, "/meetings")

    return app
