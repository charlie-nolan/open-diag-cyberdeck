#!/bin/bash
# One screen of what's running on the PC.
#   bash ~/subaru_cyberdeck/tools/status.sh            # everything
#   bash ~/subaru_cyberdeck/tools/status.sh charm      # one job: charm | nhtsa | telemetry | photos | reddit | dbs | disk | gpu | ollama
CK=~/subaru_cyberdeck
L=$CK/logs
SEA="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}"
up() { pgrep -f "$1" >/dev/null && echo "running" || echo "stopped"; }
bar() { tail -c 4000 "$1" 2>/dev/null | tr '\r' '\n' | grep -a '^\[#' | tail -1; }

charm() {
    local done total cur
    done=$(ls $CK/kb/.*.done 2>/dev/null | wc -l); total=$(grep -c . $CK/kb/charm_make_groups.txt)
    cur=$(grep -a "^=== 20" $L/charm_text_all.log | tail -1 | sed -E 's/^=== ([^ ]+) ([^ ]+).*/\2 since \1/')
    echo "CHARM per-make build: $(up build_charm_all.sh), $done of $((total - 1)) makes done (+subaru, built separately); now: $cur"
    local todo; todo=$(for g in $(cut -d'|' -f1 $CK/kb/charm_make_groups.txt); do [ "$g" = subaru ] || [ -f $CK/kb/.$g.done ] || printf "%s " "$g"; done)
    [ -n "$todo" ] && echo "  not done yet: $todo"
    echo "  ETA: roughly 1.5-3 h per make (big makes like Ford/Chevy longer). Log: $L/charm_text_all.log"
}
nhtsa() { echo "NHTSA follow: $(up nhtsa_follow.sh), $(ls $CK/kb/.*.nhtsa 2>/dev/null | wc -l) makes have NHTSA rows; last: $(grep -a -v '^===' $L/nhtsa_follow.log | tail -1 | cut -c1-90)"; }
photos() {
    local b; b=$(bar $L/charm_images_dl.log)
    echo "CHARM photos: $(up 'aria2c --select-file=23') ${b:-no progress line yet}  (file: $SEA/charm/operation-charm/lmdb-images/data.mdb)"
}
reddit() {
    echo "Reddit pull: $(up reddit_all_makes.sh) $(bar $L/reddit_all_makes.log)"
    grep -a "^=== 20" $L/reddit_all_makes.log | tail -1 | sed 's/^/  now: /'
    for f in $CK/raw/reddit/reddit_*.db; do
        [ -f "$f" ] && printf "  %-28s %6s posts\n" "$(basename "$f")" "$(sqlite3 -readonly "$f" 'SELECT count(*) FROM reddit_post' 2>/dev/null)"
    done | head -40
}
community() {
    echo "Community DB (kb/build_community.py): extract $(up 'build_community.py.* extract')"
    for f in $CK/kb/community/work/*.work.db; do
        [ -f "$f" ] || continue
        printf "  %-14s %s\n" "$(basename "$f" .work.db)" "$(sqlite3 -readonly "$f" "SELECT (SELECT count(*) FROM thread) || ' kept, ' || (SELECT count(*) FROM thread WHERE pilot=1) || ' pilot, ' || (SELECT count(*) FROM extract WHERE status='ok') || ' extracted'" 2>/dev/null)"
    done
    for f in $L/community_*.log; do [ -f "$f" ] && grep -a "stats:" "$f" | tail -1 | cut -c1-200 | sed "s|^|  $(basename "$f"): |"; done
}
dbs() {
    echo "Make DBs (kb/ and Seagate kb_build/):"
    ls -la $CK/kb/*.db "$SEA"/kb_build/*.db 2>/dev/null | grep -vE "pre_|bak|bake_work|cards|nhtsa_all|vin\.db|yards|romid" |
        awk '{printf "  %-22s %6.0f MB\n", gensub(".*/", "", 1, $9), $5/1e6}'
}
disk() { df -h --output=avail,size,target / "$SEA" 2>/dev/null | awk 'NR>1{a=$1; s=$2; $1=$2=""; print "Disk" $0 ": " a " free of " s}'; }
gpu() { echo "GPU: $(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader 2>/dev/null)"; }
telemetry() {   # kb/build_telemetry.py: per-make telemetry DBs and whether each is merged into its make DB
    echo "Telemetry build: $(up build_telemetry.py), $(ls $CK/kb/telemetry/*.telemetry.db 2>/dev/null | wc -l) make DBs in kb/telemetry/"
    for f in $(ls -t $CK/logs/telemetry_*.log 2>/dev/null | grep -v '\.run\.log' | head -3); do echo "  $(basename $f .log): $(tail -1 $f | cut -c1-110)"; done
}
ollama_() { echo "Ollama loaded: $(ollama ps 2>/dev/null | awk 'NR>1{printf "%s ", $1}')"; [ -z "$(ollama ps 2>/dev/null | awk 'NR>1')" ] && echo "  (none)"; }

case "${1:-all}" in
    telemetry) telemetry;; charm) charm;; nhtsa) nhtsa;; photos) photos;; reddit) reddit;; community) community;; dbs) dbs;; disk) disk;; gpu) gpu;; ollama) ollama_;;
    all) charm; nhtsa; photos; reddit | head -3; telemetry; dbs; disk; gpu; ollama_;;
    *) sed -n 2,4p "$0";;
esac
