#!/usr/bin/env bash
# Build a knowledge DB for ONE car: kb/cars/<make>_<model>_<year>.db, same layout as a make DB (kb/<make>.db),
# so the app and kb/kb_retriever.py read it unchanged. Runs on any Linux laptop; no GPU needed (--device cpu).
#
#   tools/build_car_db.sh --make <Make> --model <Model> --year <YYYY> \
#       [--fsm "test_batch_out/<manual>/<manual>.fixed.md" --chassis <code> --engines <code[,code]> [--title "<manual>"]] \
#       [--device cpu|cuda] [--reuse-vectors] [--no-embed] [--charm-images DIR] [--no-images] [--dry-run]
#
# Steps (each skipped with one line when its source is missing):
#   a fsm        build_subaru_kb.py fsm  (only with --fsm)
#   b charm      build_subaru_kb.py charm --only "<year> <model>" when CHARM text is mounted (~/mnt/charm/data.mdb);
#                else the car's CHARM pages are copied from kb/<make>.db (kb/car_db.py copy-docs, read-only)
#   b2 fsm-copy  no --fsm: the car's FSM pages are copied from kb/<make>.db if it has any
#   c nhtsa+     kb/car_db.py extras: add_nhtsa (kb/nhtsa_all.db), add_investigations, add_stackexchange
#   d diag       build_subaru_kb.py diag <make>_diag.db (DTC names, SSM params, TSB/NHTSA, forum fixes) if present
#   e cards      build_subaru_kb.py cards kb/cards_<make>.db (or kb/cards.db when it holds this model)
#   f community  kb/car_db.py community   (comm_* rows for this car from kb/<make>.db)
#   g telemetry  kb/car_db.py telemetry   (telem_* rows for this car from kb/<make>.db)
#   h trim       kb/car_db.py trim        (other cars' rows out; model-less make-wide rows stay)
#   i embed      build_subaru_kb.py embed --device cpu|cuda   (--reuse-vectors first copies identical rows' vectors
#                from kb/<make>.db, then embeds only the rest; --no-embed: keyword search only)
#   i2 images    kb/car_db.py images: manual figures + CHARM pictures + chart readings stored inside the DB
#                (CHARM pictures need the images LMDB: --charm-images <dir with data.mdb>; --no-images skips)
#   j finish     build_subaru_kb.py finish
#   k ann        skipped: one car is far under the 100k rows where the retriever uses the ANN sidecar
#   l app view   kb/cars/<slug>/<make>.db symlink: point the app's "kb_dir" at that folder
# Existing DBs in kb/ are only read. Work happens in kb/cars/.work_<slug>/ (removed on success).
set -u -o pipefail
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PY=${PY:-$CK/venv/bin/python}; [ -x "$PY" ] || PY=python3
B=$CK/kb/build_subaru_kb.py
H=$CK/kb/car_db.py
export HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-1}

MAKE="" MODEL="" YEAR="" FSM="" CHASSIS="" ENGINES="" TITLE="" DEVICE=cpu REUSE=0 EMBED=1 DRY=0 KEEP=0 IMAGES=1
CHARM_DIR=${CHARM_DIR:-$HOME/mnt/charm}
CHARM_IMAGES=${CHARM_IMAGES:-}
FSM_DIR=${FSM_DIR:-$CK/test_batch_out}
usage() { sed -n '2,8p' "$0"; exit "${1:-1}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --make) MAKE=$2; shift 2;; --model) MODEL=$2; shift 2;; --year) YEAR=$2; shift 2;;
        --fsm) FSM=$2; shift 2;; --chassis) CHASSIS=$2; shift 2;; --engines) ENGINES=$2; shift 2;;
        --title) TITLE=$2; shift 2;; --device) DEVICE=$2; shift 2;; --charm) CHARM_DIR=$2; shift 2;;
        --charm-images) CHARM_IMAGES=$2; shift 2;; --no-images) IMAGES=0; shift;;
        --reuse-vectors) REUSE=1; shift;; --no-embed) EMBED=0; shift;; --dry-run) DRY=1; shift;;
        --keep-work) KEEP=1; shift;; -h|--help) usage 0;; *) echo "unknown option $1"; usage;;
    esac
done
[ -n "$MAKE" ] && [ -n "$MODEL" ] && [ -n "$YEAR" ] || usage
if [ -n "$FSM" ]; then
    [ -f "$FSM" ] || { echo "no such FSM file: $FSM"; exit 1; }
    [ -n "$CHASSIS" ] && [ -n "$ENGINES" ] || { echo "--fsm needs --chassis and --engines"; exit 1; }
    if [ -z "$TITLE" ]; then          # load_manuals.sh uses the manual's folder name as the title
        d=$(basename "$(dirname "$FSM")"); f=$(basename "$FSM"); f=${f%.md}; f=${f%.fixed}
        if [ "$d" = "$f" ]; then TITLE=$d; else TITLE=$f; fi
    fi
fi
mk=$(echo "$MAKE" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_|_$//g')
md=$(echo "$MODEL" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_|_$//g')
SLUG=${mk}_${md}_${YEAR}
MAKE_DB=$CK/kb/$mk.db
CARS=$CK/kb/cars
WORK=$CARS/.work_$SLUG
DB=$WORK/$mk.db                  # named <make>.db while building: the NHTSA/StackExchange loaders read the make from it
OUT=$CARS/$SLUG.db
CHARM_MAKES=$(awk -F'|' -v m="$mk" '$1==m{print $2}' "$CK/kb/charm_make_groups.txt" 2>/dev/null)
[ -n "$CHARM_MAKES" ] || CHARM_MAKES=$MAKE

T0=$(date +%s); TS=$T0
say() { echo "$(date '+%F %T') $*"; }
lap() { local now; now=$(date +%s); say "   ($1: $((now - TS)) s)"; TS=$now; }
run() {   # run CMD...: print it; in --dry-run only print
    say "+ $*"
    [ "$DRY" = 1 ] && return 0
    "$@"
}
skip() { say "skip $1: $2"; }

say "== one-car DB: $MAKE $MODEL $YEAR -> $OUT${DRY:+ }$([ "$DRY" = 1 ] && echo '(dry run)')"
if [ -e "$OUT" ] && [ "$DRY" = 0 ]; then say "$OUT exists; move it away first"; exit 1; fi
[ "$DRY" = 1 ] || { mkdir -p "$WORK"; rm -f "$DB"; }
[ -f "$MAKE_DB" ] || say "note: no $MAKE_DB, so nothing can be copied from a make DB"
# empty DB with the builder's schema (its 'labor' step creates the tables and finds no pages: 0 rows)
run "$PY" "$B" --db "$DB" labor > /dev/null || { say "could not create $DB"; exit 1; }

# a. FSM
if [ -n "$FSM" ]; then
    run "$PY" "$B" --db "$DB" fsm "$FSM" --model "$MODEL" --chassis "$CHASSIS" --years "$YEAR" \
        --engines "$ENGINES" --title "$TITLE" || { say "fsm FAILED"; exit 1; }
    lap fsm
else
    skip fsm "no --fsm given"
fi

# b. CHARM
if [ -f "$CHARM_DIR/data.mdb" ]; then
    run "$PY" "$B" --db "$DB" charm --charm "$CHARM_DIR" --makes "$CHARM_MAKES" --years "$YEAR-$YEAR" \
        --only "$YEAR $MODEL" || { say "charm FAILED"; exit 1; }
elif [ -f "$MAKE_DB" ]; then
    say "CHARM text not mounted at $CHARM_DIR; copying the car's CHARM pages from $MAKE_DB (read-only)"
    run "$PY" "$H" copy-docs --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" --kinds charm || { say "copy-docs FAILED"; exit 1; }
else
    skip charm "CHARM not mounted ($CHARM_DIR/data.mdb) and no $MAKE_DB"
fi
lap charm
if [ -z "$FSM" ] && [ -f "$MAKE_DB" ]; then
    run "$PY" "$H" copy-docs --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" --kinds fsm
    lap fsm-copy
fi
if [ "$DRY" = 0 ] && [ "$(sqlite3 "$DB" 'SELECT count(*) FROM section')" = 0 ]; then
    say "no manual pages loaded (no --fsm, no CHARM, nothing for this car in $MAKE_DB): nothing to build"; exit 1
fi

# c. NHTSA, investigations, StackExchange
run "$PY" "$H" extras --db "$DB" || say "extras FAILED (carrying on)"
lap extras

# d. diag DB (DTC names, SSM params, TSBs/NHTSA, forum fixes) when this make has one
if [ -f "$CK/${mk}_diag.db" ]; then
    run "$PY" "$B" --db "$DB" diag "$CK/${mk}_diag.db" || say "diag FAILED (carrying on)"
    lap diag
else
    skip diag "no ${mk}_diag.db"
fi

# e. cards
CARDS=""
if [ -f "$CK/kb/cards_$mk.db" ]; then CARDS=$CK/kb/cards_$mk.db
elif [ -f "$CK/kb/cards.db" ] && sqlite3 "file:$CK/kb/cards.db?mode=ro" \
        "SELECT 1 FROM dtc_card WHERE upper(model)=upper('${MODEL//\'/}') LIMIT 1" 2>/dev/null | grep -q 1; then
    CARDS=$CK/kb/cards.db
fi
if [ -n "$CARDS" ]; then run "$PY" "$B" --db "$DB" cards "$CARDS" || say "cards FAILED (carrying on)"; lap cards
else skip cards "no cards DB for $MAKE $MODEL"; fi

# f/g. community + telemetry from the make DB
if [ -f "$MAKE_DB" ]; then
    run "$PY" "$H" community --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" || say "community FAILED (carrying on)"
    lap community
    run "$PY" "$H" telemetry --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" || say "telemetry FAILED (carrying on)"
    lap telemetry
else
    skip community "no $MAKE_DB"; skip telemetry "no $MAKE_DB"
fi

# h. trim
run "$PY" "$H" trim --db "$DB" --model "$MODEL" --year "$YEAR" || { say "trim FAILED"; exit 1; }
lap trim

# i. embed
if [ "$EMBED" = 1 ]; then
    if [ "$REUSE" = 1 ] && [ -f "$MAKE_DB" ]; then
        run "$PY" "$H" reuse-vectors --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" && lap reuse-vectors
        run "$PY" "$B" --db "$DB" embed --device "$DEVICE" --missing || say "embed FAILED (keyword search still works)"
    else
        run "$PY" "$B" --db "$DB" embed --device "$DEVICE" || say "embed FAILED (keyword search still works)"
    fi
    lap embed
else
    skip embed "--no-embed (keyword search only)"
fi

# i2. pictures inside the DB
if [ "$IMAGES" = 1 ]; then
    FD=$FSM_DIR; [ -n "$FSM" ] && FD=$(dirname "$(dirname "$FSM")")   # the converter writes <dir>/<title>/<title>.md
    run "$PY" "$H" images --db "$DB" --fsm-dir "$FD" --lmdb "$CHARM_IMAGES" || say "images FAILED (carrying on)"
    lap images
else
    skip images "--no-images"
fi

# j. finish
run "$PY" "$B" --db "$DB" finish || { say "finish FAILED"; exit 1; }
lap finish
skip ann "one car is under the retriever's 100k-row ANN threshold"

# l. move into place + app view
if [ "$DRY" = 0 ]; then
    mv "$DB" "$OUT"
    mkdir -p "$CARS/$SLUG"
    ln -sfn "../$SLUG.db" "$CARS/$SLUG/$mk.db"
    [ "$KEEP" = 1 ] || rm -rf "$WORK"
fi
say "done: $OUT ($( [ -f "$OUT" ] && du -h "$OUT" | cut -f1)), total $(( $(date +%s) - T0 )) s"
say "app: set \"kb_dir\": \"$CARS/$SLUG\" in app/config.json (the folder holds $mk.db -> ../$SLUG.db)"
