#!/bin/bash
# 编译普通窗口应用并安装到 /Applications/书历.app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="书历"
DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"
MACOS_DIR="$APP/Contents/MacOS"
RES="$APP/Contents/Resources/reading-log"
SDK="$(xcrun --sdk macosx --show-sdk-path)"

# 重建时退出旧应用，并清掉 :18766，让新安装的应用自己拉起 python（不要靠终端）。
pkill -f '/Applications/书历.app/Contents/MacOS/' 2>/dev/null || true
pkill -f '/reading-log/dist/书历.app/Contents/MacOS/' 2>/dev/null || true
pkill -f '/Applications/阅读记录.app/Contents/MacOS/' 2>/dev/null || true
pkill -f '/reading-log/dist/阅读记录.app/Contents/MacOS/' 2>/dev/null || true
killall "$APP_NAME" 2>/dev/null || true
killall "阅读记录" 2>/dev/null || true
if command -v lsof >/dev/null 2>&1; then
  lsof -nP -iTCP:18766 -sTCP:LISTEN -t 2>/dev/null | xargs -n1 kill 2>/dev/null || true
fi
sleep 0.4

rm -rf "$APP"
mkdir -p "$MACOS_DIR" "$RES/static"

swiftc -O -parse-as-library \
  -target arm64-apple-macos14.0 \
  -sdk "$SDK" \
  -framework AppKit -framework WebKit -framework ServiceManagement -framework CryptoKit \
  -o "$MACOS_DIR/$APP_NAME" \
  "$ROOT"/macos/Sources/*.swift

cp "$ROOT/macos/Info.plist" "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"
cp "$ROOT/macos/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$ROOT/app.py" "$ROOT/store.py" "$ROOT/weread.py" "$ROOT/ireader.py" "$ROOT/sync.py" "$RES/"
cp "$ROOT/static/"* "$RES/static/"
chmod +x "$MACOS_DIR/$APP_NAME"

if /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(date +%Y%m%d%H%M)" "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  :
fi

codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true

DEST="/Applications/$APP_NAME.app"
rm -rf "$DEST"
rm -rf "/Applications/阅读记录.app"
cp -R "$APP" "$DEST"
xattr -cr "$APP" "$DEST" 2>/dev/null || true
codesign --force --deep -s - "$DEST" >/dev/null 2>&1 || true
echo "Built $APP"
echo "Installed $DEST"
