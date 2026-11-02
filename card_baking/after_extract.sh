#!/usr/bin/env bash
# Everything after the forum extract, in order, unattended:
#   wait for extract -> backup subaru.db -> builder diag (forum fixes into subaru.db) -> bake cards
#   -> builder cards -> builder finish -> embed new/changed rows (after finish, so card rowids stay put)
# Detached:
#   setsid nohup ~/subaru_cyberdeck/card_baking/after_extract.sh > ~/subaru_cyberdeck/logs/after_extract.log 2>&1 &
set -uo pipefail
ME="${USER:-$(id -un)}"
CK=~/subaru_cyberdeck
KB=$CK/kb
cd "$(dirname "$0")"
log() { echo "$(date '+%F %T') $*"; }
step() { log "START $1"; shift; "$@" || { log "FAILED, stopping here"; exit 1; }; }

while pgrep -u "$ME" -f -- 'run_bake\.sh extract|extract_fixes\.py' >/dev/null; do
  log "waiting: forum extract still running"; sleep 300
done
grep -q "extract done" "$CK/logs/bake_extract.log" || { log "extract log has no 'extract done', stopping"; exit 1; }

step "backup subaru.db -> kb/subaru.pre_cards.db" cp "$KB/subaru.db" "$KB/subaru.pre_cards.db"
step "builder diag (forum fixes, TSBs into subaru.db)" python3 "$KB/build_subaru_kb.py" diag "$CK/subaru_diag.db"
step "bake cards (long, log logs/serve_cards.log)" ./run_bake.sh cards
step "builder cards" python3 "$KB/build_subaru_kb.py" cards "$KB/cards.db"
step "builder finish" python3 "$KB/build_subaru_kb.py" finish

PY=""
for p in "$CK/venv/bin/python" python3 ~/rkllm-env/bin/python; do
  "$p" -c "import sentence_transformers, sqlite_vec" 2>/dev/null && { PY="$p"; break; }
done
if [[ -z "$PY" ]]; then
  log "embed skipped: no python here has sentence-transformers + sqlite-vec. Everything else is done."
  exit 0
fi
step "embed new rows with $PY" "$PY" "$KB/build_subaru_kb.py" embed --missing
log "ALL DONE. subaru.db ready to copy to the Pi."
