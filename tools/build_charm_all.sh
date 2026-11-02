#!/bin/bash
# Build one knowledge DB per make from the mounted CHARM text image: charm -> finish -> embed (GPU) -> finish.
# Reads kb/charm_make_groups.txt ("db|Make,Make Truck"). Skips subaru (built separately) and any make
# with a kb/.<db>.done marker, so re-running continues where it stopped.
#   setsid nohup bash ~/subaru_cyberdeck/tools/build_charm_all.sh >> ~/subaru_cyberdeck/logs/charm_text_all.log 2>&1 &
set -o pipefail
# one copy at a time: two loops writing the same DBs gave 'database is locked' and GPU out-of-memory (Oct 23)
exec 8>~/subaru_cyberdeck/.charm_loop.lock
flock -n 8 || { echo "another build_charm_all.sh is already running"; exit 1; }
P=~/subaru_cyberdeck/venv/bin/python
B=~/subaru_cyberdeck/kb/build_subaru_kb.py
KB=~/subaru_cyberdeck/kb
ALT="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}/kb_build"
export HF_HUB_OFFLINE=1   # the embedding model is already cached
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
    if $P "$B" --db "$out" charm --makes "$makes" --years 1982-2013 2>&1 | grep -E "^charm:|Error|error|Traceback" &&
       $P "$B" --db "$out" finish 2>&1 | tail -1 &&
       $P "$B" --db "$out" embed 2>&1 | grep -E "^embed:|Error|error|Traceback|CUDA" &&
       $P "$B" --db "$out" finish 2>&1 | tail -1; then
        touch "$KB/.$db.done"
        echo "=== OK $db $(du -h "$out" | cut -f1) in $(( ($(date +%s)-t0)/60 )) min"
    else
        echo "=== FAIL $db"
    fi
done < "$KB/charm_make_groups.txt"
echo "=== ALL DONE $(date -Is)"
