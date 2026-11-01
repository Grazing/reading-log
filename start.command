#!/bin/bash
# 正式启动方式：双击本文件，在「终端」里跑。已有健康的 :18766 时不要 kill 再起。
# Cursor / Agent 里执行本脚本会转交给「终端」，这样关掉 Cursor 网页还在。
cd "$(dirname "$0")"
DIR="$(pwd)"
URL="http://127.0.0.1:18766/"

already_up() {
  curl -sf -o /dev/null --max-time 1 "$URL"
}

under_cursor() {
  local p comm
  p=${PPID:-0}
  local i
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

if already_up; then
  open "$URL"
  echo "书历已在运行：$URL"
  echo "关掉原来的那个终端窗口才会真正退出。"
  if [ -t 0 ] && [ "$TERM_PROGRAM" = "Apple_Terminal" ]; then
    sleep 2
  fi
  exit 0
fi

# Agent / Cursor 集成终端里启动时，改由「终端.app」拉起，避免随 Cursor 一起被 SIGTERM。
if under_cursor; then
  /usr/bin/open -a Terminal "$DIR/start.command"
  echo "已交给「终端」启动，关掉 Cursor 也不会退出。"
  exit 0
fi

exec python3 app.py
