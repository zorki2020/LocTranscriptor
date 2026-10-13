#!/usr/bin/env python3
"""Builds src/transcribator/model_manifest.json: the exact model files the app downloads.

Developer tool, uses the network (huggingface.co API). For each model repository pinned to a
commit it lists the files the app needs with their size and SHA256. Large (LFS) files take
the SHA256 from the Hugging Face API; small files are downloaded and hashed. With `--local`
the hashes are also checked against model files already on this Mac (downloaded earlier by
FluidAudio).

    scripts/make_model_manifest.py [--local ASR_DIR DIARIZER_DIR] \
        > src/transcribator/model_manifest.json

Review the diff of the manifest before committing: it decides what the app trusts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://huggingface.co/api/models"
RESOLVE = "https://huggingface.co"

# What FluidAudio v0.17.7 loads (checked offline with only these files present):
# Parakeet TDT v3 with the int8-v2 encoder, and offline diarization.
REPOS: list[dict[str, Any]] = [
    {
        "repo": "FluidInference/parakeet-tdt-0.6b-v3-coreml",
        "revision": "7dd20fe6b1797d35f5e3307e8b1732d9a178edfe",
        "target": "parakeet-tdt-0.6b-v3",
        "marker": False,
        "include": [
            "config.json",
            "parakeet_v3_vocab.json",
            "parakeet_vocab.json",
            "Decoder.mlmodelc/",
            "Encoder_v2.mlmodelc/",
            "JointDecisionv3.mlmodelc/",
            "Preprocessor.mlmodelc/",
        ],
        # In the local copy but not used: the plain int8 encoder (we use int8-v2).
        "unused": ["Encoder.mlmodelc/"],
    },
    {
        "repo": "FluidInference/speaker-diarization-coreml",
        "revision": "df2625ac79a7ac6b65ad868fee6d80f320da4232",
        # FluidAudio has no flag for the diarization model directory: the app points its
        # home directory (CFFIXED_USER_HOME) at models/home.
        "target": "home/Library/Application Support/FluidAudio/Models/speaker-diarization",
        "marker": True,
        "include": [
            "config.json",
            "plda-parameters.json",
            "provenance.json",
            "Embedding.mlmodelc/",
            "FBank.mlmodelc/",
            "PldaRho.mlmodelc/",
            "Segmentation.mlmodelc/",
            "xvector-transform.json",
        ],
    },
]


def _get(url: str) -> bytes:
    if not url.startswith("https://huggingface.co/"):
        raise ValueError("only huggingface.co over https")
    request = urllib.request.Request(url, headers={"User-Agent": "transcribator-manifest"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - checked above
        return bytes(response.read())


def _tree(repo: str, revision: str) -> list[dict[str, Any]]:
    url = f"{API}/{repo}/tree/{revision}?recursive=true"
    entries: list[dict[str, Any]] = json.loads(_get(url))
    return [e for e in entries if e.get("type") == "file"]


def _wanted(path: str, include: list[str]) -> bool:
    return any(path == item or (item.endswith("/") and path.startswith(item)) for item in include)


def build(local: dict[str, Path]) -> dict[str, Any]:
    repos = []
    for spec in REPOS:
        files = []
        for entry in sorted(_tree(spec["repo"], spec["revision"]), key=lambda e: e["path"]):
            path = entry["path"]
            if not _wanted(path, spec["include"]):
                continue
            lfs = entry.get("lfs")
            if lfs and lfs.get("oid"):
                sha = str(lfs["oid"])
                size = int(lfs.get("size", entry["size"]))
            else:
                quoted = urllib.parse.quote(path)
                data = _get(f"{RESOLVE}/{spec['repo']}/resolve/{spec['revision']}/{quoted}")
                sha = hashlib.sha256(data).hexdigest()
                size = len(data)
            files.append({"path": path, "size": size, "sha256": sha})
        missing = [i for i in spec["include"] if not any(_wanted(f["path"], [i]) for f in files)]
        if missing:
            raise SystemExit(f"{spec['repo']}: not found at the pinned revision: {missing}")
        if spec["target"] in local:
            _check_local(local[spec["target"]], files, spec.get("unused", []))
        repos.append(
            {k: spec[k] for k in ("repo", "revision", "target", "marker")} | {"files": files}
        )
    total = sum(f["size"] for r in repos for f in r["files"])
    return {"version": 1, "total_bytes": total, "repos": repos}


def _check_local(root: Path, files: list[dict[str, Any]], unused: list[str]) -> None:
    """The local copy works with FluidAudio: it must match the manifest file for file,
    with nothing extra (a missing entry in `include` would break the app offline)."""
    present = {
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and p.name != ".fluidaudio-revision"
    }
    extra = sorted(p for p in present - {f["path"] for f in files} if not _wanted(p, unused))
    if extra:
        raise SystemExit(f"local files missing from the manifest: {extra}")
    for item in files:
        path = root / item["path"]
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != item["sha256"]:
            raise SystemExit(f"local file differs from the pinned revision: {item['path']}")
    print(f"local copy matches: {len(files)} files", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--local", nargs=2, type=Path, metavar=("ASR_DIR", "DIARIZER_DIR"))
    args = parser.parse_args()
    local = {}
    if args.local:
        local = {REPOS[0]["target"]: args.local[0], REPOS[1]["target"]: args.local[1]}
    json.dump(build(local), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
