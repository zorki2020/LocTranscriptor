# Third-party notices

LocTranscriptor's own code, including the `transcribator` Python package, is MIT-licensed (see `LICENSE`). It relies on the components below, each under its own license. Only `htmx.min.js` and the Radix Colors stylesheets are stored in this repository. The Mac app (`LocTranscriptor.app`, built by `scripts/build_app.sh`) ships the programs and libraries marked "In the app". This file and `LICENSE` are inside the app (`Contents/Resources`). The installation from source gets them from PyPI, Homebrew and GitHub instead.

## Speech models (downloaded on first use)

The app downloads them once, when you press "Download" on its first screen, from
huggingface.co at pinned commits (`src/transcribator/model_manifest.json`, SHA-256 checked).
The installation from source downloads them with `transcribator models download`.

| Component | Used for | License | What it requires |
|---|---|---|---|
| [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), CoreML conversion by FluidInference ([parakeet-tdt-0.6b-v3-coreml](https://huggingface.co/FluidInference/parakeet-tdt-0.6b-v3-coreml)) | Speech recognition | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) | Attribution to NVIDIA (this notice); the weights were modified (converted and quantized for CoreML) by FluidInference |
| [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) pipeline (segmentation, WeSpeaker embeddings, VBx), CoreML conversion by FluidInference ([speaker-diarization-coreml](https://huggingface.co/FluidInference/speaker-diarization-coreml)) | Speaker diarization | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) (pyannote Community-1); see the model cards for the converted weights | Attribution to pyannote and its authors (this notice); converted to CoreML by FluidInference |

Model files are not redistributed by this project: they are downloaded from Hugging Face.
Check the model cards for the current terms before using the models commercially.

## Programs and libraries

| Component | Version | License | In the repository | In the app |
|---|---|---|---|---|
| [FluidAudio](https://github.com/FluidInference/FluidAudio) (`fluidaudiocli`, built from source by `scripts/install_fluidaudio.sh`, unmodified) | v0.17.7 | Apache-2.0 | No | Yes |
| [CPython](https://www.python.org) from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) (`install_only_stripped`, release 20261009) | 3.12.15 | PSF-2.0; statically linked libraries include OpenSSL (Apache-2.0), SQLite (public domain), libffi (MIT), zlib, xz, bzip2, mpdecimal and libedit under their own permissive licenses (see [python-build-standalone licensing](https://gregoryszorc.com/docs/python-build-standalone/main/running.html#licensing)) | No | Yes |
| [htmx](https://htmx.org) (`src/transcribator/web/static/htmx.min.js`, unmodified, SHA-256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`) | 2.0.11 | 0BSD | Yes | Yes |
| [Radix Colors](https://github.com/radix-ui/colors) (`src/transcribator/web/static/vendor/radix-colors/`: sand, indigo, red, green, amber scales, light and dark, unmodified; tarball SHA-256 `f6cec1ff1fcc58a45eeefc25d0e6aa1f1659845dbd198963617a86e43d21f3d1`) | 3.0.0 | MIT, Copyright (c) 2021 Radix | Yes | Yes |
| [FastAPI](https://github.com/fastapi/fastapi) | 0.143 | MIT | No | Yes |
| [Starlette](https://github.com/encode/starlette) | 1.7 | BSD-3-Clause | No | Yes |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.54 | BSD-3-Clause | No | Yes |
| [Jinja2](https://github.com/pallets/jinja) and MarkupSafe | 3.1 / 3.0 | BSD-3-Clause | No | Yes |
| [Pydantic](https://github.com/pydantic/pydantic) and pydantic-core | 2.14 / 2.50 | MIT | No | Yes |
| AnyIO, h11, annotated-types, typing-inspection, annotated-doc | — | MIT | No | Yes |
| Click, idna | — | BSD-3-Clause | No | Yes |
| typing-extensions | — | PSF-2.0 | No | Yes |
| opentelemetry-api (pulled in by FastAPI; API only, no exporter, sends nothing) | 1.45 | Apache-2.0 | No | Yes |
| [FFmpeg](https://ffmpeg.org) (installed with Homebrew; the app uses macOS AVFoundation instead) | — | LGPL/GPL depending on build | No | No |

The full license texts of the Python packages are in their `*.dist-info` folders inside the
app (`Contents/Resources/python/lib/python3.12/site-packages`). The CPython license is in
`Contents/Resources/python/lib/python3.12/LICENSE.txt`.

Exact Python versions are pinned in `uv.lock`.
