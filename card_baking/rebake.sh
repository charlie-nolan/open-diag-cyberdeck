#!/usr/bin/env bash
# Everything after the forum extract, in order, unattended:
#   wait for extract -> re-bucket fixes (recat) + export -> backup subaru.db -> builder diag (forum fixes into subaru.db) -> bake cards
#   -> builder cards -> builder finish -> embed new/changed rows (after finish, so card rowids stay put)
# Detached:
#   setsid nohup ~/subaru_cyberdeck/card_baking/rebake.sh > ~/subaru_cyberdeck/logs/rebake.log 2>&1 &
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

step "re-bucket forum fixes from their part/fix text" python3 extract_fixes.py recat
step "export fixes to subaru_diag.db" python3 extract_fixes.py export --diag-db "$CK/subaru_diag.db"
if [[ ! -e "$KB/subaru.pre_cards.db" ]]; then
  step "backup subaru.db -> kb/subaru.pre_cards.db" cp "$KB/subaru.db" "$KB/subaru.pre_cards.db"
fi
step "builder diag (forum fixes, TSBs into subaru.db)" python3 "$KB/build_subaru_kb.py" diag "$CK/subaru_diag.db"
if [[ -f "$KB/cards.db" ]]; then   # cards from an older run are set aside, not mixed in
  step "set old cards aside -> kb/cards.old.db" mv -f "$KB/cards.db" "$KB/cards.old.db"
fi
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
# card rows are replaced on each load but keep the same rowids, so --missing would keep the old cards' vectors
step "re-embed all cards" "$PY" "$KB/build_subaru_kb.py" embed --only dtc_card
log "ALL DONE. subaru.db ready to copy to the Pi."
