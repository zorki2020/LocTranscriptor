#!/bin/bash
# Builds and installs `fluidaudiocli` (FluidAudio, pinned release) from source.
# Transcribator calls this program for speaker diarization and speech recognition.
#
#   scripts/install_fluidaudio.sh [--force]
#
# Layout (default TRANSCRIBATOR_TOOLS_DIR=~/.local/share/transcribator-tools/fluidaudio):
#   src/               FluidAudio sources at the pinned tag and commit
#   bin/fluidaudiocli  the release binary used by the app
#   models/            ASR models, filled later by `uv run transcribator models download`
#
# Requirements: Apple Silicon Mac, macOS 15+, Command Line Tools with macOS SDK 15+,
# Swift 6.2+ (via swiftly), git. Network is used only to clone the sources and for the
# SwiftPM step of the build. FluidAudio v0.17.7 has no Swift package dependencies; its only
# downloaded artifact is the NemoTextProcessing binary, pinned by checksum in Package.swift.
#
# The build uses `--disable-default-traits`, so the prebuilt NemoTextProcessing (Rust)
# binary that SwiftPM downloads is not linked into `fluidaudiocli`; this is verified below.
#
# Without --force an existing binary is left as is.
set -euo pipefail

FLUIDAUDIO_REPO="https://github.com/FluidInference/FluidAudio.git"
FLUIDAUDIO_TAG="v0.17.7"
FLUIDAUDIO_COMMIT="503b4bd1bbf7220882de39fe8ae6716aae4132da"
MIN_MACOS_MAJOR=15
MIN_SDK_MAJOR=15
MIN_SWIFT="6.2"

DEFAULT_TOOLS_DIR="$HOME/.local/share/transcribator-tools/fluidaudio"
tools_dir="${TRANSCRIBATOR_TOOLS_DIR:-$DEFAULT_TOOLS_DIR}"
src_dir="$tools_dir/src"
bin_dir="$tools_dir/bin"
bin_path="$bin_dir/fluidaudiocli"

usage() {
    echo "usage: scripts/install_fluidaudio.sh [--force]" >&2
    echo "  --force   rebuild and replace an existing binary" >&2
    echo "  env TRANSCRIBATOR_TOOLS_DIR   install dir (default: $DEFAULT_TOOLS_DIR)" >&2
}

force=0
for arg in "$@"; do
    case "$arg" in
        --force) force=1 ;;
        -h | --help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

die() {
    echo "error: $*" >&2
    exit 1
}

info() {
    echo "==> $*"
}

# version_ge A B: true if dotted version A >= B (compares major.minor[.patch] numerically).
version_ge() {
    local IFS=.
    local -a a=($1) b=($2)
    local i x y
    for i in 0 1 2; do
        x="${a[i]:-0}"
        y="${b[i]:-0}"
        x="${x//[^0-9]/}"
        y="${y//[^0-9]/}"
        x="${x:-0}"
        y="${y:-0}"
        if ((10#$x > 10#$y)); then return 0; fi
        if ((10#$x < 10#$y)); then return 1; fi
    done
    return 0
}

if [ -x "$bin_path" ] && [ "$force" -eq 0 ]; then
    echo "fluidaudiocli is already installed: $bin_path"
    echo "Run with --force to rebuild it."
    exit 0
fi

# --- Prerequisites -----------------------------------------------------------

info "Checking prerequisites"

[ "$(uname -s)" = "Darwin" ] || die "macOS is required (FluidAudio uses CoreML)."
[ "$(uname -m)" = "arm64" ] || die "an Apple Silicon Mac (arm64) is required."

macos_version="$(sw_vers -productVersion)"
if ! version_ge "$macos_version" "$MIN_MACOS_MAJOR"; then
    die "macOS $MIN_MACOS_MAJOR or newer is required, this Mac runs $macos_version.
  Fix: update macOS in System Settings > General > Software Update."
fi

command -v git >/dev/null 2>&1 || die "git not found.
  Fix: install the Command Line Tools: xcode-select --install"

if ! sdk_version="$(xcrun --show-sdk-version 2>/dev/null)"; then
    die "Command Line Tools (xcrun, macOS SDK) not found.
  Fix: xcode-select --install"
fi
if ! version_ge "$sdk_version" "$MIN_SDK_MAJOR"; then
    cat >&2 <<EOF
error: macOS SDK $MIN_SDK_MAJOR or newer is required, the active SDK is $sdk_version.
  FluidAudio needs CoreML APIs (MLState) that older SDKs lack.
  Fix: install Command Line Tools 16.4 or 26 (or newer). If Software Update does not
  offer them, ask softwareupdate to list Command Line Tools explicitly:

    touch /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress
    softwareupdate --list
    # pick the newest "Command Line Tools for Xcode-..." label from the list, e.g.:
    sudo softwareupdate --install "Command Line Tools for Xcode-16.4"
    rm /tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress

  If Xcode is installed instead, select it: sudo xcode-select -s /Applications/Xcode.app
EOF
    exit 1
fi

# Prefer swiftly (it does not touch the system toolchain), fall back to `swift` in PATH.
swiftly_bin=""
if command -v swiftly >/dev/null 2>&1; then
    swiftly_bin="$(command -v swiftly)"
elif [ -x "$HOME/.swiftly/bin/swiftly" ]; then
    swiftly_bin="$HOME/.swiftly/bin/swiftly"
fi

swift_cmd=()
swift_version=""
if [ -n "$swiftly_bin" ]; then
    v="$("$swiftly_bin" run swift --version 2>/dev/null | sed -n 's/.*Swift version \([0-9][0-9.]*\).*/\1/p' | head -n 1 || true)"
    if [ -n "$v" ] && version_ge "$v" "$MIN_SWIFT"; then
        swift_cmd=("$swiftly_bin" run swift)
        swift_version="$v"
    fi
fi
if [ "${#swift_cmd[@]}" -eq 0 ] && command -v swift >/dev/null 2>&1; then
    v="$(swift --version 2>/dev/null | sed -n 's/.*Swift version \([0-9][0-9.]*\).*/\1/p' | head -n 1 || true)"
    if [ -n "$v" ] && version_ge "$v" "$MIN_SWIFT"; then
        swift_cmd=(swift)
        swift_version="$v"
    fi
fi
if [ "${#swift_cmd[@]}" -eq 0 ]; then
    cat >&2 <<EOF
error: Swift $MIN_SWIFT or newer is required (only 6.2+ can disable the bundled
  NemoTextProcessing binary). Command Line Tools ship an older Swift.
  Fix: install the toolchain with swiftly (https://www.swift.org/install/macos/):

    curl -O https://download.swift.org/swiftly/darwin/swiftly.pkg
    installer -pkg swiftly.pkg -target CurrentUserHomeDirectory
    ~/.swiftly/bin/swiftly init --quiet-shell-followup
    ~/.swiftly/bin/swiftly install 6.2

  Then run this script again.
EOF
    exit 1
fi

echo "    macOS $macos_version, SDK $sdk_version, Swift $swift_version (${swift_cmd[*]})"

# --- Sources -----------------------------------------------------------------

mkdir -p "$tools_dir"

if [ -e "$src_dir" ]; then
    [ -d "$src_dir/.git" ] || die "$src_dir exists but is not a git checkout.
  Fix: move it away or delete it, then run this script again."
    if [ -n "$(git -C "$src_dir" status --porcelain --untracked-files=no)" ]; then
        die "$src_dir has local changes; refusing to build modified sources.
  Fix: delete $src_dir and run this script again."
    fi
    if [ "$(git -C "$src_dir" rev-parse HEAD)" != "$FLUIDAUDIO_COMMIT" ]; then
        info "Fetching FluidAudio $FLUIDAUDIO_TAG into the existing checkout"
        git -C "$src_dir" fetch --depth 1 "$FLUIDAUDIO_REPO" "refs/tags/$FLUIDAUDIO_TAG:refs/tags/$FLUIDAUDIO_TAG"
        git -C "$src_dir" -c advice.detachedHead=false checkout --detach "refs/tags/$FLUIDAUDIO_TAG"
    else
        info "Reusing FluidAudio sources in $src_dir"
    fi
else
    info "Cloning FluidAudio $FLUIDAUDIO_TAG"
    git -c advice.detachedHead=false clone --depth 1 --branch "$FLUIDAUDIO_TAG" "$FLUIDAUDIO_REPO" "$src_dir"
fi

head_commit="$(git -C "$src_dir" rev-parse HEAD)"
if [ "$head_commit" != "$FLUIDAUDIO_COMMIT" ]; then
    die "FluidAudio $FLUIDAUDIO_TAG resolved to commit $head_commit,
  expected $FLUIDAUDIO_COMMIT. The tag may have been moved; not building."
fi
echo "    commit $head_commit verified"

# --- Build -------------------------------------------------------------------

info "Building fluidaudiocli (release, default traits disabled); this takes a few minutes"
build_args=(build -c release --product fluidaudiocli --disable-default-traits --package-path "$src_dir")
"${swift_cmd[@]}" "${build_args[@]}"
built_dir="$("${swift_cmd[@]}" build -c release --disable-default-traits --package-path "$src_dir" --show-bin-path)"
built="$built_dir/fluidaudiocli"
[ -x "$built" ] || die "build finished but $built was not found."

nemo_symbols="$(nm "$built" 2>/dev/null | grep -c nemo_normalize || true)"
if [ "$nemo_symbols" != "0" ]; then
    die "the built binary links NemoTextProcessing ($nemo_symbols nemo_normalize symbols).
  Expected none with --disable-default-traits; not installing."
fi
echo "    no NemoTextProcessing symbols linked"

# --- Install -----------------------------------------------------------------

mkdir -p "$bin_dir"
tmp_bin="$(mktemp "$bin_dir/.fluidaudiocli.XXXXXX")"
trap 'rm -f "$tmp_bin"' EXIT
cp "$built" "$tmp_bin"
chmod 755 "$tmp_bin"
mv -f "$tmp_bin" "$bin_path"
trap - EXIT

sha="$(shasum -a 256 "$bin_path" | cut -d ' ' -f 1)"

echo
echo "Installed: $bin_path"
echo "SHA256:    $sha"
echo
if [ "$tools_dir" != "$DEFAULT_TOOLS_DIR" ]; then
    echo "Non-default install dir: point the app at the binary with"
    echo "  export TRANSCRIBATOR_FLUIDAUDIO_CLI=\"$bin_path\""
    echo
fi
echo "Next step: download the models (the only command that uses the network):"
echo "  uv run transcribator models download"
