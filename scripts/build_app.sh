#!/bin/bash
# Builds LocTranscriptor.app (Apple Silicon, macOS 15+) and dist/LocTranscriptor.dmg with its SHA256.
#
#   scripts/build_app.sh [--allow-dirty]
#
# What goes into the app:
#   Contents/MacOS/LocTranscriptor       the window (macos/App, Swift + AppKit + WebKit)
#   Contents/Helpers/fluidaudiocli       FluidAudio, built from the pinned source
#   Contents/Helpers/transcribator-audio AVFoundation helper (macos/AudioTool), no ffmpeg
#   Contents/Resources/python/           CPython from python-build-standalone (pinned URL and
#                                        SHA256) with the dependencies from uv.lock (hashes
#                                        checked) and this project; .pyc compiled ahead
#
# There is no Apple Developer ID: every Mach-O file is signed ad hoc, inside out. Users open
# the app once with System Settings > Privacy & Security > Open Anyway.
#
# The build checks itself on a read-only copy: the service starts with an access key, answers
# only with it, the helpers run, and nothing gets written into the bundle.
#
# Network: python-build-standalone and PyPI wheels on first build (cached in build/), the
# FluidAudio sources on first build. The app itself downloads nothing until the user asks
# for the models. Output: build/ (download cache) and dist/, both ignored by git.
#
# Compilers write build paths into binaries (resource bundle paths, rpaths, .pyc file
# names). So everything is compiled in a neutral work directory under $TMPDIR (override:
# TRANSCRIBATOR_BUILD_DIR), and the build fails if the home directory path shows up anywhere
# in the bundle: the image is published.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
APP_NAME="LocTranscriptor"
BUNDLE_ID="app.loctranscriptor.mac"
MIN_MACOS="15.0"

PBS_RELEASE="20261009"
PY_VERSION="3.12.15"
PBS_FILE="cpython-${PY_VERSION}+${PBS_RELEASE}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_FILE//+/%2B}"
PBS_SHA256="233f8e15255b3e1d1fd47ea5dd5230dfed803f371138e896e54d98c60ae19f6f"

ALLOW_DIRTY=0
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) ALLOW_DIRTY=1 ;;
    -h | --help) sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "usage: scripts/build_app.sh [--allow-dirty]" >&2; exit 2 ;;
  esac
done

die() { echo "Build stopped: $*" >&2; exit 1; }
step() { echo "==> $*"; }

CHECK_PID=""
MOUNT=""
cleanup() {
  [ -n "$CHECK_PID" ] && kill "$CHECK_PID" 2>/dev/null || true
  [ -n "$MOUNT" ] && hdiutil detach -quiet "$MOUNT" 2>/dev/null || true
}
trap cleanup EXIT

# --- prerequisites -----------------------------------------------------------------------

[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || die "needs an Apple Silicon Mac"
macos="$(sw_vers -productVersion)"
[ "${macos%%.*}" -ge 15 ] || die "needs macOS 15 or newer (this Mac: $macos)"
for tool in uv swiftc codesign hdiutil shasum curl git; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
if [ -x "$HOME/.swiftly/bin/swiftly" ]; then
  SWIFT=("$HOME/.swiftly/bin/swiftly" run swift)
else
  SWIFT=(swift)
fi
DIRTY=0
if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
  if [ "$ALLOW_DIRTY" = 0 ]; then
    git -C "$ROOT" status --short >&2
    die "uncommitted changes (commit them or pass --allow-dirty)"
  fi
  DIRTY=1
fi

VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/pyproject.toml" | head -n 1)"
[ -n "$VERSION" ] || die "no version in pyproject.toml"
BUILD_NUMBER="$(git -C "$ROOT" rev-list --count HEAD)"
COMMIT="$(git -C "$ROOT" rev-parse --short HEAD)"
if [ "$DIRTY" = 1 ]; then
  echo "WARNING: building from uncommitted changes: for testing only, do not publish" >&2
  COMMIT="$COMMIT-dirty"
fi
step "$APP_NAME $VERSION ($BUILD_NUMBER, $COMMIT)"

tmp_root="${TMPDIR:-/tmp}"
WORK="${TRANSCRIBATOR_BUILD_DIR:-${tmp_root%/}/transcribator-app-build}"
WORK="${WORK%/}"
case "$WORK" in "$HOME"/*) die "the work directory must be outside the home directory" ;; esac
mkdir -p "$BUILD/cache" "$DIST" "$WORK"
STAGE="$WORK/stage"
APP="$STAGE/$APP_NAME.app"
rm -rf "$STAGE"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Helpers" "$APP/Contents/Resources"
PY="$APP/Contents/Resources/python"

# --- FluidAudio --------------------------------------------------------------------------

step "fluidaudiocli (pinned source, built in the work directory)"
TRANSCRIBATOR_TOOLS_DIR="$WORK/tools" "$ROOT/scripts/install_fluidaudio.sh" >/dev/null
cp "$WORK/tools/bin/fluidaudiocli" "$APP/Contents/Helpers/fluidaudiocli"

# --- Python ------------------------------------------------------------------------------

step "Python $PY_VERSION (python-build-standalone $PBS_RELEASE)"
tarball="$BUILD/cache/$PBS_FILE"
if [ ! -f "$tarball" ]; then
  curl -fsSL --proto '=https' -o "$tarball.tmp" "$PBS_URL"
  mv "$tarball.tmp" "$tarball"
fi
echo "$PBS_SHA256  $tarball" | shasum -a 256 -c --status || {
  rm -f "$tarball"
  die "python-build-standalone checksum mismatch (deleted, run again)"
}
tar -xzf "$tarball" -C "$APP/Contents/Resources"
[ -x "$PY/bin/python3.12" ] || die "unexpected python-build-standalone layout"

LIB="$PY/lib/python3.12"
# Not needed by the service: tests, GUI toolkits, IDLE, pip, headers, static library.
rm -rf "$LIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data} \
  "$LIB"/turtle.py "$LIB"/config-3.12-darwin \
  "$PY"/include "$PY"/share "$PY"/lib/tcl* "$PY"/lib/tk* "$PY"/lib/itcl* "$PY"/lib/thread* \
  "$PY"/lib/libtcl* "$PY"/lib/libtk* "$PY"/lib/pkgconfig "$LIB"/lib-dynload/_tkinter*.so \
  "$LIB"/site-packages/pip "$LIB"/site-packages/pip-*
find "$PY/bin" -mindepth 1 ! -name python3.12 -exec rm -f {} +
ln -s python3.12 "$PY/bin/python3"

step "Dependencies from uv.lock (hashes checked, wheels only)"
(cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  -o "$BUILD/requirements.txt" >/dev/null)
rm -rf "$WORK/wheel"
(cd "$ROOT" && uv build --wheel -o "$WORK/wheel" >/dev/null 2>&1)
pip_install() {
  uv pip install --quiet --python "$PY/bin/python3.12" --system --break-system-packages \
    --no-deps "$@"
}
pip_install --require-hashes --only-binary :all: -r "$BUILD/requirements.txt"
pip_install "$WORK"/wheel/transcribator-*.whl
rm -rf "$LIB"/site-packages/*.dist-info/direct_url.json
find "$PY/bin" -mindepth 1 ! -name python3.12 ! -name python3 -exec rm -f {} +
"$PY/bin/python3.12" -I -c "import transcribator.modelstore as m; m.bundled_manifest()"

step "Bytecode (unchecked-hash: never rewritten at run time)"
"$PY/bin/python3.12" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$PY/lib" >/dev/null

# --- Swift -------------------------------------------------------------------------------

step "Window app (macos/App)"
swiftpm=(-c release --package-path "$ROOT/macos/App" --scratch-path "$WORK/swiftpm")
"${SWIFT[@]}" build "${swiftpm[@]}" --product "$APP_NAME" >/dev/null
bin_dir="$("${SWIFT[@]}" build "${swiftpm[@]}" --show-bin-path)"
cp "$bin_dir/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"

step "transcribator-audio (macos/AudioTool)"
mkdir -p "$WORK/swift-cache"
swiftc -O -swift-version 6 -target "arm64-apple-macos$MIN_MACOS" \
  -module-cache-path "$WORK/swift-cache" \
  -o "$APP/Contents/Helpers/transcribator-audio" "$ROOT/macos/AudioTool/main.swift"

# --- bundle ------------------------------------------------------------------------------

step "Bundle"
sed -e "s/__VERSION__/$VERSION/g" -e "s/__BUILD__/$BUILD_NUMBER/g" \
  "$ROOT/macos/App/Info.plist" > "$APP/Contents/Info.plist"
# Which source the app was built from; "-dirty" marks a test build of uncommitted code.
/usr/libexec/PlistBuddy -c "Add :TranscribatorSourceCommit string $COMMIT" "$APP/Contents/Info.plist"
plutil -lint -s "$APP/Contents/Info.plist" || die "Info.plist is not valid"
[ "$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist")" = "$BUNDLE_ID" ] \
  || die "Info.plist must use the bundle id $BUNDLE_ID"
cp -R "$ROOT/macos/App/Resources/." "$APP/Contents/Resources/"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/"
# The icon is drawn from source (variant a) on every build: no binary icon in git.
swiftc -O -swift-version 5 -module-cache-path "$WORK/swift-cache" \
  -o "$WORK/make_icon" "$ROOT/macos/App/Icon/make_icon.swift"
rm -rf "$WORK/AppIcon.iconset"
"$WORK/make_icon" iconset a "$WORK/AppIcon.iconset" >/dev/null
iconutil -c icns "$WORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
[ -f "$APP/Contents/Resources/AppIcon.icns" ] || die "the app icon was not built"
find "$APP" -name .DS_Store -delete
xattr -cr "$APP"

step "Build paths out of the binaries"
for bin in "$APP/Contents/MacOS/$APP_NAME" "$APP/Contents/Helpers/fluidaudiocli" \
  "$APP/Contents/Helpers/transcribator-audio"; do
  strip -S -x "$bin"
  otool -l "$bin" | awk '/cmd LC_RPATH/ {r=1} r && /path / {print $2; r=0}' | while read -r rpath; do
    case "$rpath" in /usr/* | /System/* | @*) ;; *) install_name_tool -delete_rpath "$rpath" "$bin" ;; esac
  done
done
leaks="$(LC_ALL=C grep -rlF "$HOME" "$APP" || true)"
[ -z "$leaks" ] || die "the home directory path is inside the bundle: $leaks"

step "Ad-hoc signature, inside out"
signed=0
while IFS= read -r -d '' file; do
  if file -b "$file" | grep -q '^Mach-O'; then
    codesign --force --sign - --timestamp=none "$file" 2>/dev/null
    signed=$((signed + 1))
  fi
done < <(find "$PY" "$APP/Contents/Helpers" -type f -print0)
codesign --force --sign - --timestamp=none --identifier "$BUNDLE_ID" "$APP" 2>/dev/null
codesign --verify --deep --strict "$APP" || die "signature check failed"
echo "    $signed nested Mach-O files signed"

# --- self-check on a read-only copy ------------------------------------------------------

step "Self-check (read-only copy)"
CHECK="$WORK/check"
[ -d "$CHECK" ] && chmod -R u+w "$CHECK"  # a failed run leaves the read-only copy behind
rm -rf "$CHECK"
mkdir -p "$CHECK"
ditto "$APP" "$CHECK/$APP_NAME.app"
CAPP="$CHECK/$APP_NAME.app"
chmod -R a-w "$CAPP"
marker="$CHECK/marker"
touch "$marker"
sleep 1
CPY="$CAPP/Contents/Resources/python/bin/python3.12"
"$CPY" -I -B -c "import ssl, sqlite3, fastapi, uvicorn, jinja2, transcribator" \
  || die "embedded Python can't import the service"
"$CAPP/Contents/Helpers/fluidaudiocli" --help >/dev/null 2>&1 || die "fluidaudiocli does not run"

wav="$CHECK/tone"
"$CPY" -I -B -c "import sys; from pathlib import Path; from transcribator.cli import write_tone; write_tone(Path(sys.argv[1]))" "$wav"
probe="$("$CAPP/Contents/Helpers/transcribator-audio" probe "$wav")"
case "$probe" in '{"duration":'*) ;; *) die "transcribator-audio probe failed" ;; esac

data="$CHECK/data"
mkdir -m 700 "$data"
key="$(openssl rand -hex 32)"
out="$CHECK/ready"
printf '%s\n' "$key" | env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
  LANG=en_US.UTF-8 TRANSCRIBATOR_DATA_DIR="$data" \
  TRANSCRIBATOR_FLUIDAUDIO_CLI="$CAPP/Contents/Helpers/fluidaudiocli" \
  TRANSCRIBATOR_AUDIO_TOOL="$CAPP/Contents/Helpers/transcribator-audio" \
  "$CPY" -I -B -m transcribator serve --app --port 0 > "$out" 2> "$CHECK/serve.log" &
CHECK_PID=$!
ready=""
for _ in $(seq 1 60); do
  ready="$(head -n 1 "$out" 2>/dev/null || true)"
  [ -n "$ready" ] && break
  sleep 0.5
done
port="$(printf '%s' "$ready" | sed -n 's/^{"ready": true, "port": \([0-9]*\)}$/\1/p')"
[ -n "$port" ] || die "the service printed no ready line (see $CHECK/serve.log)"
code_key="$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' -b "tb_key=$key" "http://127.0.0.1:$port/api/status")"
code_nokey="$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' "http://127.0.0.1:$port/api/status")"
setup="$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' --noproxy '*' -b "tb_key=$key" "http://127.0.0.1:$port/")"
kill -TERM "$CHECK_PID"
wait "$CHECK_PID" || true
CHECK_PID=""
[ "$code_key" = 200 ] || die "/api/status with the key answered $code_key"
[ "$code_nokey" = 403 ] || die "/api/status without the key answered $code_nokey"
case "$setup" in "303 "*"/setup") ;; *) die "first start does not lead to /setup ($setup)" ;; esac
written="$(find "$CAPP" -newer "$marker" | head -n 5)"
[ -z "$written" ] || die "the app wrote into its own bundle: $written"
lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && die "the service is still listening"
chmod -R u+w "$CHECK"
rm -rf "$CHECK"
echo "    service, key check, helpers: ok; bundle unchanged"

# --- disk image --------------------------------------------------------------------------

step "Disk image"
DMG="$DIST/$APP_NAME.dmg"
img="$WORK/dmg"
rm -rf "$img" "$DMG" "$DMG.sha256"
mkdir -p "$img"
ditto "$APP" "$img/$APP_NAME.app"
ln -s /Applications "$img/Applications"
hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$img" -fs APFS -format ULFO -ov "$DMG"
MOUNT="$(mktemp -d "$WORK/mnt.XXXXXX")"
hdiutil attach -quiet -nobrowse -readonly -mountpoint "$MOUNT" "$DMG"
codesign --verify --deep --strict "$MOUNT/$APP_NAME.app" || die "signature broken inside the disk image"
hdiutil detach -quiet "$MOUNT"
rmdir "$MOUNT"
MOUNT=""
(cd "$DIST" && shasum -a 256 "$APP_NAME.dmg" > "$APP_NAME.dmg.sha256")

size_app="$(du -sh "$APP" | cut -f1)"
size_dmg="$(du -sh "$DMG" | cut -f1)"
echo
echo "App:   $APP ($size_app)"
echo "Image: $DMG ($size_dmg)"
echo "SHA256 $(cut -d' ' -f1 "$DMG.sha256")"
echo "Source $COMMIT"
[ "$DIRTY" = 0 ] || echo "WARNING: built from uncommitted changes: not for publishing" >&2
