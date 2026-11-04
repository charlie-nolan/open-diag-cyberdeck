#!/bin/bash
# Build one knowledge DB per make from the mounted CHARM text image: charm -> finish -> embed (GPU) -> finish.
# Reads kb/charm_make_groups.txt ("db|Make,Make Truck"). Skips subaru (built separately) and any make
# with a kb/.<db>.done marker, so re-running continues where it stopped.
#   setsid nohup bash tools/build_charm_all.sh >> logs/charm_text_all.log 2>&1 &
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
set -o pipefail
# one copy at a time: two loops writing the same DBs gave 'database is locked' and GPU out-of-memory (Oct 23)
exec 8>$CK/.charm_loop.lock
flock -n 8 || { echo "another build_charm_all.sh is already running"; exit 1; }
P=$CK/venv/bin/python
B=$CK/kb/build_subaru_kb.py
KB=$CK/kb
ALT="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}/kb_build"
export HF_HUB_OFFLINE=1   # the embedding model is already cached
STEP_TIMEOUT=${STEP_TIMEOUT:-24h}     # a hung step is killed after this; that make fails, the loop goes on
failed=""
# step PATTERN ARGS...: run one builder step on $out, show matching lines (or the last line when PATTERN
# is empty), return the builder's own exit code.
step() {
    local pat=$1 rc; shift
    if [ -n "$pat" ]; then
        timeout -k 60 "$STEP_TIMEOUT" "$P" "$B" --db "$out" "$@" < /dev/null 2>&1 | { grep -E "$pat" || true; }; rc=${PIPESTATUS[0]}
    else
        timeout -k 60 "$STEP_TIMEOUT" "$P" "$B" --db "$out" "$@" < /dev/null 2>&1 | tail -1; rc=${PIPESTATUS[0]}
    fi
    return "$rc"
}
while IFS='|' read -r db makes; do
    [ -z "$db" ] && continue
    case "$db" in subaru) echo "skip $db (built separately)"; continue;; esac
    [ -f "$KB/.$db.done" ] && { echo "skip $db (done)"; continue; }
    dir=$KB
    if [ "$(df --output=avail -B1G / | tail -1)" -lt 50 ]; then dir=$ALT; mkdir -p "$dir"; fi
    out="$dir/$db.db"
    echo "=== $(date -Is) $db [$makes] -> $out"
    t0=$(date +%s)
    for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop "$m"; echo "stopped ollama $m"; done
    # Success is the Python exit code (PIPESTATUS[0]); the grep/tail only trims what goes to the log.
    if step "^charm:|Error|Traceback" charm --makes "$makes" --years 1982-2013 &&
       step "" finish &&
       step "^embed:|Error|Traceback|CUDA" embed &&
       step "" finish; then
        touch "$KB/.$db.done"
        echo "=== OK $db $(du -h "$out" | cut -f1) in $(( ($(date +%s)-t0)/60 )) min"
    else
        echo "=== FAIL $db"
        failed="$failed $db"
    fi
done < "$KB/charm_make_groups.txt"
if [ -n "$failed" ]; then echo "=== DONE WITH FAILURES:$failed $(date -Is)"; exit 1; fi
echo "=== ALL DONE $(date -Is)"
