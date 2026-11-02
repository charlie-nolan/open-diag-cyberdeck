#!/bin/bash
# Runs beside build_charm_all.sh: gives each finished make its NHTSA TSBs/complaints/recalls (CPU only, seconds),
# then, once the CHARM loop is done and the GPU is free, embeds the new knowledge rows (embed --missing) per DB.
#   setsid nohup bash ~/subaru_cyberdeck/tools/nhtsa_follow.sh >> ~/subaru_cyberdeck/logs/nhtsa_follow.log 2>&1 &
set -o pipefail
P=~/subaru_cyberdeck/venv/bin/python
KB=~/subaru_cyberdeck/kb
ALT="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}/kb_build"
export HF_HUB_OFFLINE=1
while pgrep -f build_charm_all.sh >/dev/null; do
    $P -I "$KB/add_nhtsa.py" --done
    sleep 300
done
$P -I "$KB/add_nhtsa.py" --done
echo "=== $(date -Is) CHARM loop finished: embedding NHTSA rows"
for mark in "$KB"/.*.nhtsa; do
    name=$(basename "$mark" .nhtsa); name=${name#.}
    [ -f "$KB/.$name.nhtsa_embedded" ] && continue
    db="$KB/$name.db"; [ -f "$db" ] || db="$ALT/$name.db"
    for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop "$m"; done
    if $P "$KB/build_subaru_kb.py" --db "$db" embed --missing --only knowledge 2>&1 | grep -E "^embed:|Error|Traceback" &&
       $P "$KB/build_subaru_kb.py" --db "$db" finish 2>&1 | tail -1; then
        touch "$KB/.$name.nhtsa_embedded"; echo "=== OK $name"
    else
        echo "=== FAIL $name"
    fi
done
echo "=== ALL DONE $(date -Is)"
