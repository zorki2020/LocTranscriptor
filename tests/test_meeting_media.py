"""The player in the meeting page: `GET /meetings/{id}/media` (whole file and byte ranges),
which file is played, and the page markup.

Synthetic data only: the "audio" is a few bytes with a real container signature, made at
run time inside `tmp_path`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_app_mode import KEY, PORT, AppEnv, FakeStore
from tests.test_meeting_audio import AUDIO, make_audio
from tests.test_recordings import _clean_languages  # noqa: F401 - autouse fixture
from transcribator.models import Segment, Transcript
from transcribator.paths import db_path
from transcribator.storage import db, repo
from transcribator.web import media, strings

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + bytes(range(256)) * 8
M4A = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00" + b"m" * 500


@pytest.fixture
def env(tmp_path: Path) -> Iterator[AppEnv]:
    app_env = AppEnv(tmp_path / "data", FakeStore(ready=True))
    yield app_env
    app_env.client.close()


def finished(
    env: AppEnv,
    *,
    kind: str = "path",
    ref: str = "",
    layout: str = "mono",
    audio_ref: str | None = None,
) -> int:
    """A finished meeting with two paragraphs; returns its id."""
    conn = db.connect(db_path(env.root))
    try:
        meeting_id = repo.enqueue(
            conn, title="t", recorded_at=None, source_kind=kind, source_ref=ref, layout=layout
        )
        job = repo.claim_next(conn)
        assert job is not None
        transcript = Transcript(
            segments=[Segment(0.0, 4.0, "S1", "one two"), Segment(4.0, 9.5, "S2", "three")],
            duration_s=9.5,
        )
        repo.complete(conn, job, transcript)
        if audio_ref is not None:
            conn.execute("UPDATE meeting SET audio_ref = ? WHERE id = ?", (audio_ref, meeting_id))
        return meeting_id
    finally:
        conn.close()


def original(tmp_path: Path, data: bytes = WAV, name: str = "source.bin") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- pure helpers --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00", "audio/mp4"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00", "video/mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        (b"ID3\x04\x00\x00\x00\x00\x00\x00", "audio/mpeg"),
        (b"\xff\xfb\x90\x00" + b"\x00" * 8, "audio/mpeg"),
        (b"fLaC\x00\x00\x00\x22", "audio/flac"),
        (b"FORM\x00\x00\x00\x00AIFF", "audio/aiff"),
        (b"caff\x00\x01\x00\x00", "audio/x-caf"),
        (b"<html><script>", None),
        (b"RIFF\x00\x00\x00\x00AVI ", None),
        (b"", None),
    ],
)
def test_sniff_goes_by_the_first_bytes(tmp_path: Path, head: bytes, expected: str | None) -> None:
    path = tmp_path / "anything.mp3"
    path.write_bytes(head)
    fd = os.open(path, os.O_RDONLY)
    try:
        assert media.sniff(fd) == expected
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, (0, 99)),
        ("bytes=0-", (0, 99)),
        ("bytes=10-19", (10, 19)),
        ("bytes=90-500", (90, 99)),
        ("bytes=-10", (90, 99)),
        ("bytes=-500", (0, 99)),
        (" bytes=5-5 ", (5, 5)),
    ],
)
def test_parse_range(header: str | None, expected: tuple[int, int]) -> None:
    assert media.parse_range(header, 100) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=100-",
        "bytes=20-10",
        "bytes=-0",
        "bytes=-",
        "bytes=0-1,5-6",
        "items=0-1",
        "bytes=a-b",
        "bytes=" + "9" * 30 + "-",
        "",
    ],
)
def test_parse_range_refuses_what_it_cannot_serve(header: str) -> None:
    with pytest.raises(ValueError, match="range"):
        media.parse_range(header, 100)


def test_open_original_takes_regular_files_only(tmp_path: Path) -> None:
    path = original(tmp_path)
    opened = media.open_original(str(path))
    assert opened is not None and opened[1] == len(WAV)
    os.close(opened[0])
    empty = original(tmp_path, b"", "empty.bin")
    os.mkfifo(tmp_path / "pipe")
    for bad in ("", "relative/name", str(tmp_path), str(empty), str(tmp_path / "gone"), "/x\0y"):
        assert media.open_original(bad) is None
    assert media.open_original(str(tmp_path / "pipe")) is None


# --- the endpoint ----------------------------------------------------------------------


def test_original_is_played_whole_and_in_ranges(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = finished(env, ref=str(original(tmp_path)))
    url = f"/meetings/{meeting_id}/media"
    whole = env.client.get(url)
    assert whole.status_code == 200 and whole.content == WAV
    assert whole.headers["content-type"] == "audio/wav"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["content-length"] == str(len(WAV))
    assert whole.headers.get_list("cache-control") == ["no-store"]
    assert whole.headers.get_list("x-content-type-options") == ["nosniff"]
    assert "content-disposition" not in whole.headers
    part = env.client.get(url, headers={"Range": "bytes=100-299"})
    assert part.status_code == 206 and part.content == WAV[100:300]
    assert part.headers["content-range"] == f"bytes 100-299/{len(WAV)}"
    assert part.headers["content-length"] == "200"
    tail = env.client.get(url, headers={"Range": "bytes=-16"})
    assert tail.status_code == 206 and tail.content == WAV[-16:]
    rest = env.client.get(url, headers={"Range": "bytes=2000-"})
    assert rest.status_code == 206 and rest.content == WAV[2000:]


def test_range_outside_the_file_is_refused(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = finished(env, ref=str(original(tmp_path)))
    for header in (f"bytes={len(WAV)}-", "bytes=5-1", "bytes=0-1,4-5", "nonsense"):
        response = env.client.get(f"/meetings/{meeting_id}/media", headers={"Range": header})
        assert response.status_code == 416
        assert response.headers["content-range"] == f"bytes */{len(WAV)}"
        assert response.content == b""


def test_kept_audio_wins_over_the_original(env: AppEnv, tmp_path: Path) -> None:
    make_audio(env, AUDIO, M4A)
    meeting_id = finished(env, ref=str(original(tmp_path)), audio_ref=AUDIO)
    response = env.client.get(f"/meetings/{meeting_id}/media")
    assert response.status_code == 200 and response.content == M4A
    assert response.headers["content-type"] == "audio/mp4"


def test_nothing_to_play(env: AppEnv, tmp_path: Path) -> None:
    gone = finished(env, ref=str(tmp_path / "moved-away"))
    not_audio = finished(env, ref=str(original(tmp_path, b"<html>hello</html>", "page.bin")))
    upload = finished(env, kind="upload", ref="0" * 32)
    folder = finished(env, ref=str(tmp_path))
    for meeting_id in (gone, not_audio, upload, folder, 999):
        assert env.client.get(f"/meetings/{meeting_id}/media").status_code == 404
    page = env.client.get(f"/meetings/{upload}").text
    assert 'id="player"' not in page and "can-play" not in page
    # The page does not touch the original: a missing file shows up only when it is played.
    for meeting_id in (gone, not_audio, folder):
        assert 'id="player"' in env.client.get(f"/meetings/{meeting_id}").text


def test_upload_copy_is_never_played_by_its_stored_name(env: AppEnv) -> None:
    """A browser upload is deleted after processing; its reference is a bare name, and a
    file that happens to sit under that name must not be served."""
    name = "a" * 32
    (env.root / "uploads").mkdir(exist_ok=True)
    (env.root / "uploads" / name).write_bytes(WAV)
    meeting_id = finished(env, kind="upload", ref=name)
    assert env.client.get(f"/meetings/{meeting_id}/media").status_code == 404


def test_unfinished_meeting_is_not_played(env: AppEnv, tmp_path: Path) -> None:
    conn = db.connect(db_path(env.root))
    try:
        meeting_id = repo.enqueue(
            conn,
            title="t",
            recorded_at=None,
            source_kind="path",
            source_ref=str(original(tmp_path)),
        )
    finally:
        conn.close()
    assert env.client.get(f"/meetings/{meeting_id}/media").status_code == 404


def test_media_needs_the_key_and_a_local_host(env: AppEnv, tmp_path: Path) -> None:
    path = f"/meetings/{finished(env, ref=str(original(tmp_path)))}/media"
    assert env.client.get(path).status_code == 200
    env.client.cookies.clear()
    assert env.client.get(path).status_code == 403
    env.client.cookies.set("tb_key", KEY, domain="127.0.0.1")
    assert env.client.get(path, headers={"Host": "evil.example"}).status_code == 403
    assert env.client.get(path, headers={"Host": f"127.0.0.1:{PORT + 1}"}).status_code == 403


def test_media_is_not_answered_to_another_site(env: AppEnv, tmp_path: Path) -> None:
    path = f"/meetings/{finished(env, ref=str(original(tmp_path)))}/media"
    for site in ("cross-site", "same-site"):
        assert env.client.get(path, headers={"Sec-Fetch-Site": site}).status_code == 404
    for site in ("same-origin", "none"):
        assert env.client.get(path, headers={"Sec-Fetch-Site": site}).status_code == 200


def test_no_descriptor_is_left_open(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = finished(env, ref=str(original(tmp_path)))
    before = len(os.listdir("/dev/fd"))
    for _ in range(20):
        env.client.get(f"/meetings/{meeting_id}")
        env.client.get(f"/meetings/{meeting_id}/media", headers={"Range": "bytes=0-9"})
        env.client.get(f"/meetings/{meeting_id}/media", headers={"Range": "bytes=9-1"})
    assert len(os.listdir("/dev/fd")) <= before + 2


# --- the page ----------------------------------------------------------------------------


def test_page_has_the_player_and_times(env: AppEnv, tmp_path: Path) -> None:
    meeting_id = finished(env, ref=str(original(tmp_path)))
    page = env.client.get(f"/meetings/{meeting_id}").text
    assert f'id="player" role="group" aria-label="{strings.t("player_label")}"' in page
    assert f'data-src="/meetings/{meeting_id}/media"' in page
    assert "data-mono" not in page
    assert '<section class="transcript can-play"' in page
    assert strings.t("player_hint") in page
    assert re.search(r'id="seg-0" data-start="0\.00" data-end="4\.00"', page)
    assert re.search(r'id="seg-1" data-start="4\.00" data-end="9\.50"', page)
    assert 'type="range" min="0" max="9.5"' in page
    assert "<audio" not in page and "style=" not in page
    assert re.search(r"\son[a-z]+\s*=", page) is None


def test_two_channel_recording_is_marked_for_mono_playback(env: AppEnv) -> None:
    make_audio(env, AUDIO, M4A)
    meeting_id = finished(
        env, kind="upload", ref="rec" + "0" * 24 + ".wav", layout="mic_system", audio_ref=AUDIO
    )
    assert " data-mono " in env.client.get(f"/meetings/{meeting_id}").text


def test_player_strings_exist_in_both_languages() -> None:
    for key in (
        "player_label",
        "player_play",
        "player_pause",
        "player_seek",
        "player_speed",
        "player_hint",
        "player_error",
    ):
        entry = strings.STRINGS[key]
        assert entry["en"].strip() and entry["ru"].strip(), key


def test_app_js_player_smoke() -> None:
    """Only that the pieces are wired together; the behaviour is checked by hand."""
    root = Path(strings.__file__).parent / "static"
    script = (root / "app.js").read_text(encoding="utf-8")
    for marker in (
        'byId("player")',
        "ui.root.dataset.src",
        "elementFromPoint",
        'audio.preload = "metadata"',
        "createMediaElementSource",
        "selection.isCollapsed",
        "player.init();",
        'word.className = "w"',
        'classList.add("is-word")',
        'classList.add("is-hover")',
        "requestAnimationFrame(tick)",
    ):
        assert marker in script, marker
    css = (root / "app.css").read_text(encoding="utf-8")
    for selector in (
        ".player {",
        ".player-seek {",
        ".seg.is-playing .ts {",
        ".i-play {",
        ".i-pause {",
        ".w.is-word {",
        ".w.is-hover {",
    ):
        assert selector in css, selector
    for icon in ("play.svg", "pause.svg"):
        assert (root / "icons" / icon).read_text(encoding="utf-8").startswith("<svg ")


# --- word times ----------------------------------------------------------------------------


def test_word_times_follow_the_words_of_the_text() -> None:
    from transcribator.models import Word
    from transcribator.pipeline.merge import build_segments

    words = [
        Word(1.0, 1.2, "Hello"),
        Word(1.2, 1.25, ","),
        Word(1.5, 1.9, "two words"),
        Word(2.0, 2.3, "end"),
        Word(2.3, 2.35, "."),
        Word(9.0, 9.4, "Next"),
    ]
    first, second = build_segments(words, ["S1"] * len(words))
    assert first.text == "Hello, two words end."
    assert first.word_starts == (1.0, 1.5, 1.5, 2.0)
    assert len(first.word_starts) == len(first.text.split())
    assert second.word_starts == (9.0,)


def test_word_times_are_stored_and_reach_the_page(env: AppEnv, tmp_path: Path) -> None:
    conn = db.connect(db_path(env.root))
    try:
        meeting_id = repo.enqueue(
            conn,
            title="t",
            recorded_at=None,
            source_kind="path",
            source_ref=str(original(tmp_path)),
        )
        job = repo.claim_next(conn)
        assert job is not None
        transcript = Transcript(
            segments=[
                Segment(1.0, 3.0, "S1", "one two three", (1.0, 1.504, 2.25)),
                # Times that do not match the words are not kept.
                Segment(3.0, 4.0, "S1", "four five", (3.0,)),
                Segment(4.0, 5.0, "S1", "six"),
            ],
            duration_s=5.0,
        )
        repo.complete(conn, job, transcript)
        loaded = repo.get_meeting(conn, meeting_id)
    finally:
        conn.close()
    assert loaded is not None
    assert [s.word_starts for s in loaded.segments] == [(1.0, 1.5, 2.25), (), ()]
    page = env.client.get(f"/meetings/{meeting_id}").text
    assert 'id="seg-0" data-start="1.00" data-end="3.00" data-words="1.0,1.5,2.25">' in page
    assert page.count("data-words=") == 1


def test_word_times_survive_the_transcript_round_trip() -> None:
    transcript = Transcript(
        segments=[Segment(0.0, 1.0, "S1", "a b", (0.0, 0.5)), Segment(1.0, 2.0, "S2", "c")],
        duration_s=2.0,
    )
    data = transcript.to_dict()
    assert "word_starts" not in data["segments"][1]
    again = Transcript.from_dict(data)
    assert [s.word_starts for s in again.segments] == [(0.0, 0.5), ()]
