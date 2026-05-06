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

echo "==> Generating app icon..."
ICON_PNG="$ROOT/macapp/icon.png"
ICONSET="$ROOT/macapp/AppIcon.iconset"
ICNS="$ROOT/macapp/icon.icns"

if [[ ! -f "$ICON_PNG" ]]; then
    UV_BIN="$(command -v uv || true)"
    if [[ -n "$UV_BIN" ]]; then
        "$UV_BIN" run "$ROOT/macapp/make-icon.py" > /dev/null
    fi
fi

if [[ -f "$ICON_PNG" ]]; then
    rm -rf "$ICONSET"
    mkdir -p "$ICONSET"
    # Standard macOS .iconset sizes (Apple HIG):
    sips -z 16 16     "$ICON_PNG" --out "$ICONSET/icon_16x16.png" > /dev/null
    sips -z 32 32     "$ICON_PNG" --out "$ICONSET/icon_16x16@2x.png" > /dev/null
    sips -z 32 32     "$ICON_PNG" --out "$ICONSET/icon_32x32.png" > /dev/null
    sips -z 64 64     "$ICON_PNG" --out "$ICONSET/icon_32x32@2x.png" > /dev/null
    sips -z 128 128   "$ICON_PNG" --out "$ICONSET/icon_128x128.png" > /dev/null
    sips -z 256 256   "$ICON_PNG" --out "$ICONSET/icon_128x128@2x.png" > /dev/null
    sips -z 256 256   "$ICON_PNG" --out "$ICONSET/icon_256x256.png" > /dev/null
    sips -z 512 512   "$ICON_PNG" --out "$ICONSET/icon_256x256@2x.png" > /dev/null
    sips -z 512 512   "$ICON_PNG" --out "$ICONSET/icon_512x512.png" > /dev/null
    cp "$ICON_PNG" "$ICONSET/icon_512x512@2x.png"
    iconutil -c icns "$ICONSET" -o "$ICNS"
    rm -rf "$ICONSET"
fi

echo "==> Assembling .app bundle..."
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"
[[ -f "$ICNS" ]] && cp "$ICNS" "$APP/Contents/Resources/AppIcon.icns"

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
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# Bundle the Python script and the Swift CLI as resources.
cp "$ROOT/dedupe_images.py" "$APP/Contents/Resources/dedupe_images.py"
cp "$SWIFT_BIN" "$APP/Contents/Resources/dedupe-images-swift"

# Launcher: prompts for a folder, runs --review against it.
# The server runs detached - no Terminal window. It opens the browser itself
# via webbrowser.open(). Stdout/stderr go to ~/Library/Logs/DedupeImages/.
cat > "$APP/Contents/MacOS/launcher" <<'LAUNCHER'
#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/../Resources"

# uv is required; if missing, alert and exit.
UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
    # Common Homebrew install paths in case PATH isn't propagated to .app launchers.
    for p in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
        if [[ -x "$p" ]]; then UV="$p"; break; fi
    done
fi
if [[ -z "$UV" ]]; then
    osascript -e 'display alert "uv is required" message "Install uv (brew install uv) and try again."' >/dev/null
    exit 1
fi

# Folder picker.
FOLDER=$(osascript -e 'POSIX path of (choose folder with prompt "Pick a folder to dedupe:")' 2>/dev/null) || exit 0
if [[ -z "$FOLDER" ]]; then exit 0; fi
FOLDER="${FOLDER%/}"

QUAR="$HOME/dedupe-quarantine"
LOG_DIR="$HOME/Library/Logs/DedupeImages"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-$(date +%Y%m%d-%H%M%S).log"

# Run the server fully detached. nohup + & + disown means the server stays
# alive after this launcher exits. Stdout/stderr go to the log file.
# The server itself calls webbrowser.open() once ready, so no Terminal needed.
nohup "$UV" run "$RES/dedupe_images.py" \
    --review --quarantine "$QUAR" --flat "$FOLDER" \
    > "$LOG" 2>&1 < /dev/null &
disown

# Friendly hint via system notification (non-blocking).
osascript -e "display notification \"Scanning $FOLDER. Browser will open when ready.\" with title \"DedupeImages\" subtitle \"Logs: $LOG\"" 2>/dev/null || true

exit 0
LAUNCHER

chmod +x "$APP/Contents/MacOS/launcher"
chmod +x "$APP/Contents/Resources/dedupe-images-swift"

INSTALL=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
    esac
done

if [[ "$INSTALL" -eq 1 ]]; then
    DEST="/Applications/DedupeImages.app"
    echo "==> Installing to $DEST..."
    rm -rf "$DEST"
    cp -R "$APP" "$DEST"
    # Bump mtime so Finder/LaunchServices notice changes.
    touch "$DEST"
    # Force LaunchServices to re-register so the new icon shows up.
    /System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister \
        -f "$DEST" 2>/dev/null || true
    echo "Installed: $DEST"
fi

echo ""
echo "Built: $APP"
echo "Double-click it from Finder, or: open '$APP'"
echo ""
echo "Note: unsigned. macOS may warn 'cannot verify developer'."
echo "      Right-click -> Open the first time to bypass."
