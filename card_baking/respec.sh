#!/usr/bin/env bash
# Redo only the spec/labor lines of the baked cards with the current kb_retriever, reload them, re-embed card vectors.
# No card model needed; runs at lowest priority so it stays out of the way of other jobs.
#   setsid nohup nice -n 19 bash card_baking/respec.sh > logs/respec.log 2>&1 &
set -uo pipefail
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
KB=$CK/kb
cd "$(dirname "$0")"
log() { echo "$(date '+%F %T') $*"; }
step() { log "START $1"; shift; "$@" || { log "FAILED, stopping here"; exit 1; }; }

step "respec cards.db (backup kb/cards.pre_respec.db)" python3 respec.py
step "builder cards" python3 "$KB/build_subaru_kb.py" cards "$KB/cards.db"
step "builder finish" python3 "$KB/build_subaru_kb.py" finish
step "re-embed all cards" "$CK/venv/bin/python" "$KB/build_subaru_kb.py" embed --only dtc_card
log "ALL DONE"
