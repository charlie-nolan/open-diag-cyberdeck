#!/usr/bin/env bash
# Build a knowledge DB for ONE car: kb/cars/<make>_<model>_<year>.db, same layout as a make DB (kb/<make>.db),
# so the app and kb/kb_retriever.py read it unchanged. Runs on any Linux laptop; no GPU needed (--device cpu).
#
#   tools/build_car_db.sh --make <Make> --model <Model> --year <YYYY> \
#       [--manuals <folder|zip|pdf|md>]... [--fsm <manual .md> [--title "<manual>"]] [--chassis <code>] [--engines <code[,code]>] \
#       [--no-charm] [--community-work DIR] [--out-name NAME] [--bench CSV] \
#       [--device cpu|cuda] [--reuse-vectors] [--no-embed] [--charm-images DIR] [--no-images] [--keep-work] [--dry-run] \
#       [--vision MODEL [--vision-url URL] [--vision-hours H]] [--threads [--threads-src FILE]] [--scope [--ecu-ids IDS]] \
#       [--bake-cards MODEL [--cards-work FILE] [--cards-url URL]]
#
# --manuals takes what a person downloaded: a folder (searched recursively) or a .zip holding factory-manual PDFs
#   (converted with kb/pdf_to_md.py, text layer, no OCR), converted .md files (a <name>.fixed.md wins over <name>.md),
#   and/or an HTML manual (a LEMON Manuals single-car download: folder with pages/*.html, or its zip). Repeatable.
#   Chassis/engine codes are optional; without them the pages are attached to the make/model/year only.
# --community-years A-B: community threads of these model years (plus threads with no year); default --year +-2.
# --community-work DIR: community rows from a fresh extraction (kb/car_db.py community-select + build_community.py
#   extract) instead of copying them from kb/<make>.db.
# --bench CSV: every step runs under tools/bench_run.py (wall, CPU, max RSS, MB in/out; GPU samples on GPU steps).
# --vision MODEL: optional, off by default, needs a GPU: an Ollama vision model (e.g. qwen3.6:35b-a3b-q4_K_M; URL from
#   --vision-url or $OLLAMA_URL, default local) reads the DB's diagnostic charts and spec tables (kb/car_db.py vision).
# --threads: optional, this car's raw owner threads + vectors from an existing kb/<make>.threads.db (or --threads-src)
#   into kb/cars/<car>.threads.db, for /wildcard. Nothing is scraped. --scope: optional, car_dtc (this car's codes)
#   and telemetry definitions trimmed to this car; trimmed rows kept in kb/cars/<car>.make_tables.db.
# --bake-cards MODEL: optional, GPU, hours: per-code diagnostic cards (card_baking/bake_cards.py) from the manual plus an
#   existing forum-fix work DB (kb/bake_work_<make>.db or --cards-work; none: manual and TSBs only), on an Ollama (or
#   llama-server) model at --cards-url (default the vision URL). Cards already in the DB are kept; resumable.
#
# Steps (each skipped with one line when its source is missing):
#   a0 manuals   --manuals: PDFs -> kb/pdf_to_md.py; .md -> fsm; HTML manual -> kb/car_db.py html-manual
#   a fsm        build_subaru_kb.py fsm  (only with --fsm)
#   b charm      build_subaru_kb.py charm --only "<year> <model>" when CHARM text is mounted (~/mnt/charm/data.mdb);
#                else the car's CHARM pages are copied from kb/<make>.db (kb/car_db.py copy-docs, read-only)
#   b2 fsm-copy  no --fsm and no --manuals: the car's FSM pages are copied from kb/<make>.db if it has any
#                (--no-charm skips b: e.g. when a manual folder already holds the CHARM-style pages)
#   c nhtsa+     kb/car_db.py extras: add_nhtsa (kb/nhtsa_all.db), add_investigations, add_stackexchange
#   d diag       build_subaru_kb.py diag <make>_diag.db (DTC names, SSM params, TSB/NHTSA, forum fixes) if present
#   e cards      build_subaru_kb.py cards kb/cards_<make>.db (or kb/cards.db when it holds this model)
#   f community  kb/car_db.py community   (comm_* rows for this car from kb/<make>.db), or with --community-work
#                kb/car_db.py community-load (pass 2 + vectors on that work dir, linked to this DB's pages)
#   g telemetry  kb/car_db.py telemetry   (telem_* rows for this car from kb/<make>.db)
#   h trim       kb/car_db.py trim        (other cars' rows out; model-less make-wide rows stay)
#   i embed      build_subaru_kb.py embed --device cpu|cuda   (--reuse-vectors first copies identical rows' vectors
#                from kb/<make>.db, then embeds only the rest; --no-embed: keyword search only)
#   i2 images    kb/car_db.py images: manual figures + CHARM pictures + chart readings stored inside the DB
#                (CHARM pictures need the images LMDB: --charm-images <dir with data.mdb>; --no-images skips)
#   i3 vision    --vision only: kb/car_db.py vision (DTC charts, then spec tables, then other charts; resumable;
#                per-picture seconds/tokens in <out>.vision.csv), then embed --missing for the new sections
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
CHARM=1 COMM_WORK="" COMM_YEARS="" OUT_NAME="" BENCH="" MANUALS=()
VISION="" VISION_URL=${OLLAMA_URL:-http://127.0.0.1:11434} VISION_HOURS=0 THREADS=0 THREADS_SRC="" SCOPE=0 ECU_IDS=""
BAKE="" CARDS_WORK="" CARDS_URL=""
CHARM_DIR=${CHARM_DIR:-$HOME/mnt/charm}
CHARM_IMAGES=${CHARM_IMAGES:-}
FSM_DIR=${FSM_DIR:-$CK/test_batch_out}
usage() { sed -n '2,27p' "$0"; exit "${1:-1}"; }
while [ $# -gt 0 ]; do
    case "$1" in
        --make) MAKE=$2; shift 2;; --model) MODEL=$2; shift 2;; --year) YEAR=$2; shift 2;;
        --fsm) FSM=$2; shift 2;; --chassis) CHASSIS=$2; shift 2;; --engines) ENGINES=$2; shift 2;;
        --title) TITLE=$2; shift 2;; --device) DEVICE=$2; shift 2;; --charm) CHARM_DIR=$2; shift 2;;
        --charm-images) CHARM_IMAGES=$2; shift 2;; --no-images) IMAGES=0; shift;;
        --reuse-vectors) REUSE=1; shift;; --no-embed) EMBED=0; shift;; --dry-run) DRY=1; shift;;
        --keep-work) KEEP=1; shift;; -h|--help) usage 0;;
        --manuals) MANUALS+=("$2"); shift 2;; --no-charm) CHARM=0; shift;; --community-work) COMM_WORK=$2; shift 2;;
        --threads) THREADS=1; shift;; --threads-src) THREADS=1; THREADS_SRC=$2; shift 2;;
        --scope) SCOPE=1; shift;; --ecu-ids) ECU_IDS=$2; shift 2;;
        --bake-cards) BAKE=$2; shift 2;; --cards-work) CARDS_WORK=$2; shift 2;; --cards-url) CARDS_URL=$2; shift 2;;
        --vision) VISION=$2; shift 2;; --vision-url) VISION_URL=$2; shift 2;; --vision-hours) VISION_HOURS=$2; shift 2;;
        --out-name) OUT_NAME=$2; shift 2;; --bench) BENCH=$2; shift 2;; --community-years) COMM_YEARS=$2; shift 2;; *) echo "unknown option $1"; usage;;
    esac
done
[ -n "$MAKE" ] && [ -n "$MODEL" ] && [ -n "$YEAR" ] || usage
if [ -n "$FSM" ]; then
    [ -f "$FSM" ] || { echo "no such FSM file: $FSM"; exit 1; }
    if [ -z "$TITLE" ]; then          # load_manuals.sh uses the manual's folder name as the title
        d=$(basename "$(dirname "$FSM")"); f=$(basename "$FSM"); f=${f%.md}; f=${f%.fixed}
        if [ "$d" = "$f" ]; then TITLE=$d; else TITLE=$f; fi
    fi
fi
mk=$(echo "$MAKE" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_|_$//g')
md=$(echo "$MODEL" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9]+/_/g; s/^_|_$//g')
SLUG=${OUT_NAME:-${mk}_${md}_${YEAR}}
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
STEP="" STEP_IN=() STEP_GPU=0
[ -n "$BENCH" ] && BENCH=$(realpath -m "$BENCH")
run() {   # run CMD...: print it; in --dry-run only print; with --bench, time it as step $STEP
    say "+ $*"
    [ "$DRY" = 1 ] && return 0
    if [ -n "$BENCH" ] && [ -n "$STEP" ]; then
        local extra=() i
        for i in "${STEP_IN[@]}"; do extra+=(--in "$i"); done
        [ "$STEP_GPU" = 1 ] && extra+=(--gpu --gpu-csv "${BENCH%.csv}_gpu_$STEP.csv")
        "$PY" "$CK/tools/bench_run.py" --csv "$BENCH" --step "$STEP" --out "$DB" "${extra[@]}" -- "$@"
    else
        "$@"
    fi
}
step() { STEP=$1; shift; STEP_IN=("$@"); STEP_GPU=0; }
skip() { say "skip $1: $2"; }

say "== one-car DB: $MAKE $MODEL $YEAR -> $OUT${DRY:+ }$([ "$DRY" = 1 ] && echo '(dry run)')"
if [ -e "$OUT" ] && [ "$DRY" = 0 ]; then say "$OUT exists; move it away first"; exit 1; fi
[ "$DRY" = 1 ] || { mkdir -p "$WORK"; rm -f "$DB"; }
[ -f "$MAKE_DB" ] || say "note: no $MAKE_DB, so nothing can be copied from a make DB"
# empty DB with the builder's schema (its 'labor' step creates the tables and finds no pages: 0 rows)
run "$PY" "$B" --db "$DB" labor > /dev/null || { say "could not create $DB"; exit 1; }

# a0. manuals given as a folder / zip / files: PDFs converted, markdown loaded, HTML manuals loaded
FIG_DIRS=()                       # folders holding <title>/<figure> for the images step
fsm_load() {   # fsm_load MD TITLE
    step "fsm:$2" "$1"
    run "$PY" "$B" --db "$DB" fsm "$1" --model "$MODEL" --chassis "$CHASSIS" --years "$YEAR" \
        --engines "$ENGINES" --title "$2" || { say "fsm FAILED: $1"; exit 1; }
    FIG_DIRS+=("$(dirname "$(dirname "$1")")"); lap "fsm $2"
}
for M in "${MANUALS[@]}"; do
    [ -e "$M" ] || { say "no such manual path: $M"; exit 1; }
    M=$(realpath "$M"); SRC=$M
    MX=${M##*.}; MX=${MX,,}
    if [ -f "$M" ] && [ "$MX" = zip ]; then
        SRC=$WORK/manual_src/$(basename "${M%.*}")
        if [ "$DRY" = 0 ] && [ ! -d "$SRC" ]; then
            mkdir -p "$SRC"; "$PY" -c 'import sys,zipfile; z=zipfile.ZipFile(sys.argv[1]);
bad=[n for n in z.namelist() if n.startswith("/") or ".." in n.split("/")]
sys.exit("unsafe zip paths: %s" % bad[:3]) if bad else z.extractall(sys.argv[2])' "$M" "$SRC" || exit 1
        fi
    fi
    # an HTML manual (LEMON / CHARM-style: pages/*.html)
    HTML_ROOTS=$( [ -d "$SRC" ] && find "$SRC" -type d -name pages -exec sh -c 'ls "$1"/*.html >/dev/null 2>&1 && dirname "$1"' _ {} \; )
    while IFS= read -r HR; do
        [ -n "$HR" ] || continue
        step "html-manual" "$HR"
        run "$PY" "$H" html-manual --db "$DB" --path "$HR" --model "$MODEL" --year "$YEAR" ${CHASSIS:+--chassis "$CHASSIS"} \
            $([ "$IMAGES" = 0 ] && echo --no-images) || { say "html-manual FAILED: $HR"; exit 1; }
        lap "html-manual"
    done <<< "$HTML_ROOTS"
    # PDFs: convert (text layer) into the work folder, then load
    while IFS= read -r P; do
        [ -n "$P" ] || continue
        N=$(basename "${P%.*}"); OUTD=$WORK/manuals/$N
        if [ -f "$OUTD/$N.fixed.md" ]; then say "already converted: $OUTD/$N.fixed.md"
        else
            step "pdf_to_md:$N" "$P"
            run "$PY" "$CK/kb/pdf_to_md.py" --pdf "$P" --out "$OUTD" $([ "$IMAGES" = 0 ] && echo --no-images) \
                || { say "pdf_to_md FAILED: $P"; exit 1; }
            lap "pdf_to_md $N"
        fi
        [ "$DRY" = 1 ] || fsm_load "$OUTD/$N.fixed.md" "$N"
    done < <(if [ -d "$SRC" ]; then find "$SRC" -type f -iname '*.pdf' | sort; elif [ "$MX" = pdf ]; then echo "$SRC"; fi)
    # converted markdown (skip <name>.md when <name>.fixed.md sits next to it)
    while IFS= read -r F; do
        [ -n "$F" ] || continue
        case "$F" in *.fixed.md) ;; *) [ -f "${F%.md}.fixed.md" ] && continue;; esac
        d=$(basename "$(dirname "$F")"); f=$(basename "$F"); f=${f%.md}; f=${f%.fixed}
        fsm_load "$F" "$([ "$d" = "$f" ] && echo "$d" || echo "$f")"
    done < <(if [ -d "$SRC" ]; then find "$SRC" -type f -name '*.md' ! -path "$WORK/*" | sort; elif [ "$MX" = md ]; then echo "$SRC"; fi)
done

# a. FSM
if [ -n "$FSM" ]; then
    fsm_load "$FSM" "$TITLE"
elif [ ${#MANUALS[@]} = 0 ]; then
    skip fsm "no --fsm or --manuals given"
fi

# b. CHARM
step charm "$CHARM_DIR"
if [ "$CHARM" = 0 ]; then
    skip charm "--no-charm"
elif [ -f "$CHARM_DIR/data.mdb" ]; then
    run "$PY" "$B" --db "$DB" charm --charm "$CHARM_DIR" --makes "$CHARM_MAKES" --years "$YEAR-$YEAR" \
        --only "$YEAR $MODEL" || { say "charm FAILED"; exit 1; }
elif [ -f "$MAKE_DB" ]; then
    say "CHARM text not mounted at $CHARM_DIR; copying the car's CHARM pages from $MAKE_DB (read-only)"
    run "$PY" "$H" copy-docs --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" --kinds charm || { say "copy-docs FAILED"; exit 1; }
else
    skip charm "CHARM not mounted ($CHARM_DIR/data.mdb) and no $MAKE_DB"
fi
lap charm
if [ -z "$FSM" ] && [ ${#MANUALS[@]} = 0 ] && [ -f "$MAKE_DB" ]; then
    step fsm-copy "$MAKE_DB"
    run "$PY" "$H" copy-docs --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" --kinds fsm
    lap fsm-copy
fi
if [ "$DRY" = 0 ] && [ "$(sqlite3 "$DB" 'SELECT count(*) FROM section')" = 0 ]; then
    say "no manual pages loaded (no --fsm, no CHARM, nothing for this car in $MAKE_DB): nothing to build"; exit 1
fi

# c. NHTSA, investigations, StackExchange
step extras "$CK/kb/nhtsa_all.db"
run "$PY" "$H" extras --db "$DB" || say "extras FAILED (carrying on)"
lap extras

# d. diag DB (DTC names, SSM params, TSBs/NHTSA, forum fixes) when this make has one
if [ -f "$CK/${mk}_diag.db" ]; then
    step diag "$CK/${mk}_diag.db"
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
if [ -n "$CARDS" ]; then step cards "$CARDS"; run "$PY" "$B" --db "$DB" cards "$CARDS" || say "cards FAILED (carrying on)"; lap cards
else skip cards "no cards DB for $MAKE $MODEL"; fi

# f/g. community (fresh extraction or the make DB) + telemetry from the make DB
if [ -n "$COMM_WORK" ]; then
    step community-load "$COMM_WORK"; [ "$DEVICE" = cuda ] && STEP_GPU=1
    run "$PY" "$H" community-load --db "$DB" --work-dir "$COMM_WORK" --make "$mk" --model "$MODEL" --year "$YEAR" \
        --device "$DEVICE" ${COMM_YEARS:+--years "$COMM_YEARS"} || say "community-load FAILED (carrying on)"
    lap community-load
elif [ -f "$MAKE_DB" ]; then
    step community "$MAKE_DB"
    run "$PY" "$H" community --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" ${COMM_YEARS:+--years "$COMM_YEARS"} \
        || say "community FAILED (carrying on)"
    lap community
else
    skip community "no $MAKE_DB and no --community-work"
fi
if [ -f "$MAKE_DB" ]; then
    step telemetry "$MAKE_DB"
    run "$PY" "$H" telemetry --db "$DB" --src "$MAKE_DB" --model "$MODEL" --year "$YEAR" || say "telemetry FAILED (carrying on)"
    lap telemetry
else
    skip telemetry "no $MAKE_DB"
fi

# h. trim
step trim
run "$PY" "$H" trim --db "$DB" --model "$MODEL" --year "$YEAR" || { say "trim FAILED"; exit 1; }
lap trim

# h2. optional: the car's own codes and telemetry definitions (--scope)
if [ "$SCOPE" = 1 ]; then
    step scope
    run "$PY" "$H" scope --db "$DB" --model "$MODEL" --year "$YEAR" ${ECU_IDS:+--ecu-ids "$ECU_IDS"} || say "scope FAILED (carrying on)"
    lap scope
else
    skip scope "no --scope (make-wide code and telemetry tables kept)"
fi

# h3. optional: this car's raw owner threads from an existing threads file (--threads)
if [ "$THREADS" = 1 ]; then
    TSRC=${THREADS_SRC:-$CK/kb/$mk.threads.db}
    step threads "$TSRC"
    if [ -f "$TSRC" ]; then
        run "$PY" "$H" threads --db "$DB" --model "$MODEL" --year "$YEAR" --src "$TSRC" --out "$WORK/$mk.threads.db" \
            ${COMM_YEARS:+--years "$COMM_YEARS"} || say "threads FAILED (carrying on)"
    else
        say "threads: no $TSRC (optional; the app works without it)"
    fi
    lap threads
else
    skip threads "no --threads"
fi

# i. embed
step embed; [ "$DEVICE" = cuda ] && STEP_GPU=1
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
    FD=(); for d in "${FIG_DIRS[@]}"; do FD+=(--fsm-dir "$d"); done   # the converter writes <dir>/<title>/<title>.md
    [ ${#FD[@]} = 0 ] && FD=(--fsm-dir "$FSM_DIR")
    step images "${FIG_DIRS[@]}"
    run "$PY" "$H" images --db "$DB" "${FD[@]}" --lmdb "$CHARM_IMAGES" || say "images FAILED (carrying on)"
    lap images
else
    skip images "--no-images"
fi

# i3. vision (optional): read the DB's charts and spec tables with a vision model
if [ -n "$VISION" ] && [ "$IMAGES" = 1 ]; then
    step vision; STEP_GPU=1
    run "$PY" "$H" vision --db "$DB" --model "$VISION" --url "$VISION_URL" --max-hours "$VISION_HOURS" \
        --log "${OUT%.db}.vision.csv" || say "vision FAILED (carrying on)"
    if [ "$EMBED" = 1 ]; then
        run "$PY" "$B" --db "$DB" embed --device "$DEVICE" --missing || say "embed of picture text FAILED (keyword search still works)"
    fi
    lap vision
else
    skip vision "no --vision (picture charts stay pictures)"
fi

# i4. cards (optional): bake per-code cards for this car on a local model
if [ -n "$BAKE" ]; then
    step bake-cards; STEP_GPU=1
    CW=${CARDS_WORK:-$CK/kb/bake_work_$mk.db}
    [ -f "$CW" ] || say "bake-cards: no forum-fix work DB at $CW: cards from the manual and TSBs only"
    CO=$WORK/cards_baked.db
    [ "$DRY" = 0 ] && [ -n "$CARDS" ] && [ ! -f "$CO" ] && cp "$CARDS" "$CO"     # cards already made are kept
    run "$PY" "$CK/card_baking/bake_cards.py" --kb "$DB" --kb-dir "$CK/kb" --work "$CW" --out "$CO" --make "$MAKE" \
        --model "$MODEL" --years "$YEAR-$YEAR" --url "${CARDS_URL:-$VISION_URL}" --server-model "$BAKE" --model-name "$BAKE" \
        && run "$PY" "$B" --db "$DB" cards "$CO" \
        && { [ "$EMBED" = 0 ] || run "$PY" "$B" --db "$DB" embed --device "$DEVICE" --missing --only dtc_card; } \
        || say "bake-cards FAILED (carrying on; the DB keeps the cards it had)"
    lap bake-cards
else
    skip bake-cards "no --bake-cards (optional)"
fi

# j. finish
step finish
run "$PY" "$B" --db "$DB" finish || { say "finish FAILED"; exit 1; }
lap finish
skip ann "one car is under the retriever's 100k-row ANN threshold"

STEP=""
# l. move into place + app view
if [ "$DRY" = 0 ]; then
    mv "$DB" "$OUT"
    mkdir -p "$CARS/$SLUG"
    ln -sfn "../$SLUG.db" "$CARS/$SLUG/$mk.db"
    [ -f "$WORK/$mk.make_tables.db" ] && mv "$WORK/$mk.make_tables.db" "$CARS/$SLUG.make_tables.db"
    if [ -f "$WORK/$mk.threads.db" ]; then
        mv "$WORK/$mk.threads.db" "$CARS/$SLUG.threads.db"
        ln -sfn "../$SLUG.threads.db" "$CARS/$SLUG/$mk.threads.db"     # app/community_ref.raw_threads looks here
    fi
    [ "$KEEP" = 1 ] || rm -rf "$WORK"
fi
say "done: $OUT ($( [ -f "$OUT" ] && du -h "$OUT" | cut -f1)), total $(( $(date +%s) - T0 )) s"
say "app: set \"kb_dir\": \"$CARS/$SLUG\" in app/config.json (the folder holds $mk.db -> ../$SLUG.db)"
