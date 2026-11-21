#!/bin/bash
# アプリ置き場の自動更新（Mac）
#  GitHub arumonwizard-star/apps の main が新しくなっていたら、Mac のアプリに反映する。
#  Light Operator の見張り役から5分ごとに呼ばれる。画面・通知は一切出さない。
A="$HOME/Applications"; D="$A/.apps-updater"
mkdir -p "$D"; cd "$D" || exit 0
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
log() { echo "[$(date '+%m/%d %H:%M:%S')] $*" >> update.log; }
# 二重に動かない
mkdir lock 2>/dev/null || { [ -n "$(find lock -maxdepth 0 -mmin +30 2>/dev/null)" ] && rm -rf lock && mkdir lock; } || exit 0
trap 'rm -rf "$D/lock" "$D/tmp"' EXIT

SHA=$(curl -fsS -m 15 -H "Accept: application/vnd.github.sha" https://api.github.com/repos/arumonwizard-star/apps/commits/main) || exit 0
case "$SHA" in *[!0-9a-f]*|"") exit 0;; esac
[ "$SHA" = "$(cat sha 2>/dev/null)" ] && exit 0

rm -rf tmp; mkdir tmp
curl -fsSL -m 300 "https://codeload.github.com/arumonwizard-star/apps/tar.gz/$SHA" | tar xzf - -C tmp --strip-components 1 || { log "取得に失敗"; exit 0; }
S="$D/tmp"
FAIL=0
# 中身が違うファイルだけ上書きする（消すことはしない）。3つ目以降は入れ替えない名前
sync() {
  local src="$1" dst="$2"; shift 2
  [ -d "$src" ] && [ -d "$dst" ] || return 0
  local n=0 f rel skip x
  while IFS= read -r -d '' f; do
    rel="${f#$src/}"; skip=0
    for x in "$@"; do case "/$rel/" in */"$x"/*) skip=1;; esac; done
    [ $skip = 1 ] && continue
    if [ -d "$f" ]; then mkdir -p "$dst/$rel" || FAIL=1; continue; fi
    cmp -s "$f" "$dst/$rel" && continue
    mkdir -p "$(dirname "$dst/$rel")" && cp -p "$f" "$dst/$rel.new" && mv -f "$dst/$rel.new" "$dst/$rel" && n=$((n+1)) || FAIL=1
  done < <(find "$src" -mindepth 1 -print0)
  [ $n -gt 0 ] && log "反映: $dst（$n 個）"
  return 0
}

# Light Operator（見張り役 supervisor.sh と鍵は入れ替えない）
sync "$S/light-operator" "$A/.lightoperator/engine" supervisor.sh opkey.txt topic.txt cloudflared
# CH Cut（プリセットはアプリ側で書き換わるので入れ替えない）
sync "$S/ch-cut" "$A/CHCut" presets __pycache__
# ARA安全装置・X-FILE 同期
sync "$S/ara-safety/ARA安全装置.app" "$A/ARA安全装置.app"
sync "$S/xfile-sync/X-FILE 同期.app" "$A/X-FILE 同期.app"
# この仕組み自体
[ -f "$S/mac-updater/update.sh" ] && cp "$S/mac-updater/update.sh" "$D/update.sh.new" && chmod +x "$D/update.sh.new" && mv "$D/update.sh.new" "$D/update.sh"

[ $FAIL = 0 ] || { log "一部の反映に失敗（次回やり直し）"; exit 0; }
echo "$SHA" > sha
log "版 $SHA に更新"
tail -n 300 update.log > update.log.tmp && mv update.log.tmp update.log
exit 0
