#!/usr/bin/env bash
# One command for the whole bake. Before each job it waits for the manual conversion / embedding runs to
# finish (they come first in the GPU queue), then clears the GPU, CPU and RAM with free_resources.sh,
# starts that job's llama-server, runs the job, and shuts the server down again.
#
#   ./run_bake.sh sample        job 1 on 100 threads, prints the projected total time
#   ./run_bake.sh extract       job 1, all threads, then export to subaru_diag.db
#                               (waits for the forum scrapers to exit first; SCRAPE_PATTERN overrides)
#   ./run_bake.sh cards         job 2 for the Forester (MODEL_ARGS to change car/years)
#   ./run_bake.sh all           extract, then cards
# Detached, so it survives closing the terminal:
#   setsid nohup ./run_bake.sh all > logs/bake.log 2>&1 &
# NO_WAIT=1 stops conversion/embedding runs instead of waiting for them.
set -euo pipefail
ME="${USER:-$(id -un)}"
cd "$(dirname "$0")"
STEP="${1:-sample}"
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
REDDIT="${REDDIT:-$CK/raw/reddit/reddit_subaru.db}"
FORUM="${FORUM:-$CK/raw/forums/ultimatesubaru.db}"
DIAG_DB="${DIAG_DB:-$CK/subaru_diag.db}"
MODEL_ARGS="${MODEL_ARGS:---model Forester --years 1998-2002}"
mkdir -p "$CK/logs"
SERVER=""

stop_server() { [[ -n "$SERVER" ]] && kill "$SERVER" 2>/dev/null && wait "$SERVER" 2>/dev/null; SERVER=""; }
trap stop_server EXIT INT TERM

wait_for_queue() {
  local q='convert_big_manuals|marker_single|marker |build_subaru_kb\.py.* embed'
  if [[ -z "${NO_WAIT:-}" ]]; then
    while pgrep -u "$ME" -f -- "$q" >/dev/null; do
      echo "$(date +%T) waiting: manual conversion / embedding still running"; sleep 300
    done
  fi
  # the database builder writing subaru.db is never stopped; the bake reads that file, so always wait
  while pgrep -u "$ME" -f -- 'build_subaru_kb\.py|charm_ingest' >/dev/null; do
    echo "$(date +%T) waiting: database builder is writing subaru.db"; sleep 120
  done
}

start_server() {   # $1 = serve script, $2 = port
  wait_for_queue
  ./free_resources.sh --yes
  "./$1" > "$CK/logs/${1%.sh}.log" 2>&1 &
  SERVER=$!
  echo "$(date +%T) starting $1 (log $CK/logs/${1%.sh}.log)"
  for _ in $(seq 360); do   # model download + load can take a while the first time
    curl -sf "http://127.0.0.1:$2/health" >/dev/null && { echo "$(date +%T) server ready"; return; }
    kill -0 "$SERVER" 2>/dev/null || { echo "llama-server exited, see the log"; exit 1; }
    sleep 5
  done
  echo "llama-server not ready after 30 min"; exit 1
}

wait_for_scrape() {   # job 1 reads the forum DBs, so it starts only once the scrapers have exited
  local q="${SCRAPE_PATTERN:-wayback|ultimatesubaru|scrape_reddit}"
  while pgrep -u "$ME" -f -- "$q" | grep -vx "$$" >/dev/null; do
    echo "$(date +%T) waiting: forum scrape still running"; sleep 600
  done
}

filter() {
  wait_for_scrape
  [[ -f "$FORUM" ]] && F=(--forum "$FORUM") || F=()
  python3 extract_fixes.py filter --reddit "$REDDIT" "${F[@]}"
}

case "$STEP" in
  sample)
    filter
    start_server serve_extract.sh 8080
    python3 extract_fixes.py run --limit 100
    python3 extract_fixes.py stats ;;
  extract)
    filter
    start_server serve_extract.sh 8080
    python3 extract_fixes.py run
    python3 extract_fixes.py export --diag-db "$DIAG_DB" ;;
  cards)
    start_server serve_cards.sh 8081
    # shellcheck disable=SC2086
    python3 bake_cards.py $MODEL_ARGS ;;
  all)
    "$0" extract && "$0" cards ;;
  *) echo "usage: $0 sample|extract|cards|all"; exit 2 ;;
esac
echo "$(date +%T) $STEP done"
