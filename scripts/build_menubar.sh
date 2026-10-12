#!/bin/bash
# Builds the Transcribator menu bar icon (macos/MenuBar) into an .app bundle.
# Needs the Command Line Tools (swiftc, codesign). No third-party dependencies.
#
#   scripts/build_menubar.sh     installs to ~/Applications/Transcribator.app
#
# The path is fixed: `transcribator autostart install` expects the app exactly there.
set -euo pipefail

if [ "$#" -ne 0 ]; then
    echo "usage: scripts/build_menubar.sh (no arguments)" >&2
    exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app="$HOME/Applications/Transcribator.app"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/transcribator-menubar.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

# Module cache in the temp dir: the build does not write to the shared cache and works in a sandbox.
swiftc -O -swift-version 6 -target arm64-apple-macos14.0 -framework AppKit \
    -module-cache-path "$tmp/module-cache" \
    -o "$tmp/Transcribator" "$repo_root/macos/MenuBar/main.swift"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$tmp/Transcribator" "$app/Contents/MacOS/Transcribator"
cp "$repo_root/macos/MenuBar/Info.plist" "$app/Contents/Info.plist"

# Ad-hoc signature: no developer certificate, valid on this Mac only.
codesign --force --sign - "$app"

echo "$app"
