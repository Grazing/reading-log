#!/bin/bash
# Build a Windows setup EXE on macOS.
#   ./win/build-win.sh            # installer includes ~/.reading-log test data
#   WITH_DATA=0 ./win/build-win.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WITH_DATA="${WITH_DATA:-1}"
PY_VER="3.12.10"
STAGE="$ROOT/dist/win-payload"
CACHE="$ROOT/dist/win-cache"
WHEELS="$CACHE/wheels"

if [[ "$WITH_DATA" == "1" ]]; then
  OUT="$ROOT/dist/书历-Setup-带数据.exe"
else
  OUT="$ROOT/dist/书历-Setup.exe"
fi

if ! command -v go >/dev/null 2>&1; then
  echo "需要 Go 才能编译 Windows 启动器" >&2
  exit 1
fi

mkdir -p "$CACHE" "$WHEELS"
EMBED_ZIP="$CACHE/python-${PY_VER}-embed-amd64.zip"
if [[ ! -f "$EMBED_ZIP" ]]; then
  echo "下载 Windows Python ${PY_VER}…"
  curl -fL --retry 3 -o "$EMBED_ZIP" \
    "https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
fi

echo "下载 Windows 依赖…"
python3 -m pip download "pypinyin>=0.49" -d "$WHEELS" --quiet
python3 -m pip download "pillow" -d "$WHEELS" \
  --only-binary=:all: \
  --platform win_amd64 \
  --python-version 312 \
  --implementation cp \
  --quiet

echo "组装安装内容…"
rm -rf "$STAGE"
mkdir -p "$STAGE/python/Lib/site-packages" "$STAGE/app/static" "$STAGE/seed-data"

unzip -q -o "$EMBED_ZIP" -d "$STAGE/python"
# Isolated ._pth: list site-packages explicitly (script dir is added by app.py).
PY_TAG="python312"
if [[ -f "$STAGE/python/python313._pth" ]]; then
  PY_TAG="python313"
fi
printf '%s\n.\nLib\\site-packages\n' "${PY_TAG}.zip" > "$STAGE/python/${PY_TAG}._pth"

find "$WHEELS" -name '*.whl' -print0 | while IFS= read -r -d '' whl; do
  unzip -q -o "$whl" -d "$STAGE/python/Lib/site-packages"
done

cp "$ROOT/app.py" "$ROOT/store.py" "$ROOT/weread.py" "$ROOT/ireader.py" "$ROOT/sync.py" "$STAGE/app/"
cp "$ROOT/static/"* "$STAGE/app/static/"

python3 - <<PY
from PIL import Image
from pathlib import Path
root = Path("$ROOT")
src = root / "macos" / "AppIcon-1024.png"
img = Image.open(src).convert("RGBA")
img.save(root / "win" / "shuli.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
PY
cp "$ROOT/win/shuli.ico" "$STAGE/shuli.ico"

if [[ "$WITH_DATA" == "1" ]]; then
  DATA="$HOME/.reading-log"
  python3 - <<'PY'
import sqlite3
from pathlib import Path
db = Path.home() / ".reading-log" / "reading.db"
con = sqlite3.connect(db)
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
con.close()
PY
  cp "$DATA/reading.db" "$STAGE/seed-data/reading.db"
  for name in session.json ireader-session.json wallpaper-state.json; do
    if [[ -f "$DATA/$name" ]]; then
      cp "$DATA/$name" "$STAGE/seed-data/$name"
    fi
  done
  if [[ -d "$DATA/covers" ]] && find "$DATA/covers" -type f | grep -q .; then
    mkdir -p "$STAGE/seed-data/covers"
    cp -R "$DATA/covers/." "$STAGE/seed-data/covers/"
  fi
  printf 'with-data\n' > "$STAGE/seed-data/SEED.txt"
else
  rm -rf "$STAGE/seed-data"
fi

echo "编译启动器和安装包…"
(
  cd "$ROOT/win/launcher"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 \
    go build -ldflags="-H=windowsgui -s -w" -o "$STAGE/书历.exe" .
)
(
  cd "$ROOT/win/uninst"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 \
    go build -ldflags="-H=windowsgui -s -w" -o "$STAGE/卸载书历.exe" .
)

python3 - <<PY
import zipfile
from pathlib import Path
stage = Path("$STAGE")
out = Path("$ROOT/win/setup/payload.zip")
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
    for path in stage.rglob("*"):
        if path.is_file():
            z.write(path, path.relative_to(stage).as_posix())
print("payload", out, out.stat().st_size)
PY

(
  cd "$ROOT/win/setup"
  GOOS=windows GOARCH=amd64 CGO_ENABLED=0 \
    go build -ldflags="-H=windowsgui -s -w" -o "$OUT" .
)
rm -f "$ROOT/win/setup/payload.zip"

ls -lh "$OUT"
file "$OUT"
echo "完成: $OUT"
