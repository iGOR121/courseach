#!/bin/bash
#
# Build LanChat.app - a double-clickable macOS wrapper around the lanchat
# terminal app.
#
# Usage:
#   ./macos/build_app.sh
#
# The result is written to dist/LanChat.app and can be moved anywhere (for
# example into /Applications). The app only needs Python 3, which it finds via
# the Xcode Command Line Tools.
#
# The bundle is assembled by hand (not with osacompile) so that launching it
# uses LaunchServices instead of Apple Events. That avoids the macOS
# "no permissions for sending Apple Events (receiver: Terminal)" error.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root_dir="$(cd "$script_dir/.." && pwd)"
dist_dir="$root_dir/dist"
app_bundle="$dist_dir/LanChat.app"
resources="$app_bundle/Contents/Resources/app"

rm -rf "$app_bundle"
mkdir -p "$app_bundle/Contents/MacOS" "$resources"

# 1. Bundle metadata and the executable entry point.
cp "$script_dir/Info.plist" "$app_bundle/Contents/Info.plist"

cp "$script_dir/LanChat" "$app_bundle/Contents/MacOS/LanChat"
chmod +x "$app_bundle/Contents/MacOS/LanChat"

# 2. The Python package and the script Terminal will run.
cp -R "$root_dir/lanchat" "$resources/"
cp "$script_dir/launch.command" "$resources/launch.command"
chmod +x "$resources/launch.command"
find "$resources" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# 3. Ad-hoc signature (best effort). This keeps the bundle stable when moved
#    around and lets Finder treat it as a normal app.
if command -v codesign >/dev/null 2>&1; then
	codesign --force --deep --sign - "$app_bundle" >/dev/null 2>&1 || true
fi

# 4. Sanity-check the plist if a checker is available.
if command -v plutil >/dev/null 2>&1; then
	plutil -lint "$app_bundle/Contents/Info.plist" >/dev/null
fi

echo "Built $app_bundle"
echo "Double-click it in Finder, or run: open \"$app_bundle\""
