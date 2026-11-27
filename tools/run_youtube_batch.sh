#!/usr/bin/env bash
set -euo pipefail

export PATH=/usr/local/bin:/usr/bin:/bin
export PYTHONUNBUFFERED=1
CK="${CYBERDECK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${CYBERDECK_PYTHON:-$CK/venv/bin/python}"
[[ -x "$PY" ]] || PY="python3"
ING="$CK/kb/ingest_youtube.py"
CACHE="${CYBERDECK_YT_CACHE:-$CK/cache/youtube}"
RAW_DB="$CACHE/youtube_raw.db"
LOG="$CK/logs/youtube_ingest.log"
LOCK="/tmp/cyberdeck_yt_batch.lock"

mkdir -p "$(dirname "$LOG")" "$CACHE"
exec 9>"$LOCK"
flock -n 9 || exit 0

exec >> "$LOG" 2>&1
echo "=== $(date '+%F %T') YouTube polite window start (sleep=25s, cap=10) ==="

before=$(sqlite3 "$RAW_DB" "SELECT count(*) FROM video WHERE accepted=1" 2>/dev/null || echo 0)
hit_429=0
CAP="${YT_WINDOW_CAP:-10}"

remaining() {
    local now
    now=$(sqlite3 "$RAW_DB" "SELECT count(*) FROM video WHERE accepted=1" 2>/dev/null || echo "$before")
    echo $(( CAP - (now - before) ))
}

rem=$(remaining)
if [[ $rem -gt 0 ]]; then
    echo "--- $(date '+%F %T') curated channels (remaining cap=$rem) ---"
    rc=0
    "$PY" "$ING" channels --limit-per-channel 200 --with-comments --max-comments 12 \
        --sleep 25 --max-captions "$rem" || rc=$?
    if [[ $rc -eq 42 ]]; then
        echo "--- $(date '+%F %T') pausing window cleanly after HTTP 429 ---"
        hit_429=1
    fi
fi

MAKES=(
    subaru chevrolet toyota honda ford nissan jeep dodge hyundai kia gmc
    mazda volkswagen acura lexus bmw volvo audi mercedes_benz
    chrysler buick cadillac lincoln scion infiniti mitsubishi mini
)
if [[ $hit_429 -eq 0 ]]; then
    for make in "${MAKES[@]}"; do
        rem=$(remaining)
        [[ $rem -le 0 ]] && break
        echo "--- $(date '+%F %T') discover-dtc: $make (remaining cap=$rem) ---"
        rc=0
        "$PY" "$ING" discover-dtc --make "$make" --top-codes 40 --search-n 5 --per-code 2 \
            --with-comments --max-comments 12 --sleep 25 --max-captions "$rem" || rc=$?
        if [[ $rc -eq 42 ]]; then
            echo "--- $(date '+%F %T') pausing window cleanly after HTTP 429 ---"
            hit_429=1
            break
        fi
    done
fi

echo "--- $(date '+%F %T') export-work ---"
"$PY" "$ING" export-work
after=$(sqlite3 "$RAW_DB" "SELECT count(*) FROM video WHERE accepted=1" 2>/dev/null || echo "$before")
echo "=== $(date '+%F %T') YouTube window finished: +$((after - before)) kept (total=$after, hit_429=$hit_429) ==="
