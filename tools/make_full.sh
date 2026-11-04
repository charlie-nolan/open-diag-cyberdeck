#!/bin/bash
# Give one make (or every make) the layers subaru.db has, in order. Each step is skipped when already done,
# so it's safe to re-run, or to resume after Ctrl-C.
#   bash tools/make_full.sh acura          # one make (file name in kb/, e.g. land_rover)
#   bash tools/make_full.sh --all          # every CHARM make except subaru
#   bash tools/make_full.sh acura --dry-run  # say what would run, run nothing
# Log: logs/make_full_<make>.log
# Steps: a CHARM text DB  b NHTSA  b2 car names  c Reddit  d NHTSA investigations + StackExchange
#        e forum fixes (LLM, GPU)  f embed --missing (GPU)  g finish  h test score -> logs/eval_make/history.csv
# GPU steps (e, f) refuse to start while another GPU job runs; they unload Ollama's models first, so
# Cyberdeck Home's PC models are slow until they finish.
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
P=$CK/venv/bin/python
KB=$CK/kb
ALT="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}/kb_build"
PROF=$CK/chevrolet/profiles/makes
. "$CK/tools/gpu_guard.sh"
export HF_HUB_OFFLINE=1

DRY=0; ARGS=()
for x in "$@"; do [ "$x" = "--dry-run" ] && DRY=1 || ARGS+=("$x"); done
[ ${#ARGS[@]} -eq 1 ] || { sed -n 2,12p "$0"; exit 2; }

say() { echo "$(date '+%F %T') [$SLUG] $*"; }
run() { if [ $DRY = 1 ]; then say "would run: $*"; else say "\$ $*"; "$@"; fi; }
count() { sqlite3 -readonly "$1" "$2" 2>/dev/null || echo 0; }

one_make() {
    SLUG=$1
    MAKES=$(grep "^$SLUG|" "$KB/charm_make_groups.txt" | cut -d'|' -f2)
    [ -n "$MAKES" ] || { say "not in kb/charm_make_groups.txt"; return 1; }
    case $SLUG in subaru) say "skip: built separately"; return 0;; esac
    DB="$KB/$SLUG.db"; [ -f "$DB" ] || { [ -f "$ALT/$SLUG.db" ] && DB="$ALT/$SLUG.db"; }

    # a. CHARM text
    if [ -f "$KB/.$SLUG.done" ] || [ "$(count "$DB" 'SELECT count(*) FROM section')" -gt 0 ]; then
        say "a. CHARM DB: done ($(du -h "$DB" | cut -f1))"
    elif pgrep -f build_charm_all.sh >/dev/null; then
        say "a. CHARM DB: not built yet, and the per-make CHARM loop is running; it will build $SLUG. Stopping here."
        return 0
    else
        run "$P" "$KB/build_subaru_kb.py" --db "$DB" charm --makes "$MAKES" --years 1982-2013 &&
        run "$P" "$KB/build_subaru_kb.py" --db "$DB" finish || { say "a. FAILED"; return 1; }
    fi

    # b. NHTSA
    if [ -f "$KB/.$SLUG.nhtsa" ]; then say "b. NHTSA: done ($(cat "$KB/.$SLUG.nhtsa"))"
    else run "$P" -I "$KB/add_nhtsa.py" "$DB" || { say "b. FAILED"; return 1; }; fi

    # b2. car names: CHARM cars renamed by the make's NHTSA model list ('K Tahoe 4WD' was stored as 'K').
    #     CPU, keeps pages and vectors. Needs the CHARM text mount (~/mnt/charm), like step a.
    if [ -n "$(sqlite3 -readonly "$DB" "SELECT 1 FROM meta WHERE k='charm_relink'" 2>/dev/null)" ]; then
        say "b2. car names: done"
    else
        run "$P" "$KB/build_subaru_kb.py" --db "$DB" relink --makes "$MAKES" ||
            say "b2. car names FAILED (is the CHARM text mounted at ~/mnt/charm?); carrying on with the old names"
    fi

    # c. Reddit
    RDB=$CK/raw/reddit/reddit_$SLUG.db
    if [ ! -f "$PROF/$SLUG.json" ]; then say "c. Reddit: no profile (no subreddit for this make in the dump)"
    elif [ "$(count "$RDB" 'SELECT count(*) FROM dump_state WHERE done=0')" = 0 ] && [ -f "$RDB" ]; then
        say "c. Reddit: done ($(count "$RDB" 'SELECT count(*) FROM reddit_post') posts)"
    elif pgrep -f reddit_all_makes.sh >/dev/null; then
        say "c. Reddit: the all-makes Reddit pull is running and will load $SLUG; re-run after it"
    else
        run "$P" "$CK/chevrolet/make_pipeline.py" "$PROF/$SLUG.json" reddit-get &&
        run "$P" "$CK/chevrolet/make_pipeline.py" "$PROF/$SLUG.json" reddit || { say "c. FAILED"; return 1; }
    fi

    # d. NHTSA defect investigations + Mechanics StackExchange Q&A -> knowledge rows (CPU, seconds per make;
    #    the first make downloads FLAT_INV.zip and reads Posts.xml once). Needs step b's NHTSA model names.
    for L in "inv|add_investigations.py|investigations" "se|add_stackexchange.py|StackExchange"; do
        IFS='|' read -r MK PY WHAT <<< "$L"
        if [ -f "$KB/.$SLUG.$MK" ]; then say "d. $WHAT: done ($(cat "$KB/.$SLUG.$MK"))"
        else run "$P" -I "$KB/$PY" "$DB" || say "d. $WHAT FAILED; carrying on without it"; fi
    done

    # e. forum fixes: threads -> 'what fixed it' rows (llama-server Qwen3-8B, GPU)
    WORK=$KB/bake_work_$SLUG.db; DIAG=$CK/${SLUG}_diag.db
    if [ -f "$KB/.$SLUG.fixes" ]; then say "e. forum fixes: done ($(count "$DB" "SELECT count(*) FROM knowledge WHERE kind='forum_fix'"))"
    elif [ ! -f "$RDB" ]; then say "e. forum fixes: skipped (no Reddit DB)"
    elif ! gpu_free; then say "e. forum fixes: waiting for the GPU; re-run later"; return 0
    else
        MODELS=$($P -c "import json; print(','.join(json.load(open('$PROF/$SLUG.json'))['models']))")
        MAKE=$($P -c "import json; print(json.load(open('$PROF/$SLUG.json'))['make'])")
        EX=("$P" "$CK/card_baking/extract_fixes.py" --work "$WORK" --make "$MAKE" --models "$MODELS")
        run "${EX[@]}" filter --reddit "$RDB" || return 1
        if [ $DRY = 0 ]; then
            gpu_lock || return 1
            "$CK/card_baking/serve_extract.sh" > "$CK/logs/serve_extract_$SLUG.log" 2>&1 & SRV=$!
            trap 'kill $SRV 2>/dev/null' EXIT
            for _ in $(seq 360); do
                curl -sf http://127.0.0.1:8080/health >/dev/null && break
                kill -0 $SRV 2>/dev/null || { say "e. llama-server exited: $(tail -2 "$CK/logs/serve_extract_$SLUG.log")"; trap - EXIT; return 1; }
                sleep 5
            done
        fi
        run "${EX[@]}" run --parallel 4 && run "${EX[@]}" export --diag-db "$DIAG" &&
        run "$P" "$KB/build_subaru_kb.py" --db "$DB" diag "$DIAG" && { [ $DRY = 1 ] || touch "$KB/.$SLUG.fixes"; }
        [ $DRY = 1 ] || { kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; trap - EXIT; }
    fi

    # f. vectors for everything new (keyword search works without them)
    if ! gpu_free; then say "f. embed: waiting for the GPU; re-run later"; return 0; fi
    [ $DRY = 1 ] || gpu_lock || return 1
    run "$P" "$KB/build_subaru_kb.py" --db "$DB" embed --missing || { say "f. FAILED"; return 1; }
    # g. finish
    run "$P" "$KB/build_subaru_kb.py" --db "$DB" finish || { say "g. FAILED"; return 1; }
    # h. score, appended to history
    (cd "$KB" && run "$P" eval_make.py "$DB" --history --label "make_full") | tail -5
    say "done"
}

if [ "${ARGS[0]}" = "--all" ]; then
    for s in $(cut -d'|' -f1 "$KB/charm_make_groups.txt"); do
        one_make "$s" 2>&1 | tee -a "$CK/logs/make_full_$s.log"
    done
else
    one_make "${ARGS[0]}" 2>&1 | tee -a "$CK/logs/make_full_${ARGS[0]}.log"
fi
