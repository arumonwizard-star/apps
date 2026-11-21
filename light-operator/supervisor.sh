#!/bin/bash
# Light Operator 見張り役：サーバーとネット公開を常に動かし続ける
#  - 止まっていたら自動で起動し直す（スリープ復帰・クラッシュ対策）
#  - 公開アドレスが変わったら固定の入口（ntfy）に掲示し直す
#  - 「サーバーを止める」（stopped.flag）が押されたら全部止めて終了
RES="$(cd "$(dirname "$0")" && pwd)"            # 部品（アプリの中）
DATA="${1:-$HOME/Library/Application Support/LightOperator}"  # 記録・状態
mkdir -p "$DATA"; cd "$DATA" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
PORT=8787
KEY=$(cat opkey.txt 2>/dev/null)
TOPIC=$(cat "$RES/topic.txt" 2>/dev/null)
log() { echo "[$(date '+%m/%d %H:%M:%S')] $*" >> supervisor.log; }

# 二重起動しない
if [ -f supervisor.pid ] && kill -0 "$(cat supervisor.pid)" 2>/dev/null; then exit 0; fi
echo $$ > supervisor.pid
log "見張り役 開始"

# サーバー本体（Node.js → Python の順に探す）
RUN=()
if command -v node >/dev/null 2>&1; then RUN=(node "$RES/server.js"); else
  for py in python3 "$HOME/chcut-env/bin/python3" "$HOME/miniconda3/bin/python3" "$HOME/anaconda3/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ "$py" = "python3" ]; then
      p=$(command -v python3) || continue
      if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
      py="$p"
    fi
    if [ -x "$py" ] && "$py" -c 'import http.server' >/dev/null 2>&1; then RUN=("$py" "$RES/server.py"); break; fi
  done
fi
[ ${#RUN[@]} -eq 0 ] && { log "Node.js も Python も見つかりません"; exit 1; }

SERVER_PID=""; CF_PID=""; URL=""; LAST_POST=0; LAST_UPD=0
UPD="$HOME/Applications/.apps-updater/update.sh"
# サーバーの部品が更新されたら、自動で新しい版に入れ替える（公開アドレスはそのまま）
code_stamp() { stat -f %m "$RES/server.py" "$RES/server.js" 2>/dev/null | tr '\n' ' '; }
STAMP=$(code_stamp)

post() {
  [ -n "$TOPIC" ] && [ -n "$URL" ] || return
  curl -s -m 10 -H "Title: Light Operator 参加リンク（$(date '+%m/%d %H:%M')）" -H "Click: $URL" -H "Tags: bulb" \
    -d "↓ここをタップして参加
$URL" "https://ntfy.sh/$TOPIC" >/dev/null && LAST_POST=$(date +%s) && log "入口に掲示: $URL"
}
server_ok() { curl -s -m 3 "http://localhost:$PORT/api/info?op=1" | grep -q phone-light; }

start_server() {
  for p in $(lsof -ti tcp:$PORT 2>/dev/null); do kill $p 2>/dev/null; done
  sleep 0.5
  HTTP=1 PORT=$PORT OPERATOR_KEY=$KEY LO_DATA="$DATA" nohup "${RUN[@]}" >> server.log 2>&1 < /dev/null &
  SERVER_PID=$!
  log "サーバー起動 (pid $SERVER_PID)"
  sleep 2
}
start_tunnel() {
  [ -n "$CF_PID" ] && kill $CF_PID 2>/dev/null
  pkill -f 'cloudflared tunnel' 2>/dev/null
  sleep 0.5
  : > tunnel.log
  nohup "$RES/cloudflared" tunnel --no-autoupdate --protocol http2 --url http://localhost:$PORT > tunnel.log 2>&1 < /dev/null &
  CF_PID=$!
  URL=""
  for i in $(seq 1 60); do
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' tunnel.log | grep -v '//api\.' | head -1)
    [ -n "$URL" ] && break
    sleep 1
  done
  if [ -n "$URL" ]; then
    printf '%s' "$URL" > current_url.txt
    log "公開: $URL"
    post
  else
    log "公開に失敗（ネット接続を確認）"
  fi
}
stop_all() {
  log "停止"
  [ -n "$CF_PID" ] && kill $CF_PID 2>/dev/null
  pkill -f 'cloudflared tunnel' 2>/dev/null
  rm -f current_url.txt supervisor.pid
  exit 0
}
trap stop_all TERM INT

while true; do
  # 「サーバーを止める」が押された
  if [ -f stopped.flag ]; then stop_all; fi

  # 部品が更新されていたらサーバーだけ入れ替え
  NOW_STAMP=$(code_stamp)
  if [ "$NOW_STAMP" != "$STAMP" ]; then
    STAMP=$NOW_STAMP; log "部品が更新されたのでサーバーを入れ替え"; start_server
  fi

  # サーバー
  if ! server_ok; then
    sleep 2
    [ -f stopped.flag ] && stop_all
    server_ok || start_server
  fi

  # ネット公開（プロセスが死んでいる・アドレスがない → 張り直し）
  if [ -z "$CF_PID" ] || ! kill -0 "$CF_PID" 2>/dev/null || [ -z "$URL" ]; then
    start_tunnel
  fi

  # 入口の掲示は3時間ごとに更新（12時間で消えるため）
  if [ -n "$URL" ] && [ $(( $(date +%s) - LAST_POST )) -gt 10800 ]; then post; fi

  # アプリ置き場（GitHub apps）の新しい版を5分ごとに確認して、Mac のアプリに反映
  if [ $(( $(date +%s) - LAST_UPD )) -ge 300 ]; then
    LAST_UPD=$(date +%s)
    [ -f "$UPD" ] && nohup /bin/bash "$UPD" >/dev/null 2>&1 < /dev/null &
  fi

  sleep 5
done
