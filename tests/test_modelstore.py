"""Model download for the app: a local HTTP server stands in for Hugging Face.

The files are random bytes: no real models, no network beyond 127.0.0.1.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from transcribator import modelstore
from transcribator.modelstore import (
    CHECKSUM,
    NO_SPACE,
    OFFLINE,
    SERVER_ERROR,
    ModelStore,
    bundled_manifest,
    parse_manifest,
)

REPO = "FluidInference/test-coreml"
REV = "a" * 40


class _Server:
    """Serves `files` under /<repo>/resolve/<rev>/<path>, with Range support."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.redirects: dict[str, str] = {}
        self.ignore_range = False
        self.cut_after: int | None = None  # send only this many bytes, then hang up
        self.requests: list[tuple[str, str | None]] = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                server.requests.append((self.path, self.headers.get("Range")))
                if self.path in server.redirects:
                    self.send_response(302)
                    self.send_header("Location", server.redirects[self.path])
                    self.end_headers()
                    return
                prefix = f"/{REPO}/resolve/{REV}/"
                data = server.files.get(self.path.removeprefix(prefix))
                if not self.path.startswith(prefix) or data is None:
                    self.send_error(404)
                    return
                start = 0
                header = self.headers.get("Range")
                if header and not server.ignore_range:
                    start = int(header.removeprefix("bytes=").rstrip("-"))
                    self.send_response(206)
                else:
                    self.send_response(200)
                body = data[start:]
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if server.cut_after is not None:
                    self.wfile.write(body[: server.cut_after])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(body)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server() -> Iterator[_Server]:
    srv = _Server()
    yield srv
    srv.close()


def _manifest(files: dict[str, bytes], *, marker: bool = True) -> modelstore.Manifest:
    return parse_manifest(
        {
            "repos": [
                {
                    "repo": REPO,
                    "revision": REV,
                    "target": "home/Models/test",
                    "marker": marker,
                    "files": [
                        {"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest()}
                        for p, d in files.items()
                    ],
                }
            ]
        }
    )


def _files() -> dict[str, bytes]:
    return {
        "config.json": b"{}",
        "Model.mlmodelc/weights/weight.bin": os.urandom(300_000),
        "Model.mlmodelc/model.mil": os.urandom(5_000),
    }


def _store(root: Path, server: _Server, files: dict[str, bytes], **kw: Any) -> ModelStore:
    server.files = dict(files)
    return ModelStore(
        root, _manifest(files), base_url=server.base, allowed_hosts=("127.0.0.1",), **kw
    )


def test_download_puts_files_in_place_and_writes_marker(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "models", server, files)
    assert not store.ready()
    assert store.state()["phase"] == "idle"

    store.download()

    for path, data in files.items():
        assert (tmp_path / "models/home/Models/test" / path).read_bytes() == data
    marker = tmp_path / "models/home/Models/test/.fluidaudio-revision"
    assert marker.read_text() == REV + "\n"
    assert store.ready() and store.verify()
    assert store.state()["phase"] == "done"
    assert not list((tmp_path / "models").rglob("*.part"))


def test_background_start_reports_progress_and_done(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    store.start()
    store.wait(30)
    state = store.state()
    assert state == {
        "phase": "done",
        "done_bytes": store.manifest.total_bytes,
        "total_bytes": store.manifest.total_bytes,
        "error": None,
    }


def test_interrupted_file_resumes_with_range(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    server.cut_after = 100_000
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code in {OFFLINE, CHECKSUM}
    parts = list((tmp_path / "m").rglob("*.part"))
    partial = sum(p.stat().st_size for p in parts)
    assert parts and partial > 0

    server.cut_after = None
    server.requests.clear()
    store.download()

    assert store.ready() and store.verify()
    ranged = [r for _, r in server.requests if r]
    assert ranged and ranged[0] == f"bytes={partial}-"


def test_server_ignoring_range_restarts_the_file(tmp_path: Path, server: _Server) -> None:
    files = {"big.bin": os.urandom(50_000)}
    store = _store(tmp_path / "m", server, files)
    part = tmp_path / "m/home/Models/test/big.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(files["big.bin"][:1000])
    server.ignore_range = True
    store.download()
    assert store.verify()


def test_corrupted_file_is_rejected_and_removed(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    server.files["Model.mlmodelc/model.mil"] = os.urandom(5_000)  # same size, other bytes
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code == CHECKSUM
    assert not (tmp_path / "m/home/Models/test/Model.mlmodelc/model.mil").exists()
    assert not list((tmp_path / "m").rglob("model.mil.part"))
    assert not store.ready()


def test_too_large_response_is_rejected(tmp_path: Path, server: _Server) -> None:
    files = {"a.bin": os.urandom(1000)}
    store = _store(tmp_path / "m", server, files)
    server.files["a.bin"] = os.urandom(5000)
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code == CHECKSUM


def test_no_space_is_checked_before_downloading(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files, free_bytes=lambda path: 1000)
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code == NO_SPACE
    assert server.requests == []


def test_missing_file_on_server_is_a_server_error(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    del server.files["config.json"]
    store.start()
    store.wait(30)
    assert store.state()["phase"] == "error"
    assert store.state()["error"] == SERVER_ERROR


def test_no_server_means_offline(tmp_path: Path, server: _Server) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    server.close()
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code == OFFLINE


def test_redirect_to_a_foreign_host_is_refused(tmp_path: Path, server: _Server) -> None:
    files = {"a.bin": os.urandom(100)}
    store = _store(tmp_path / "m", server, files)
    server.redirects[f"/{REPO}/resolve/{REV}/a.bin"] = "http://evil.example/a.bin"
    with pytest.raises(modelstore.DownloadError) as exc:
        store.download()
    assert exc.value.code == SERVER_ERROR


def test_real_hosts_need_https_and_hugging_face() -> None:
    allowed = modelstore.ALLOWED_HOSTS
    ok = modelstore._host_allowed
    assert ok("https://huggingface.co/x", allowed, False)
    assert ok("https://cas-bridge.xethub.hf.co/x", allowed, False)
    assert ok("https://cdn-lfs.huggingface.co/x", allowed, False)
    assert not ok("http://huggingface.co/x", allowed, False)
    assert not ok("https://huggingface.co.evil.example/x", allowed, False)
    assert not ok("https://evilhf.co/x", allowed, False)
    assert not ok("file:///etc/passwd", allowed, False)


def test_ready_needs_the_marker(tmp_path: Path, server: _Server) -> None:
    files = {"a.bin": b"x" * 10}
    store = _store(tmp_path / "m", server, files)
    store.download()
    (tmp_path / "m/home/Models/test/.fluidaudio-revision").write_text("other\n")
    fresh = ModelStore(tmp_path / "m", store.manifest)
    assert not fresh.ready()


def test_manifest_rejects_paths_outside_the_models_dir() -> None:
    for target, path in (("../x", "a"), ("home", "../../a"), ("/abs", "a")):
        with pytest.raises(ValueError):
            parse_manifest(
                {
                    "repos": [
                        {
                            "repo": REPO,
                            "revision": REV,
                            "target": target,
                            "files": [{"path": path, "size": 1, "sha256": "0" * 64}],
                        }
                    ]
                }
            )


def test_bundled_manifest_is_pinned_and_complete() -> None:
    manifest = bundled_manifest()
    data = json.loads((Path(modelstore.__file__).parent / "model_manifest.json").read_text())
    for repo in data["repos"]:
        assert len(repo["revision"]) == 40  # a commit, not a branch
    targets = {str(f.target).split("/")[0] for f in manifest.files}
    assert targets == {"parakeet-tdt-0.6b-v3", "home"}
    names = {f.target.name for f in manifest.files}
    assert {"parakeet_vocab.json", "parakeet_v3_vocab.json", "config.json"} <= names
    assert 500 * 1024**2 < manifest.total_bytes < 1024**3


def test_tampered_file_with_same_size_is_noticed_and_replaced(
    tmp_path: Path, server: _Server
) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    store.download()
    target = tmp_path / "m/home/Models/test/Model.mlmodelc/model.mil"
    target.write_bytes(os.urandom(5_000))  # same size, other bytes
    fresh = ModelStore(
        tmp_path / "m", store.manifest, base_url=server.base, allowed_hosts=("127.0.0.1",)
    )
    assert not fresh.ready()
    server.requests.clear()
    fresh.download()
    assert fresh.ready() and fresh.verify()
    assert [p for p, _ in server.requests] == [f"/{REPO}/resolve/{REV}/Model.mlmodelc/model.mil"]


def test_checked_files_without_stamps_are_rehashed_not_downloaded(
    tmp_path: Path, server: _Server
) -> None:
    files = _files()
    store = _store(tmp_path / "m", server, files)
    store.download()
    (tmp_path / "m" / modelstore.VERIFIED).unlink()
    assert not ModelStore(tmp_path / "m", store.manifest).ready()
    server.requests.clear()
    store._ready = False
    store.download()
    assert server.requests == [] and store.ready()
