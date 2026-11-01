#!/bin/bash
# Build a Mac disk image that does not include ~/.reading-log.
#   ./macos/package.sh
# Output: dist/书历.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="书历"
DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"
MACOS_DIR="$APP/Contents/MacOS"
RES="$APP/Contents/Resources/reading-log"
CACHE="$DIST/mac-cache"
PY_VER_TAG="3.12.15+20261020"
PY_ARCHIVE="cpython-${PY_VER_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261020/${PY_ARCHIVE}"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
STAGE="$DIST/dmg-root"
OUT="$DIST/${APP_NAME}.dmg"

mkdir -p "$CACHE"
if [[ ! -f "$CACHE/$PY_ARCHIVE" ]]; then
  echo "下载 macOS Python…"
  curl -fL --retry 3 -A "Mozilla/5.0" -o "$CACHE/$PY_ARCHIVE" "$PY_URL"
fi

PY_SRC="$CACHE/python"
if [[ ! -x "$PY_SRC/bin/python3" ]]; then
  rm -rf "$PY_SRC"
  tar -xzf "$CACHE/$PY_ARCHIVE" -C "$CACHE"
fi

echo "安装 Python 依赖…"
"$PY_SRC/bin/python3" -m pip install --disable-pip-version-check "pypinyin>=0.49" pillow

echo "编译书历…"
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
mkdir -p "$APP/Contents/Resources/python"
cp -R "$PY_SRC/." "$APP/Contents/Resources/python/"
chmod +x "$MACOS_DIR/$APP_NAME" "$APP/Contents/Resources/python/bin/python3"

if /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(date +%Y%m%d%H%M)" "$APP/Contents/Info.plist" >/dev/null 2>&1; then
  :
fi

if find "$APP" \( -name 'reading.db' -o -name 'session.json' -o -name 'ireader-session.json' -o -name 'wallpaper-state.json' \) | grep -q .; then
  echo "安装包里混进了阅读数据" >&2
  exit 1
fi

# 先验证依赖，再清掉签名后会失效的 __pycache__，最后 ad-hoc 签名。
# 注意：-s - 仅本机自用；传给另一台 Mac 时 Gatekeeper 常误报「已损坏」。
"$APP/Contents/Resources/python/bin/python3" -c "import PIL, pypinyin"
find "$APP" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
xattr -cr "$APP" 2>/dev/null || true
codesign --force --deep -s - "$APP"

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
cp "$ROOT/macos/安装说明.txt" "$STAGE/安装说明.txt"
ln -s /Applications "$STAGE/Applications"

echo "制作磁盘映像…"
rm -f "$OUT"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$OUT" >/dev/null
rm -rf "$STAGE"

ls -lh "$OUT"
echo "完成: $OUT"
echo
echo "说明：当前为 ad-hoc 签名、未公证。另一台 Mac 若提示「已损坏」，一般不是真损坏，而是 Gatekeeper + 隔离属性。"
echo "对方可执行：xattr -cr /Applications/书历.app   或对 .app 右键 → 打开。"
echo "若要双击即开、无提示，需 Apple Developer ID 签名并 notarize。"
