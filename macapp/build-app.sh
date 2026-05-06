#!/usr/bin/env bash
# Build a clickable DedupeImages.app bundle.
#
# The app's launcher pops a folder picker (osascript), then runs the Python
# tool's --review web UI against the chosen folder with a default quarantine
# at ~/dedupe-quarantine. The native Swift CLI is bundled too, for users who
# prefer the pure-native path; see Resources/dedupe-images-swift.
#
# Output: macapp/dist/DedupeImages.app
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DIST="$ROOT/macapp/dist"
APP="$DIST/DedupeImages.app"

# Build the Swift binary (release) so it ends up tight in the bundle.
echo "==> Building Swift CLI (release)..."
( cd "$ROOT/swift" && swift build -c release ) > /dev/null

SWIFT_BIN="$ROOT/swift/.build/release/DedupeImagesCLI"
if [[ ! -x "$SWIFT_BIN" ]]; then
    echo "Swift binary not found at $SWIFT_BIN" >&2
    exit 1
fi

echo "==> Assembling .app bundle..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# Info.plist — minimum needed for a clickable Cocoa app.
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIdentifier</key><string>com.nautis.dedupeimages</string>
    <key>CFBundleName</key><string>DedupeImages</string>
    <key>CFBundleDisplayName</key><string>Dedupe Images</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Bundle the Python script and the Swift CLI as resources.
cp "$ROOT/dedupe_images.py" "$APP/Contents/Resources/dedupe_images.py"
cp "$SWIFT_BIN" "$APP/Contents/Resources/dedupe-images-swift"

# Launcher: prompts for a folder, runs --review against it.
cat > "$APP/Contents/MacOS/launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../Resources"

# Folder picker via osascript.
FOLDER=$(osascript -e 'POSIX path of (choose folder with prompt "Pick a folder to dedupe:")' 2>/dev/null) || exit 0
if [[ -z "$FOLDER" ]]; then exit 0; fi

# Strip trailing slash.
FOLDER="${FOLDER%/}"

QUAR="$HOME/dedupe-quarantine"

# uv is the cleanest path; if not installed, fall back to a clear error.
UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
    osascript -e 'display alert "uv is required" message "Install uv (brew install uv) and try again. The DedupeImages app uses uv to auto-install Python deps."' >/dev/null
    exit 1
fi

# Open Terminal so the user can see progress + interact, since --review
# blocks until commit. Using AppleScript to launch in a new Terminal window.
SCRIPT="exec '$UV' run '$RES/dedupe_images.py' --review --quarantine '$QUAR' --flat '$FOLDER'"
osascript <<APPLESCRIPT
tell application "Terminal"
    activate
    do script "$SCRIPT"
end tell
APPLESCRIPT
LAUNCHER

chmod +x "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/Resources/dedupe-images-swift"

echo ""
echo "Built: $APP"
echo "Double-click it from Finder, or: open '$APP'"
echo ""
echo "Note: unsigned. macOS may warn 'cannot verify developer'."
echo "      Right-click -> Open the first time to bypass."
