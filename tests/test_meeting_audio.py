"""Kept audio of recordings: `audio_ref` on `POST /api/recordings`, `GET /meetings/{id}/audio`,
deletion together with the meeting.

Synthetic data only: the "audio" is a few bytes made at run time inside `tmp_path`.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_app_mode import BASE, KEY, PORT, AppEnv, FakeStore
from tests.test_recordings import (
    OTHER_REF,
    REF,
    _clean_languages,  # noqa: F401 - autouse fixture of the recordings tests
    body,
    make_wav,
    rows,
    send,
)
from transcribator.paths import audio_dir, db_path
from transcribator.storage import db, repo
from transcribator.web import app as web_app
from transcribator.web import recordings

AUDIO = REF.replace(".wav", ".m4a")
OTHER_AUDIO = OTHER_REF.replace(".wav", ".m4a")
PAYLOAD = b"fake-audio-bytes" * 64


@pytest.fixture
def env(tmp_path: Path) -> Iterator[AppEnv]:
    app_env = AppEnv(tmp_path / "data", FakeStore(ready=True))
    yield app_env
    app_env.client.close()


def make_audio(env: AppEnv, name: str = AUDIO, data: bytes = PAYLOAD) -> Path:
    path = audio_dir(env.root) / name
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def queue(env: AppEnv, ref: str = REF, audio: str | None = AUDIO) -> int:
    """Queue a synthetic recording with its audio; return the meeting id."""
    make_wav(env, ref)
    if audio is not None and not (audio_dir(env.root) / audio).exists():
        make_audio(env, audio)
    response = send(env, body(ref=ref, audio_ref=audio))
    assert response.status_code == 200, response.text
    return int(response.json()["meeting_id"])


def stored(env: AppEnv) -> list[str | None]:
    return [r["audio_ref"] for r in rows(env)]


def poison(env: AppEnv, meeting_id: int, value: str | None) -> None:
    conn = db.connect(db_path(env.root))
    try:
        conn.execute("UPDATE meeting SET audio_ref = ? WHERE id = ?", (value, meeting_id))
    finally:
        conn.close()


def get_audio(env: AppEnv, meeting_id: int, **kwargs: Any) -> httpx.Response:
    return env.client.get(f"/meetings/{meeting_id}/audio", **kwargs)


# --- storage -------------------------------------------------------------------------


def test_meeting_without_audio_has_no_ref(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    try:
        meeting_id = repo.enqueue(
            conn, title="a", recorded_at=None, source_kind="upload", source_ref="u"
        )
        meeting = repo.get_meeting(conn, meeting_id)
        assert meeting is not None and meeting.audio_ref is None
        assert repo.remove_meeting(conn, meeting_id) == repo.RemovedMeeting("upload", "u", None)
    finally:
        conn.close()


def test_old_database_gets_the_audio_column(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path, isolation_level=None)
    for script in db._MIGRATIONS[:3]:
        for statement in db._split(script):
            old.execute(statement)
    old.execute("PRAGMA user_version = 3")
    old.execute(
        "INSERT INTO meeting (title, added_at, status, source_kind, source_ref)"
        " VALUES ('old', '2026-01-01T00:00:00+00:00', 'queued', 'upload', 'u')"
    )
    old.close()
    conn = db.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT audio_ref FROM meeting").fetchone()[0] is None
    finally:
        conn.close()


def test_enqueue_upload_once_stores_the_audio_ref(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "t.sqlite3")
    try:
        meeting_id = repo.enqueue_upload_once(
            conn,
            title="a",
            recorded_at=None,
            source_ref=REF,
            language="ru",
            layout="mono",
            audio_ref=AUDIO,
        )
        meeting = repo.get_meeting(conn, meeting_id)
        assert meeting is not None and meeting.audio_ref == AUDIO
        removed = repo.remove_meeting(conn, meeting_id)
        assert removed == repo.RemovedMeeting("upload", REF, AUDIO)
    finally:
        conn.close()


# --- POST /api/recordings with audio_ref ----------------------------------------------


def test_valid_audio_ref_is_accepted_and_stored(env: AppEnv) -> None:
    meeting_id = queue(env)
    assert stored(env) == [AUDIO]
    assert get_audio(env, meeting_id).status_code == 200


def test_audio_ref_is_optional_and_null_means_none(env: AppEnv) -> None:
    make_wav(env)
    assert send(env, body(audio_ref=None)).status_code == 200
    make_wav(env, OTHER_REF)
    assert send(env, body(ref=OTHER_REF)).status_code == 200
    assert stored(env) == [None, None]


def test_audio_folder_is_private(env: AppEnv) -> None:
    queue(env)
    assert (env.root / "audio").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "bad",
    [
        "../" + AUDIO,
        "audio/" + AUDIO,
        "/" + AUDIO,
        AUDIO + "/",
        AUDIO + "\n",
        AUDIO.replace(".m4a", ".wav"),
        AUDIO.replace(".m4a", ".mp3"),
        AUDIO.replace(".m4a", ""),
        AUDIO.replace(".m4a", ".M4A"),
        AUDIO.replace("0123456789abcdef", "0123456789ABCDEF"),
        "rec0123456789abcdef0123.m4a",  # too short
        "rec0123456789abcdef012345678.m4a",  # too long
        "xxx0123456789abcdef01234567.m4a",
        "rec0123456789abcdef0123456g.m4a",
        "",
        ".m4a",
        OTHER_AUDIO,  # a well-formed name of another recording
        5,
        ["x"],
        {"a": 1},
        True,
    ],
)
def test_bad_audio_ref_is_refused(env: AppEnv, bad: object) -> None:
    make_wav(env)
    make_audio(env)
    make_audio(env, OTHER_AUDIO)
    response = send(env, body(audio_ref=bad))
    assert response.status_code == 400
    assert response.json() == {"error": "bad_audio_ref"}
    assert rows(env) == []


def test_symlink_audio_is_refused(env: AppEnv, tmp_path: Path) -> None:
    make_wav(env)
    target = tmp_path / "elsewhere"
    target.write_bytes(PAYLOAD)
    os.symlink(target, audio_dir(env.root) / AUDIO)
    response = send(env, body(audio_ref=AUDIO))
    assert (response.status_code, response.json()) == (400, {"error": "bad_audio_ref"})
    assert rows(env) == []
    assert target.read_bytes() == PAYLOAD


def test_dangling_symlink_audio_is_refused(env: AppEnv, tmp_path: Path) -> None:
    make_wav(env)
    os.symlink(tmp_path / "nowhere", audio_dir(env.root) / AUDIO)
    response = send(env, body(audio_ref=AUDIO))
    assert response.status_code == 400 and rows(env) == []


def test_directory_audio_is_refused(env: AppEnv) -> None:
    make_wav(env)
    (audio_dir(env.root) / AUDIO).mkdir()
    response = send(env, body(audio_ref=AUDIO))
    assert (response.status_code, response.json()) == (400, {"error": "bad_audio_ref"})


def test_named_pipe_audio_is_refused_without_hanging(env: AppEnv) -> None:
    make_wav(env)
    os.mkfifo(audio_dir(env.root) / AUDIO)
    response = send(env, body(audio_ref=AUDIO))
    assert (response.status_code, response.json()) == (400, {"error": "bad_audio_ref"})


def test_missing_audio_file(env: AppEnv) -> None:
    make_wav(env)
    response = send(env, body(audio_ref=AUDIO))
    assert (response.status_code, response.json()) == (400, {"error": "audio_missing"})
    assert rows(env) == []


def test_missing_audio_folder_is_not_an_error_of_its_own(env: AppEnv) -> None:
    make_wav(env)
    assert not (env.root / "audio").exists()
    response = send(env, body(audio_ref=AUDIO))
    assert response.json() == {"error": "audio_missing"}


def test_empty_audio_file(env: AppEnv) -> None:
    make_wav(env)
    make_audio(env, data=b"")
    response = send(env, body(audio_ref=AUDIO))
    assert (response.status_code, response.json()) == (400, {"error": "audio_missing"})
    assert rows(env) == []


def test_smallest_accepted_audio(env: AppEnv) -> None:
    make_wav(env)
    make_audio(env, data=b"x")
    assert send(env, body(audio_ref=AUDIO)).status_code == 200


def test_wav_problems_are_reported_before_audio_ones(env: AppEnv) -> None:
    make_audio(env)
    response = send(env, body(audio_ref=AUDIO))
    assert response.json() == {"error": "file_missing"}


def test_audio_for_another_recording_is_refused(env: AppEnv) -> None:
    make_wav(env)
    make_audio(env, OTHER_AUDIO)
    response = send(env, body(audio_ref=OTHER_AUDIO))
    assert response.json() == {"error": "bad_audio_ref"}


def test_repeat_is_idempotent_and_keeps_the_first_audio_ref(env: AppEnv) -> None:
    first = queue(env)
    again = send(env, body(audio_ref=AUDIO))
    assert again.json() == {"meeting_id": first}
    # The retry may come without audio or with a different one: the meeting keeps its own.
    make_audio(env, OTHER_AUDIO)
    assert send(env, body(audio_ref=None)).json() == {"meeting_id": first}
    assert stored(env) == [AUDIO]


def test_repeat_with_missing_audio_still_returns_the_meeting(env: AppEnv) -> None:
    first = queue(env)
    (audio_dir(env.root) / AUDIO).unlink()
    assert send(env, body(audio_ref=AUDIO)).json() == {"meeting_id": first}
    assert len(rows(env)) == 1


def test_repeat_without_audio_does_not_add_audio(env: AppEnv) -> None:
    make_wav(env)
    first = send(env, body()).json()["meeting_id"]
    make_audio(env)
    assert send(env, body(audio_ref=AUDIO)).json() == {"meeting_id": first}
    assert stored(env) == [None]


def test_errors_do_not_echo_the_audio_ref(env: AppEnv) -> None:
    make_wav(env)
    response = send(env, body(audio_ref="../secret-name"))
    assert "secret-name" not in response.text and response.json() == {"error": "bad_audio_ref"}


def test_body_limit_still_applies_with_audio_ref(env: AppEnv) -> None:
    make_wav(env)
    response = send(env, body(audio_ref=AUDIO, title="x" * 5000))
    assert response.status_code == 413


def test_audio_ref_needs_the_same_protections_as_before(env: AppEnv) -> None:
    make_wav(env)
    make_audio(env)
    payload = body(audio_ref=AUDIO)
    assert send(env, payload, headers={"Origin": "http://evil.example"}).status_code == 403
    assert send(env, payload, headers={"X-CSRF-Token": "nope"}).status_code == 403
    env.client.cookies.clear()
    assert send(env, payload).status_code == 403
    assert rows(env) == []


# --- GET /meetings/{id}/audio --------------------------------------------------------


def test_download_headers_in_app_mode(env: AppEnv) -> None:
    make_wav(env)
    make_audio(env)
    meeting_id = send(env, body(title="Weekly: sync/plan", audio_ref=AUDIO)).json()["meeting_id"]
    response = get_audio(env, meeting_id)
    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-type"] == "audio/mp4"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f'filename="meeting-{meeting_id}.m4a"' in disposition
    assert "filename*=UTF-8''Weekly%20sync%20plan.m4a" in disposition
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-length"] == str(len(PAYLOAD))


def test_download_name_uses_the_default_title_when_there_is_none(env: AppEnv) -> None:
    meeting_id = queue(env)
    disposition = get_audio(env, meeting_id).headers["content-disposition"]
    assert disposition.endswith(".m4a") and "filename*=UTF-8''" in disposition


def test_download_name_outside_app_mode_carries_the_id_only(tmp_path: Path) -> None:
    root = tmp_path / "data"
    app_env = AppEnv(root, FakeStore(ready=True))
    app_env.client.close()
    plain = web_app.create_app(db_file=db_path(app_env.root), data_root=app_env.root, port=PORT)
    conn = db.connect(db_path(app_env.root))
    try:
        meeting_id = repo.enqueue_upload_once(
            conn,
            title="Private title",
            recorded_at=None,
            source_ref=REF,
            language="ru",
            layout="mono",
            audio_ref=AUDIO,
        )
    finally:
        conn.close()
    path = audio_dir(app_env.root) / AUDIO
    path.write_bytes(PAYLOAD)
    with TestClient(plain, base_url=BASE) as client:
        response = client.get(f"/meetings/{meeting_id}/audio")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="meeting-{meeting_id}.m4a"'
    )
    assert "Private" not in str(response.headers)


def test_range_header_is_ignored_and_the_whole_file_is_sent(env: AppEnv) -> None:
    meeting_id = queue(env)
    response = get_audio(env, meeting_id, headers={"Range": "bytes=0-9"})
    assert response.status_code == 200
    assert response.content == PAYLOAD
    assert response.headers["content-type"] == "audio/mp4"
    assert response.headers["cache-control"] == "no-store"


def test_no_audio_means_404(env: AppEnv) -> None:
    make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    assert get_audio(env, meeting_id).status_code == 404


def test_file_gone_means_404(env: AppEnv) -> None:
    meeting_id = queue(env)
    (audio_dir(env.root) / AUDIO).unlink()
    assert get_audio(env, meeting_id).status_code == 404


def test_empty_file_means_404(env: AppEnv) -> None:
    meeting_id = queue(env)
    (audio_dir(env.root) / AUDIO).write_bytes(b"")
    assert get_audio(env, meeting_id).status_code == 404


def test_unknown_meeting_means_404(env: AppEnv) -> None:
    assert get_audio(env, 999).status_code == 404
    assert get_audio(env, 10**30).status_code == 404


def test_symlink_swapped_in_after_queueing_is_not_served(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = queue(env)
    secret = tmp_path / "secret"
    secret.write_bytes(b"do-not-serve")
    path = audio_dir(env.root) / AUDIO
    path.unlink()
    os.symlink(secret, path)
    assert get_audio(env, meeting_id).status_code == 404


def test_audio_of_another_meeting_cannot_be_reached_by_tampering(env: AppEnv) -> None:
    first = queue(env, REF, AUDIO)
    make_audio(env, OTHER_AUDIO, b"other-meeting-audio")
    make_wav(env, OTHER_REF)
    second = send(env, body(ref=OTHER_REF, audio_ref=OTHER_AUDIO)).json()["meeting_id"]
    assert get_audio(env, first).content == PAYLOAD
    assert get_audio(env, second).content == b"other-meeting-audio"
    for url in (
        f"/meetings/{first}/audio?ref={OTHER_AUDIO}",
        f"/meetings/{first}/audio?audio_ref={OTHER_AUDIO}&id={second}",
        f"/meetings/{first}/audio/{OTHER_AUDIO}",
        f"/meetings/{first}/audio/../{second}/audio",
    ):
        response = env.client.get(url, headers={"X-Audio-Ref": OTHER_AUDIO})
        assert response.content != b"other-meeting-audio", url
    assert get_audio(env, 0).status_code == 404


def test_poisoned_reference_in_the_database_is_not_served(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = queue(env)
    outside = tmp_path / "data" / "transcribator.sqlite3"
    assert outside.exists()
    for bad in ("../transcribator.sqlite3", "/etc/hosts", "audio/../uploads/" + REF, "x.m4a"):
        poison(env, meeting_id, bad)
        assert get_audio(env, meeting_id).status_code == 404


@pytest.mark.parametrize("path", ["/meetings/1/audio"])
def test_download_needs_the_key_and_a_local_host(env: AppEnv, path: str) -> None:
    queue(env)
    assert env.client.get(path).status_code == 200
    env.client.cookies.clear()
    assert env.client.get(path).status_code == 403
    env.client.cookies.set("tb_key", "wrong" * 8, domain="127.0.0.1")
    assert env.client.get(path).status_code == 403
    env.client.cookies.set("tb_key", KEY, domain="127.0.0.1")
    assert env.client.get(path, headers={"Host": "evil.example"}).status_code == 403
    assert env.client.get(path, headers={"Host": f"127.0.0.1:{PORT + 1}"}).status_code == 403


def test_download_carries_the_security_headers_once(env: AppEnv) -> None:
    meeting_id = queue(env)
    response = get_audio(env, meeting_id)
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]
    assert response.headers.get_list("cache-control") == ["no-store"]
    assert "content-security-policy" in response.headers


# --- the meeting page ----------------------------------------------------------------


def test_meeting_page_context_has_audio_flag(env: AppEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real = web_app.recordings.has_audio

    def spy(folder: Path, ref: str | None) -> bool:
        found = real(folder, ref)
        seen.append(found)
        return found

    monkeypatch.setattr(recordings, "has_audio", spy)
    with_audio = queue(env)
    assert env.client.get(f"/meetings/{with_audio}").status_code == 200
    (audio_dir(env.root) / AUDIO).unlink()
    assert env.client.get(f"/meetings/{with_audio}").status_code == 200
    assert seen == [True, False]


# --- deletion ------------------------------------------------------------------------


def test_deleting_a_meeting_removes_its_audio(env: AppEnv) -> None:
    meeting_id = queue(env)
    path = audio_dir(env.root) / AUDIO
    assert path.exists()
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 200
    assert not path.exists()
    assert rows(env) == []


def test_deleting_leaves_other_meetings_audio(env: AppEnv) -> None:
    first = queue(env, REF, AUDIO)
    make_audio(env, OTHER_AUDIO)
    make_wav(env, OTHER_REF)
    send(env, body(ref=OTHER_REF, audio_ref=OTHER_AUDIO))
    env.post(f"/meetings/{first}/delete")
    assert not (audio_dir(env.root) / AUDIO).exists()
    assert (audio_dir(env.root) / OTHER_AUDIO).exists()


def test_deleting_with_the_file_already_gone(env: AppEnv) -> None:
    meeting_id = queue(env)
    (audio_dir(env.root) / AUDIO).unlink()
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 200
    assert rows(env) == []


def test_deleting_without_audio_folder(env: AppEnv) -> None:
    make_wav(env)
    meeting_id = send(env, body()).json()["meeting_id"]
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 200
    assert not (env.root / "audio").exists()  # deleting does not create the folder


def test_deleting_removes_a_symlink_but_not_its_target(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = queue(env)
    target = tmp_path / "keep-me"
    target.write_bytes(b"precious")
    path = audio_dir(env.root) / AUDIO
    path.unlink()
    os.symlink(target, path)
    env.post(f"/meetings/{meeting_id}/delete")
    assert target.read_bytes() == b"precious"


def test_a_busy_meeting_keeps_its_audio(env: AppEnv) -> None:
    meeting_id = queue(env)
    conn = db.connect(db_path(env.root))
    try:
        conn.execute("UPDATE meeting SET status = 'processing'")
    finally:
        conn.close()
    assert env.post(f"/meetings/{meeting_id}/delete").status_code == 409
    assert (audio_dir(env.root) / AUDIO).exists()


def test_poisoned_reference_never_deletes_outside_the_audio_folder(
    env: AppEnv, tmp_path: Path
) -> None:
    bait = [
        env.root / "bait.m4a",
        env.root / "uploads" / "bait.m4a",
        tmp_path / "outside.m4a",
    ]
    for item in bait:
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_bytes(PAYLOAD)
    poisons = [
        "../bait.m4a",
        "../uploads/bait.m4a",
        "../../outside.m4a",
        "/" + str(tmp_path / "outside.m4a"),
        str(tmp_path / "outside.m4a"),
        "..",
        ".",
        "",
        AUDIO + "/../../bait.m4a",
        "rec0123456789abcdef01234567.m4a\n",
    ]
    for index, bad in enumerate(poisons):
        ref = f"rec{index:024x}.wav"
        make_wav(env, ref)
        meeting_id = send(env, body(ref=ref)).json()["meeting_id"]
        poison(env, meeting_id, bad)
        assert env.post(f"/meetings/{meeting_id}/delete").status_code == 200
    assert rows(env) == []
    assert all(item.read_bytes() == PAYLOAD for item in bait)
    assert (env.root / "uploads").is_dir() and (env.root / "transcribator.sqlite3").exists()


# --- helpers -------------------------------------------------------------------------


def test_has_audio_and_remove_audio_ignore_bad_references(tmp_path: Path) -> None:
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / AUDIO).write_bytes(PAYLOAD)
    assert recordings.has_audio(folder, AUDIO)
    assert not recordings.has_audio(folder, None)
    assert not recordings.has_audio(folder, "../audio/" + AUDIO)
    recordings.remove_audio(folder, "../audio/" + AUDIO)
    recordings.remove_audio(folder, None)
    assert (folder / AUDIO).exists()
    recordings.remove_audio(folder, AUDIO)
    recordings.remove_audio(folder, AUDIO)  # already gone: fine
    assert not (folder / AUDIO).exists()


def test_remove_audio_survives_a_folder_in_the_way(tmp_path: Path) -> None:
    folder = tmp_path / "audio"
    (folder / AUDIO).mkdir(parents=True)
    recordings.remove_audio(folder, AUDIO)
    assert (folder / AUDIO).is_dir()


def test_streamed_descriptor_is_closed_even_when_the_stream_is_abandoned(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "audio"
    folder.mkdir()
    (folder / AUDIO).write_bytes(PAYLOAD)
    opened = recordings.open_meeting_audio(folder, AUDIO)
    assert opened is not None
    fd, size = opened
    assert size == len(PAYLOAD)
    closer = web_app._FdCloser(fd)
    stream = web_app._read_fd(closer, size)
    assert next(stream) == PAYLOAD
    stream.close()  # the client left
    with pytest.raises(OSError):
        os.fstat(fd)
    closer.close()  # a second close is harmless


def test_stream_stops_at_the_verified_size(tmp_path: Path) -> None:
    folder = tmp_path / "audio"
    folder.mkdir()
    path = folder / AUDIO
    path.write_bytes(PAYLOAD)
    opened = recordings.open_meeting_audio(folder, AUDIO)
    assert opened is not None
    fd, size = opened
    path.write_bytes(PAYLOAD + b"appended-later")  # the file grows while it is served
    data = b"".join(web_app._read_fd(web_app._FdCloser(fd), size))
    assert data == PAYLOAD
