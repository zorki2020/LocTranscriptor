"""Speech models of the app: download on first run, quick check on every start.

What to download is fixed by `model_manifest.json` (built by
`scripts/make_model_manifest.py`): repository, pinned commit, file path, size and SHA256.
Files go into the models directory in the layout `fluidaudiocli` expects, so the program
itself always runs offline.

The download is the only network access of the app. It starts only when the user asks
for it, talks only to Hugging Face over https (redirects included), resumes an interrupted
file with an HTTP Range request, and accepts a file only if its SHA256 matches: the file is
written as `.part` and renamed after the check. Errors are reported as codes; URLs and
paths are not logged.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import http.client
import json
import os
import shutil
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

MODELS_ENV = "TRANSCRIBATOR_MODELS_DIR"
BASE_URL = "https://huggingface.co"
ALLOWED_HOSTS = ("huggingface.co", ".huggingface.co", ".hf.co")
# Free space kept on top of what the download still needs.
HEADROOM_BYTES = 256 * 1024**2
CHUNK = 1024**2
TIMEOUT_S = 60
MARKER = ".fluidaudio-revision"
# Size and modification time of every file whose SHA256 was checked. A file that changed
# afterwards (even with the same size) is no longer trusted and gets checked again.
VERIFIED = ".verified.json"

# Error codes shown on the setup page.
NO_SPACE = "no_space"
OFFLINE = "offline"
SERVER_ERROR = "server_error"
CHECKSUM = "checksum"
WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class ModelFile:
    repo: str
    revision: str
    path: str  # inside the repository
    target: Path  # relative to the models directory
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    files: tuple[ModelFile, ...]
    markers: tuple[tuple[Path, str], ...]  # (relative path, content)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("bad path in the model manifest")
    return path


def parse_manifest(data: Any) -> Manifest:
    files: list[ModelFile] = []
    markers: list[tuple[Path, str]] = []
    for repo in data["repos"]:
        target = _safe_relative(str(repo["target"]))
        revision = str(repo["revision"])
        for item in repo["files"]:
            sha = str(item["sha256"]).lower()
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                raise ValueError("bad sha256 in the model manifest")
            files.append(
                ModelFile(
                    repo=str(repo["repo"]),
                    revision=revision,
                    path=str(item["path"]),
                    target=target / _safe_relative(str(item["path"])),
                    size=int(item["size"]),
                    sha256=sha,
                )
            )
        if repo.get("marker"):
            markers.append((target / MARKER, revision + "\n"))
    return Manifest(tuple(files), tuple(markers))


def bundled_manifest() -> Manifest:
    text = resources.files("transcribator").joinpath("model_manifest.json").read_text("utf-8")
    return parse_manifest(json.loads(text))


def models_dir() -> Path | None:
    """The app's models directory, or None outside the app (FluidAudio's own layout)."""
    value = os.environ.get(MODELS_ENV)
    return Path(value) if value else None


class DownloadError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _host_allowed(url: str, allowed: tuple[str, ...], insecure_ok: bool) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" and not (insecure_ok and parts.scheme == "http"):
        return False
    host = (parts.hostname or "").lower()
    return any(host == a or (a.startswith(".") and host.endswith(a)) for a in allowed)


class _RestrictedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: tuple[str, ...], insecure_ok: bool) -> None:
        self._allowed = allowed
        self._insecure_ok = insecure_ok

    def redirect_request(  # type: ignore[no-untyped-def]
        self, req, fp, code, msg, headers, newurl
    ) -> urllib.request.Request | None:
        if not _host_allowed(newurl, self._allowed, self._insecure_ok):
            raise DownloadError(SERVER_ERROR)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


class ModelStore:
    """Models directory plus a background download with a progress state."""

    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        *,
        base_url: str = BASE_URL,
        allowed_hosts: tuple[str, ...] = ALLOWED_HOSTS,
        free_bytes: Callable[[Path], int] | None = None,
    ) -> None:
        self.root = root
        self.manifest = manifest
        self._base_url = base_url.rstrip("/")
        self._allowed = allowed_hosts
        base = urllib.parse.urlsplit(base_url)
        self._insecure_ok = base.scheme == "http" and base.hostname == "127.0.0.1"  # tests only
        self._free = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = False
        self._state: dict[str, Any] = {
            "phase": "idle",
            "done_bytes": 0,
            "total_bytes": manifest.total_bytes,
            "error": None,
        }

    # --- checks -----------------------------------------------------------------------

    def _verified(self) -> dict[str, list[Any]]:
        try:
            data = json.loads((self.root / VERIFIED).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _stamp(self, item: ModelFile) -> list[Any]:
        """Size, modification time and the manifest's SHA256: a new manifest (another
        revision) or a changed file both make the old check void."""
        info = (self.root / item.target).stat()
        return [info.st_size, info.st_mtime_ns, item.sha256]

    def _trusted(self, item: ModelFile, verified: dict[str, list[Any]]) -> bool:
        try:
            stamp = self._stamp(item)
        except OSError:
            return False
        return stamp[0] == item.size and verified.get(str(item.target)) == stamp

    def ready(self) -> bool:
        """Quick check without reading the models: every file is exactly as it was when
        its SHA256 was checked (size and modification time), markers are written."""
        if self._ready:
            return True
        verified = self._verified()
        if not all(self._trusted(item, verified) for item in self.manifest.files):
            return False
        for path, content in self.manifest.markers:
            try:
                if (self.root / path).read_text(encoding="utf-8") != content:
                    return False
            except (OSError, UnicodeDecodeError):
                return False
        self._ready = True
        return True

    def verify(self) -> bool:
        """Full check of every file's SHA256 (slow: reads all models)."""
        try:
            return all(_sha256(self.root / f.target) == f.sha256 for f in self.manifest.files)
        except OSError:
            return False

    def free_bytes(self) -> int:
        probe = self.root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return int(self._free(probe))
        except OSError:
            return 0

    # --- state ------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state["phase"] == "idle" and self.ready():
            state.update(phase="done", done_bytes=state["total_bytes"])
        return state

    def _set(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    # --- download ---------------------------------------------------------------------

    def start(self) -> None:
        """Start or resume the download in a background thread (no-op if running)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._state.update(phase="running", error=None)
            self._thread = threading.Thread(target=self._run, name="models", daemon=True)
            self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            self.download()
        except DownloadError as exc:
            self._set(phase="error", error=exc.code)
        except Exception:  # noqa: BLE001 - only a code goes out, never the message
            self._set(phase="error", error=WRITE_FAILED)
        else:
            self._set(phase="done", error=None, done_bytes=self.manifest.total_bytes)

    def _have(self, item: ModelFile) -> int:
        """Bytes already on disk for this file (final or partial)."""
        final = self.root / item.target
        with contextlib.suppress(OSError):
            if final.stat().st_size == item.size:
                return item.size
        with contextlib.suppress(OSError):
            return min(_part(final).stat().st_size, item.size)
        return 0

    def download(self) -> None:
        """Download every missing file (blocking). Raises DownloadError with a code."""
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise DownloadError(WRITE_FAILED) from None
        verified = self._verified()
        for item in self.manifest.files:
            final = self.root / item.target
            if final.exists() and not self._trusted(item, verified):
                # Changed since its check (or never checked): keep it only if it still matches.
                try:
                    if _sha256(final) != item.sha256:
                        final.unlink()
                except OSError:
                    raise DownloadError(WRITE_FAILED) from None
        have = {item: self._have(item) for item in self.manifest.files}
        missing = sum(item.size - have[item] for item in self.manifest.files)
        if missing and missing + HEADROOM_BYTES > self.free_bytes():
            raise DownloadError(NO_SPACE)
        done = sum(have.values())
        self._set(done_bytes=done)
        opener = urllib.request.build_opener(_RestrictedRedirects(self._allowed, self._insecure_ok))
        for item in self.manifest.files:
            if have[item] == item.size and (self.root / item.target).exists():
                continue
            done = self._fetch(opener, item, done - have[item])
        self._set(phase="verifying")
        for path, content in self.manifest.markers:
            _write_atomic(self.root / path, content.encode("utf-8"))
        # Every file now has its final name only after a SHA256 check: record them.
        try:
            stamps = {str(f.target): self._stamp(f) for f in self.manifest.files}
        except OSError:
            raise DownloadError(WRITE_FAILED) from None
        _write_atomic(self.root / VERIFIED, json.dumps(stamps).encode("utf-8"))
        if not self.ready():
            raise DownloadError(CHECKSUM)
        self._set(phase="done", error=None, done_bytes=self.manifest.total_bytes)

    def _url(self, item: ModelFile) -> str:
        path = urllib.parse.quote(item.path)
        return f"{self._base_url}/{item.repo}/resolve/{item.revision}/{path}"

    def _fetch(self, opener: urllib.request.OpenerDirector, item: ModelFile, base: int) -> int:
        """Download one file, resuming its `.part`. Returns the new total of done bytes."""
        final = self.root / item.target
        part = _part(final)
        try:
            final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            offset = part.stat().st_size if part.exists() else 0
        except OSError:
            raise DownloadError(WRITE_FAILED) from None
        if offset > item.size:
            part.unlink(missing_ok=True)
            offset = 0
        url = self._url(item)
        if not _host_allowed(url, self._allowed, self._insecure_ok):
            raise DownloadError(SERVER_ERROR)
        if offset < item.size:
            request = urllib.request.Request(url, headers={"User-Agent": "transcribator"})  # noqa: S310
            if offset:
                request.add_header("Range", f"bytes={offset}-")
            try:
                with opener.open(request, timeout=TIMEOUT_S) as response:
                    if offset and response.status != 206:
                        offset = 0  # the server ignored Range: start over
                    mode = "ab" if offset else "wb"
                    with part.open(mode) as out:
                        written = offset
                        self._set(done_bytes=base + written)
                        # read1: whatever has arrived, so a dropped connection keeps the
                        # bytes received so far for the next resume.
                        while chunk := response.read1(CHUNK):
                            written += len(chunk)
                            if written > item.size:
                                raise DownloadError(CHECKSUM)
                            out.write(chunk)
                            self._set(done_bytes=base + written)
                    if written < item.size:
                        raise DownloadError(OFFLINE)  # connection dropped: keep the .part
            except DownloadError:
                raise
            except urllib.error.HTTPError:
                raise DownloadError(SERVER_ERROR) from None
            except ssl.SSLError:
                raise DownloadError(SERVER_ERROR) from None  # TLS trouble, not the disk
            except urllib.error.URLError as exc:
                code = SERVER_ERROR if isinstance(exc.reason, ssl.SSLError) else OFFLINE
                raise DownloadError(code) from None
            except (
                http.client.HTTPException,
                TimeoutError,
                ConnectionError,
            ):
                raise DownloadError(OFFLINE) from None
            except OSError as exc:
                code = NO_SPACE if exc.errno == errno.ENOSPC else WRITE_FAILED
                raise DownloadError(code) from None
        try:
            ok = part.stat().st_size == item.size and _sha256(part) == item.sha256
            if not ok:
                part.unlink(missing_ok=True)
                raise DownloadError(CHECKSUM)
            os.replace(part, final)
        except OSError:
            raise DownloadError(WRITE_FAILED) from None
        return base + item.size


def _part(final: Path) -> Path:
    return final.with_name(final.name + ".part")


def _write_atomic(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        raise DownloadError(WRITE_FAILED) from None
