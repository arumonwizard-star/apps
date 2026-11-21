#!/bin/bash
# ARA安全装置 — Logic の Melodyne(ARA) 補正データを世代バックアップする見張り役
VERSION="1.0"
export LANG=en_US.UTF-8
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

HOME_DIR="${ARA_HOME:-$HOME}"
APPDIR="$HOME_DIR/Music/ARA安全装置"
STATE="$APPDIR/.state"
LOG="$APPDIR/記録.log"
EXT="${ARA_EXT:-/Volumes/Extreme SSD}"
KEEP=30
STABLE_SEC=40          # ARAデータの書き込みが終わったとみなすまでの秒数
AFTER_QUIT_SEC=300     # Logic終了後も見張り続ける秒数
LIST_REFRESH_SEC=300   # プロジェクト一覧を探し直す間隔
TAB="$(printf '\t')"

mkdir -p "$STATE"
if [ "$(uname)" = "Darwin" ]; then MAC=1; else MAC=0; fi

now() { date +%s; }
mt()  { if [ $MAC = 1 ]; then stat -f %m "$1" 2>/dev/null; else stat -c %Y "$1" 2>/dev/null; fi; }
sz()  { if [ $MAC = 1 ]; then stat -f %z "$1" 2>/dev/null; else stat -c %s "$1" 2>/dev/null; fi; }
fmt() { if [ $MAC = 1 ]; then date -r "$1" "+$2"; else date -d "@$1" "+$2"; fi; }
hfile() { if command -v shasum >/dev/null 2>&1; then shasum -a 1 "$1" | cut -c1-16; else sha1sum "$1" | cut -c1-16; fi; }
hstr()  { if command -v shasum >/dev/null 2>&1; then printf '%s' "$1" | shasum -a 1 | cut -c1-10; else printf '%s' "$1" | sha1sum | cut -c1-10; fi; }
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [v$VERSION] $*" >> "$LOG"; }
alert() { printf 'ALERT\t%s\t%s\n' "$1" "$2"; log "通知: $1 / $2"; }
mb() { awk -v s="$1" 'BEGIN{printf "%.1fMB", s/1048576}'; }

bkroot() {
  if [ -d "$EXT" ] && [ -w "$EXT" ]; then echo "$EXT/ARA安全装置/バックアップ"; else echo "$APPDIR/バックアップ"; fi
}
allroots() { echo "$EXT/ARA安全装置/バックアップ"; echo "$APPDIR/バックアップ"; }

logic_pid() {
  if [ -n "$ARA_TEST_PID" ]; then echo "$ARA_TEST_PID"; return; fi
  p=$(pgrep -x "Logic Pro" 2>/dev/null | head -1)
  [ -z "$p" ] && p=$(pgrep -x "Logic Pro X" 2>/dev/null | head -1)
  echo "$p"
}

# --- Rosetta チェック（Logic起動ごとに1回） ---
rosetta_check() {
  local pid flags tr_flag lsof_out tr_lsof
  pid="$1"
  [ "$(cat "$STATE/rosetta_pid" 2>/dev/null)" = "$pid" ] && return 0
  echo "$pid" > "$STATE/rosetta_pid"
  [ $MAC = 1 ] || return 0
  [ "$(uname -m)" = "arm64" ] || return 0
  sleep 3
  flags=$(ps -o flags= -p "$pid" 2>/dev/null | tr -d ' ')
  tr_flag=0
  if [ -n "$flags" ] && [ $(( 0x$flags & 0x20000 )) -ne 0 ]; then tr_flag=1; fi
  lsof_out=$(lsof -p "$pid" 2>/dev/null)
  tr_lsof=0
  echo "$lsof_out" | grep -qi -e 'RosettaRuntime' -e '/oah/' && tr_lsof=1
  log "Logic起動 pid=$pid flags=$flags rosetta(flags)=$tr_flag rosetta(lsof)=$tr_lsof lsof行数=$(echo "$lsof_out" | wc -l | tr -d ' ')"
  if [ $tr_flag = 0 ] && [ $tr_lsof = 0 ] && [ -n "$flags" ]; then
    alert "LogicがRosettaなしで起動しています" "Melodyne(ARA)の補正が読み込めず、保存すると消えるおそれがあります。保存せずにLogicを終了し、アプリケーション→Logic Proを右クリック→情報を見る→「Rosettaを使用して開く」にチェックしてから開き直してください。"
  fi
}

refresh_projects() {
  local last
  last=$(cat "$STATE/projects.time" 2>/dev/null || echo 0)
  if [ "$1" != "force" ] && [ $(( $(now) - last )) -lt $LIST_REFRESH_SEC ] && [ -f "$STATE/projects.txt" ]; then return; fi
  find -H "$HOME_DIR/Desktop" "$HOME_DIR/Documents" "$HOME_DIR/Music" -maxdepth 9 \
    \( -name 'Media' -o -name '*.fcpbundle' -o -name '*.photoslibrary' -o -name 'Logic Pro Library.bundle' \
       -o -name 'Audio Music Apps' -o -name 'ARA安全装置' -o -name 'Library' -o -name '.Trash' -o -name 'node_modules' \
       -o -name 'Project File Backups' -o -name '*.app' \) -prune \
    -o \( -type d -name '*.logicx' -print -prune \) 2>/dev/null > "$STATE/projects.tmp"
  mv "$STATE/projects.tmp" "$STATE/projects.txt"
  now > "$STATE/projects.time"
}

keydir_for() {  # $1 = Alternatives/NNN のパス
  local alt pkg name altn
  alt="$1"; pkg=$(dirname "$(dirname "$alt")")
  name=$(basename "$pkg" .logicx); altn=$(basename "$alt")
  echo "$(bkroot)/${name}__${altn}__$(hstr "$alt")"
}

# --- 1世代とっておく ---  $1=alt $2=メモ $3=force
snapshot() {
  local alt note force arc kd idx h s lastline lh lsz snap tmp pname older when first
  alt="$1"; note="$2"; force="$3"
  arc="$alt/ARA Files/ARA Archive.1"
  [ -f "$arc" ] || return 2
  kd="${4:-$(keydir_for "$alt")}"; mkdir -p "$kd"; echo "$alt" > "$kd/path.txt"
  idx="$kd/index.tsv"; touch "$idx"
  h=$(hfile "$arc"); s=$(sz "$arc")
  lastline=$(tail -n 1 "$idx")
  lh=$(echo "$lastline" | cut -f4); lsz=$(echo "$lastline" | cut -f3)
  if [ -z "$force" ] && [ -n "$lh" ] && [ "$h" = "$lh" ]; then return 1; fi
  snap=$(date +%Y-%m-%d_%H%M%S)
  [ -e "$kd/$snap" ] && snap="${snap}_$$"
  tmp="$kd/.tmp_$snap"; rm -rf "$tmp"; mkdir -p "$tmp"
  if ! cp -Rp "$alt/ARA Files" "$tmp/" || ! cp -p "$alt/ProjectData" "$tmp/"; then
    rm -rf "$tmp"; log "コピー失敗: $alt"; return 2
  fi
  cp -p "$alt/MetaData.plist" "$tmp/" 2>/dev/null
  if [ "$(hfile "$tmp/ARA Files/ARA Archive.1")" != "$h" ]; then
    rm -rf "$tmp"; log "コピー中に変化したので次回やり直し: $alt"; return 2
  fi
  mv "$tmp" "$kd/$snap"
  pname=$(basename "$(dirname "$(dirname "$alt")")" .logicx)
  if [ -z "$force" ] && [ -n "$lh" ]; then
    older=$(sed '$d' "$idx" | cut -f4 | grep -x "$h" | head -1)
    if [ -n "$older" ]; then
      when=$(awk -F'\t' -v h="$h" '$4==h{w=$1} END{print w}' "$idx")
      alert "補正が古い版に戻った疑い（$pname）" "保存されたMelodyneのデータが、以前の版（$when）と完全に同じ中身です。補正が巻き戻っていないか確認してください。消えていたら、これ以上保存せずにLogicを閉じ、ARA安全装置の「補正を戻す」で直前の版に戻せます。"
      note="${note}巻き戻り疑い"
    elif [ -n "$lsz" ] && [ "$lsz" -gt 0 ] && [ $(( s * 100 )) -lt $(( lsz * 60 )) ]; then
      alert "補正データが急に減りました（$pname）" "Melodyneのデータが $(mb "$lsz") → $(mb "$s") に減りました。補正が消えていないか確認してください。消えていたら、これ以上保存せずにLogicを閉じ、ARA安全装置の「補正を戻す」で直前の版に戻せます。"
      note="${note}急減"
    fi
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$snap" "$(now)" "$s" "$h" "$note" >> "$idx"
  log "保存: $pname [$snap] $(mb "$s") $note"
  # 古い世代を整理
  while [ "$(wc -l < "$idx" | tr -d ' ')" -gt $KEEP ]; do
    first=$(head -n 1 "$idx" | cut -f1)
    [ -n "$first" ] && rm -rf "$kd/$first"
    tail -n +2 "$idx" > "$idx.tmp" && mv "$idx.tmp" "$idx"
  done
  return 0
}

scan() {
  local pkg alt arc kid m s lm pend r
  while IFS= read -r pkg; do
    [ -d "$pkg/Alternatives" ] || continue
    for alt in "$pkg"/Alternatives/*; do
      arc="$alt/ARA Files/ARA Archive.1"
      [ -f "$arc" ] || continue
      kid=$(hstr "$alt")
      m=$(mt "$arc"); s=$(sz "$arc")
      lm=$(cat "$STATE/$kid.m" 2>/dev/null)
      if [ -z "$lm" ]; then
        # 初めて見るプロジェクト：今の状態を基準としてとっておく
        if [ $(( $(now) - m )) -ge $STABLE_SEC ]; then
          snapshot "$alt" "初回" "" ; r=$?
          [ $r -ne 2 ] && echo "$m" > "$STATE/$kid.m"
        fi
        continue
      fi
      [ "$m" -gt "$lm" ] || continue
      pend=$(cat "$STATE/$kid.p" 2>/dev/null)
      if [ "$pend" != "$m $s" ] || [ $(( $(now) - m )) -lt $STABLE_SEC ]; then
        echo "$m $s" > "$STATE/$kid.p"; continue
      fi
      snapshot "$alt" "" ""; r=$?
      [ $r -ne 2 ] && echo "$m" > "$STATE/$kid.m"
    done
  done < "$STATE/projects.txt"
}

cmd_tick() {
  local pid lr
  pid=$(logic_pid)
  if [ -n "$pid" ]; then
    now > "$STATE/last_running"
    rosetta_check "$pid"
  else
    rm -f "$STATE/rosetta_pid"
    lr=$(cat "$STATE/last_running" 2>/dev/null || echo 0)
    [ $(( $(now) - lr )) -gt $AFTER_QUIT_SEC ] && return 0
  fi
  refresh_projects
  scan
}

cmd_init() {
  log "起動"
  refresh_projects force
  scan
}

cmd_projects() {  # 出力: キーdir \t 表示名
  allroots | while IFS= read -r root; do
    [ -d "$root" ] || continue
    where="Mac本体"; case "$root" in "$EXT"*) where="外付け";; esac
    for kd in "$root"/*__*; do
      [ -f "$kd/index.tsv" ] || continue
      n=$(wc -l < "$kd/index.tsv" | tr -d ' ')
      [ "$n" -gt 0 ] || continue
      le=$(tail -n 1 "$kd/index.tsv" | cut -f2)
      name=$(basename "$kd"); name="${name%%__*}"
      printf '%s\t%s\t%s（%s件・最新 %s・%s）\n' "$le" "$kd" "$name" "$n" "$(fmt "$le" '%m/%d %H:%M')" "$where"
    done
  done | sort -rn | cut -f2-
}

cmd_snaps() {  # 出力: 世代名 \t 表示名（新しい順）
  local kd="$1"
  awk -F'\t' '{a[NR]=$0} END{for(i=NR;i>0;i--) print a[i]}' "$kd/index.tsv" | while IFS="$TAB" read -r snap ep s h note; do
    [ -d "$kd/$snap" ] || continue
    printf '%s\t%s  %s  %s\n' "$snap" "$(fmt "$ep" '%m/%d %H:%M:%S')" "$(mb "$s")" "$note"
  done
}

cmd_restore() {
  local kd snap alt src h s
  kd="$1"; snap="$2"
  if [ -n "$(logic_pid)" ]; then echo "ERR Logicが起動中です"; return; fi
  alt=$(cat "$kd/path.txt" 2>/dev/null)
  src="$kd/$snap"
  [ -d "$alt" ] || { echo "ERR 元のプロジェクトが見つかりません: $alt"; return; }
  [ -f "$src/ARA Files/ARA Archive.1" ] || { echo "ERR バックアップが見つかりません"; return; }
  snapshot "$alt" "復元前" "force" "$kd"
  rm -rf "$alt/ARA Files.restoring"
  cp -Rp "$src/ARA Files" "$alt/ARA Files.restoring" || { echo "ERR コピーに失敗しました"; return; }
  rm -rf "$alt/ARA Files" && mv "$alt/ARA Files.restoring" "$alt/ARA Files"
  cp -p "$src/ProjectData" "$alt/ProjectData"
  [ -f "$src/MetaData.plist" ] && cp -p "$src/MetaData.plist" "$alt/MetaData.plist"
  h=$(hfile "$alt/ARA Files/ARA Archive.1"); s=$(sz "$alt/ARA Files/ARA Archive.1")
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date +%Y-%m-%d_%H%M%S)_r" "$(now)" "$s" "$h" "復元: $snap" >> "$kd/index.tsv"
  # 戻したファイルを「新しい保存」と見なさないよう基準を更新
  now > "$STATE/$(hstr "$alt").m"
  log "復元: $alt ← $snap"
  echo "OK"
}

cmd_status() {
  local root where n last pid
  root=$(bkroot)
  where="外付け（Extreme SSD）"; case "$root" in "$APPDIR"*) where="Mac本体（外付けが見つからないため）";; esac
  n=$(cmd_projects | wc -l | tr -d ' ')
  last=$(grep '保存:' "$LOG" 2>/dev/null | tail -n 1 | cut -c1-16)
  pid=$(logic_pid)
  echo "見張り中です（v$VERSION）"
  echo "バックアップ先：$where"
  echo "とってあるプロジェクト：${n}件"
  echo "最後にとった時刻：${last:-まだなし}"
  [ -n "$pid" ] && echo "Logic：起動中" || echo "Logic：起動していません"
}

case "$1" in
  tick) cmd_tick ;;
  init) cmd_init ;;
  projects) cmd_projects ;;
  snaps) cmd_snaps "$2" ;;
  restore) cmd_restore "$2" "$3" ;;
  status) cmd_status ;;
  logic-running) [ -n "$(logic_pid)" ] && echo 1 || echo 0 ;;
  log) log "$2" ;;
  version) echo "$VERSION" ;;
esac
exit 0
