#!/bin/bash
# 把本机书历暴露成固定公网网址（ngrok），手机可随时打开同一份数据。
# 前提：Mac 开着，且本机 :18766 在跑（书历.app 或 start.command）。
# 首次使用会引导登录 ngrok（免费账号自带固定域名，重启不变）。
cd "$(dirname "$0")"
DIR="$(pwd)"
LOCAL="http://127.0.0.1:18766/"
NGROK_API="http://127.0.0.1:4040/api/tunnels"
URL_FILE="${HOME}/.reading-log/remote-url.txt"

under_cursor() {
  local p comm i
  p=${PPID:-0}
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    [ -z "$p" ] || [ "$p" = "0" ] || [ "$p" = "1" ] && return 1
    comm=$(ps -o command= -p "$p" 2>/dev/null) || return 1
    case "$comm" in
      *"/Cursor.app/"*|*Cursor\ Helper*) return 0 ;;
    esac
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  return 1
}

if under_cursor; then
  /usr/bin/open -a Terminal "$DIR/remote.command"
  echo "已交给「终端」启动隧道。"
  exit 0
fi

if ! command -v ngrok >/dev/null 2>&1; then
  echo "未找到 ngrok。请先执行：brew install ngrok"
  read -r -p "按回车关闭…" _
  exit 1
fi

if ! ngrok config check >/dev/null 2>&1; then
  echo "首次使用：需要 ngrok 账号（免费，自带固定域名）。"
  echo "浏览器将打开 authtoken 页面；复制 token 后粘贴到下面。"
  /usr/bin/open "https://dashboard.ngrok.com/get-started/your-authtoken"
  echo
  read -r -p "粘贴 authtoken：" TOKEN
  TOKEN=$(printf '%s' "$TOKEN" | tr -d '[:space:]')
  if [ -z "$TOKEN" ]; then
    echo "未输入 token，已取消。"
    read -r -p "按回车关闭…" _
    exit 1
  fi
  if ! ngrok config add-authtoken "$TOKEN"; then
    echo "写入 authtoken 失败。"
    read -r -p "按回车关闭…" _
    exit 1
  fi
  echo "已保存。以后双击本脚本即可。"
  echo
fi

if ! curl -sf -o /dev/null --max-time 1 "$LOCAL"; then
  echo "本机服务未启动，正在打开 start.command…"
  /usr/bin/open -a Terminal "$DIR/start.command"
  i=0
  while [ "$i" -lt 40 ]; do
    if curl -sf -o /dev/null --max-time 1 "$LOCAL"; then
      break
    fi
    i=$((i + 1))
    sleep 0.25
  done
  if ! curl -sf -o /dev/null --max-time 1 "$LOCAL"; then
    echo "等不到 http://127.0.0.1:18766/ ，请先打开「书历.app」。"
    read -r -p "按回车关闭…" _
    exit 1
  fi
fi

# 若已有指向本机 18766 的隧道，直接打印网址，避免重复开。
EXISTING=$(curl -sf --max-time 1 "$NGROK_API" 2>/dev/null | python3 -c '
import json,sys
try:
  data=json.load(sys.stdin)
except Exception:
  raise SystemExit(0)
for t in data.get("tunnels") or []:
  conf=(t.get("config") or {})
  addr=str(conf.get("addr") or "")
  pub=str(t.get("public_url") or "")
  if pub.startswith("https://") and ("18766" in addr or addr.rstrip("/").endswith(":18766")):
    print(pub)
    break
' 2>/dev/null || true)

if [ -n "$EXISTING" ]; then
  mkdir -p "$(dirname "$URL_FILE")"
  printf '%s\n' "$EXISTING" > "$URL_FILE"
  echo "隧道已在运行（固定网址）："
  echo "  $EXISTING"
  echo
  echo "已写入 $URL_FILE"
  echo "手机浏览器打开上面的 https 地址即可（Mac 需保持开机且本窗口/ngrok 勿关）。"
  echo "免费域名首次打开可能有 ngrok 提示页，点 Visit Site 即可。"
  read -r -p "按回车关闭本说明（不会关掉已有隧道）…" _
  exit 0
fi

# 清掉旧的同端口 agent，再起一条。
pkill -f "ngrok http 18766" 2>/dev/null || true
sleep 0.4

echo "正在建立公网隧道…"
ngrok http 18766 --log=stdout --log-format=logfmt >/tmp/reading-log-ngrok.log 2>&1 &
NGROK_PID=$!

PUBLIC=""
i=0
while [ "$i" -lt 40 ]; do
  if ! kill -0 "$NGROK_PID" 2>/dev/null; then
    echo "ngrok 启动失败。日志："
    tail -n 40 /tmp/reading-log-ngrok.log 2>/dev/null || true
    read -r -p "按回车关闭…" _
    exit 1
  fi
  PUBLIC=$(curl -sf --max-time 1 "$NGROK_API" 2>/dev/null | python3 -c '
import json,sys
try:
  data=json.load(sys.stdin)
except Exception:
  raise SystemExit(0)
for t in data.get("tunnels") or []:
  pub=str(t.get("public_url") or "")
  if pub.startswith("https://"):
    print(pub)
    break
' 2>/dev/null || true)
  if [ -n "$PUBLIC" ]; then
    break
  fi
  i=$((i + 1))
  sleep 0.25
done

if [ -z "$PUBLIC" ]; then
  echo "拿不到公网地址。日志："
  tail -n 40 /tmp/reading-log-ngrok.log 2>/dev/null || true
  kill "$NGROK_PID" 2>/dev/null || true
  read -r -p "按回车关闭…" _
  exit 1
fi

mkdir -p "$(dirname "$URL_FILE")"
printf '%s\n' "$PUBLIC" > "$URL_FILE"

echo
echo "========================================"
echo " 手机打开（固定网址，账号内不变）："
echo " $PUBLIC"
echo "========================================"
echo
echo "已写入 $URL_FILE"
echo "Mac 保持开机；关掉本窗口会结束隧道。"
echo "免费域名首次打开可能有 ngrok 提示页，点 Visit Site 即可。"
echo

trap 'kill "$NGROK_PID" 2>/dev/null || true; exit 0' INT TERM
wait "$NGROK_PID"
