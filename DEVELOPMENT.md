# Developing LocTranscriptor

The Mac app is the easy way to use LocTranscriptor ([README](README.md)). This page is for running the service from source and for working on the code. From source it keeps its development name, `transcribator`. It runs in your browser at `http://127.0.0.1:8765` and keeps its data in `~/Library/Application Support/transcribator`, apart from the app.

## Requirements

- Mac with Apple Silicon (M1 or newer), macOS 15 Sequoia or newer
- About 3 GB of free disk space (models ~1 GB, build files ~0.7 GB)
- An internet connection once, during installation, to download tools and models

## Install from source

Run these in Terminal, one block at a time.

**1. Command Line Tools with the macOS 15 SDK** (skip if `xcrun --show-sdk-version` prints 15 or higher)

```bash
xcode-select --install
```

If that says the tools are already installed but the SDK is older than 15, make Software Update offer the newer tools and install them:

```bash
touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
softwareupdate --list | grep -A1 "Command Line Tools"
sudo softwareupdate -i "Command Line Tools for Xcode-26.x"   # use the exact label from the list
rm /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
```

**2. Homebrew, FFmpeg and uv**

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install ffmpeg uv
```

**3. Swift 6.2** (needed to build FluidAudio; installs into your home folder, no admin rights)

```bash
curl -O https://download.swift.org/swiftly/darwin/swiftly.pkg
pkgutil --check-signature swiftly.pkg   # should say: Developer ID Installer: Swift Open Source
installer -pkg swiftly.pkg -target CurrentUserHomeDirectory
~/.swiftly/bin/swiftly init --quiet-shell-followup
~/.swiftly/bin/swiftly install 6.2
```

**4. The transcribator service**

```bash
git clone https://github.com/zorki2020/transcribator.git ~/transcribator
cd ~/transcribator
scripts/install_fluidaudio.sh        # builds FluidAudio v0.17.7 from source, ~3 minutes
uv sync                              # Python environment
uv run transcribator models download # speech models, ~1 GB, the only step that downloads models
```

**5. Start at login and the menu bar icon**

```bash
scripts/build_menubar.sh             # builds ~/Applications/Transcribator.app
uv run transcribator autostart install
```

macOS may show "Background items added". That is expected. The service is now running and a waveform icon appears in the menu bar.

To run it only when you need it, skip step 5 and start it by hand with `cd ~/transcribator && uv run transcribator serve`. Ctrl+C stops it.

## Use

1. Click the waveform icon in the menu bar → **Open LocTranscriptor**, or go to <http://127.0.0.1:8765>.
2. On **Add recording**, choose the recording language, then click **Choose file** (opens a Finder dialog, and the file is read in place) or drag a file onto the page. mp3, mp4, m4a, wav and anything else FFmpeg can read with an audio track will do.
3. Watch the queue: *Queued* → *Processing* → *Done*. Processing continues with the browser tab closed. One recording is processed at a time.
4. Open the meeting from the queue or from **Archive**. Click a speaker to rename them everywhere, edit the title and date in place, **Download .md**, or delete the meeting with everything stored about it.
5. **Archive** lists all meetings by date. The search box (press `/`) searches every transcript, ignores case and jumps to the exact line.

The menu bar icon shows the queue state, stops and starts the service and toggles **Start at login**. Quitting the icon does not stop the service.

### Command line

```bash
uv run transcribator process recording.mp4 --language en   # transcript next to the file
uv run transcribator bench recording.mp4 --language en     # timings and memory only
uv run transcribator autostart status
```

## Where things are

| What | Where |
|---|---|
| Meetings database and temporary uploads | `~/Library/Application Support/transcribator/` (folder 700, files 600) |
| Service log (address and error codes only) | `~/Library/Logs/transcribator/serve.log` |
| FluidAudio binary and speech model | `~/.local/share/transcribator-tools/fluidaudio/` |
| Diarization model | `~/Library/Application Support/FluidAudio/Models/speaker-diarization/` |
| Login items | `~/Library/LaunchAgents/local.transcribator*.plist` |

`TRANSCRIBATOR_TOOLS_DIR` moves the FluidAudio folder (set it for both `scripts/install_fluidaudio.sh` and the service).

Settings are environment variables: `TRANSCRIBATOR_UI_LANG=en|ru` (interface), `TRANSCRIBATOR_LANGUAGE=en|ru` (default recording language), `TRANSCRIBATOR_DATA_DIR` (database location). `autostart install` copies the current values into the login item, so run it again after changing them.

## Building the Mac app

`scripts/build_app.sh` builds `dist/LocTranscriptor.dmg` and its SHA-256 (Apple Silicon, macOS 15+, Swift 6.2). It downloads a pinned python-build-standalone and the Python wheels on the first run, builds FluidAudio, the window app (`macos/App`) and the audio helper (`macos/AudioTool`), signs everything ad hoc and checks the result.

## Development

```bash
uv sync
uv run pytest            # tests use synthetic audio and fake models only
uv run ruff check . && uv run mypy src
cd macos/App && swift test
```

Code layout:
- `src/transcribator/pipeline/`: audio, diarization and word alignment, Markdown
- `src/transcribator/backends/fluidaudio.py`: calls `fluidaudiocli`
- `src/transcribator/storage/`: SQLite, FTS5, job queue
- `src/transcribator/worker/`: one job at a time, sandboxed
- `src/transcribator/web/`: FastAPI, Jinja2 and htmx
- `src/transcribator/modelstore.py`: model download for the app
- `macos/App/`: the Mac app (Swift, WKWebView)
- `macos/AudioTool/`: AVFoundation audio helper
- `macos/MenuBar/`: menu bar icon for the source install

## Privacy model

What is enforced:

- The server binds only to `127.0.0.1`. Requests with another `Host` are rejected (DNS rebinding). State-changing requests need the page's `Origin` and a CSRF token. A strict Content-Security-Policy forbids any external script, style or font. In the app every request also needs a per-launch access key.
- Speech recognition runs in a separate process under `sandbox-exec` with all networking denied. The models cannot reach the internet.
- No request log, no telemetry. Logs and errors contain codes, never file names or text.
- Temporary audio is deleted on every exit path, including crashes (cleaned on the next start).

What it does not protect against: someone with access to your unlocked Mac. Turn on FileVault and keep the data folder out of iCloud or Dropbox sync.

## Uninstall the source install

```bash
cd ~/transcribator && uv run transcribator autostart remove
rm -rf ~/Applications/Transcribator.app ~/.local/share/transcribator-tools ~/Library/Logs/transcribator
rm -rf "$HOME/Library/Application Support/transcribator"   # deletes all your transcripts
rm -rf ~/transcribator
```
