# Third-party notices

Transcribator's own code is MIT-licensed (see `LICENSE`). It relies on the components
below. Only `htmx.min.js` is shipped inside this repository; everything else is installed
on your Mac by the setup steps in the README and keeps its own license.

## Speech models (downloaded by `transcribator models download`)

| Component | Used for | License | What it requires |
|---|---|---|---|
| [NVIDIA Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), CoreML conversion by FluidInference ([parakeet-tdt-0.6b-v3-coreml](https://huggingface.co/FluidInference/parakeet-tdt-0.6b-v3-coreml)) | Speech recognition | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) | Attribution to NVIDIA (this notice); the weights were modified (converted and quantized for CoreML) by FluidInference |
| [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) pipeline (segmentation, WeSpeaker embeddings, VBx), CoreML conversion by FluidInference ([speaker-diarization-coreml](https://huggingface.co/FluidInference/speaker-diarization-coreml)) | Speaker diarization | [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) (pyannote Community-1); see the model cards for the converted weights | Attribution to pyannote and its authors (this notice); converted to CoreML by FluidInference |

Model files are not redistributed by this project. Check the model cards for the current
terms before using the models commercially.

## Programs and libraries

| Component | Version | License | Shipped here |
|---|---|---|---|
| [FluidAudio](https://github.com/FluidInference/FluidAudio) (`fluidaudiocli`, built from source by `scripts/install_fluidaudio.sh`) | v0.17.7 | Apache-2.0 | No |
| [htmx](https://htmx.org) (`src/transcribator/web/static/htmx.min.js`, unmodified, SHA-256 `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717`) | 2.0.11 | 0BSD | Yes |
| [FastAPI](https://github.com/fastapi/fastapi) | 0.143 | MIT | No |
| [Starlette](https://github.com/encode/starlette) | 1.7 | BSD-3-Clause | No |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.54 | BSD-3-Clause | No |
| [Jinja2](https://github.com/pallets/jinja) and MarkupSafe | 3.1 / 3.0 | BSD-3-Clause | No |
| [Pydantic](https://github.com/pydantic/pydantic) and pydantic-core | 2.14 / 2.50 | MIT | No |
| AnyIO, h11, annotated-types, typing-inspection, annotated-doc | — | MIT | No |
| Click, idna | — | BSD-3-Clause | No |
| typing-extensions | — | PSF-2.0 | No |
| opentelemetry-api (pulled in by FastAPI; API only, no exporter, sends nothing) | 1.45 | Apache-2.0 | No |
| [FFmpeg](https://ffmpeg.org) (installed with Homebrew) | — | LGPL/GPL depending on build | No |

Exact Python versions are pinned in `uv.lock`.
