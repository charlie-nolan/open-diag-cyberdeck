#!/bin/bash
# Pre-baked DTC cards (like the 2002 Forester's 176) for one model of one make. ~6 h per model on the GPU.
#   bash ~/subaru_cyberdeck/tools/bake_cards.sh acura                 # lists the models worth baking, bakes nothing
#   bash ~/subaru_cyberdeck/tools/bake_cards.sh acura TL               # bake TL, all its CHARM years
#   bash ~/subaru_cyberdeck/tools/bake_cards.sh acura TL 1999-2003     # bake TL 1999-2003
#   add --dry-run to see the commands only
# Needs make_full.sh step e done for that make (forum fixes). Uses llama-server with Qwen3.6-35B-A3B
# (card_baking/serve_cards.sh, port 8081; the first run downloads the model, ~22 GB). Refuses while another
# GPU job runs; unloads Ollama's models, so Cyberdeck Home is slow until it finishes.
# Log: ~/subaru_cyberdeck/logs/bake_cards_<make>.log   Resumable: cards already built are skipped.
CK=~/subaru_cyberdeck
P=$CK/venv/bin/python
KB=$CK/kb
. "$CK/tools/gpu_guard.sh"
DRY=0; A=()
for x in "$@"; do [ "$x" = "--dry-run" ] && DRY=1 || A+=("$x"); done
SLUG=${A[0]}; MODEL=${A[1]}; YEARS=${A[2]}
[ -n "$SLUG" ] || { sed -n 2,11p "$0"; exit 2; }
DB=$KB/$SLUG.db; [ -f "$DB" ] || DB="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}/kb_build/$SLUG.db"
[ -f "$DB" ] || { echo "no DB for $SLUG yet (CHARM build not done)"; exit 1; }
WORK=$KB/bake_work_$SLUG.db
MAKE=$($P -c "import json; print(json.load(open('$CK/chevrolet/profiles/makes/$SLUG.json'))['make'])" 2>/dev/null || echo "$SLUG")

if [ -z "$MODEL" ]; then
    echo "Models in $SLUG.db, most DTC procedures first (pick one; forum fixes count if step e ran):"
    sqlite3 -readonly "$DB" "SELECT v.model, min(v.year)||'-'||max(v.year), count(DISTINCT d.code) FROM dtc_procedure d
        JOIN vehicle v ON v.id=d.vehicle_id GROUP BY v.model ORDER BY 3 DESC LIMIT 15" | column -t -s'|'
    exit 0
fi
[ -f "$WORK" ] || { echo "no forum-fix work DB ($WORK): run make_full.sh $SLUG first (step e)"; exit 1; }
[ -n "$YEARS" ] || YEARS=$(sqlite3 -readonly "$DB" "SELECT min(year)||'-'||max(year) FROM vehicle WHERE model='$MODEL'")
OUT=$KB/cards_$SLUG.db
CMD=("$P" "$CK/card_baking/bake_cards.py" --make "$MAKE" --model "$MODEL" --years "$YEARS" --kb "$DB" --kb-dir "$KB"
     --work "$WORK" --out "$OUT")
if [ $DRY = 1 ]; then
    echo "would run: serve_cards.sh, then ${CMD[*]}"
    echo "then: build_subaru_kb.py --db $DB cards $OUT; embed --missing --only dtc_card; finish"; exit 0
fi
gpu_free || exit 1
gpu_lock || exit 1
exec > >(tee -a "$CK/logs/bake_cards_$SLUG.log") 2>&1
"$CK/card_baking/serve_cards.sh" > "$CK/logs/serve_cards_$SLUG.log" 2>&1 & SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT
for _ in $(seq 720); do curl -sf http://127.0.0.1:8081/health >/dev/null && break; kill -0 $SRV || { echo "llama-server died, see logs/serve_cards_$SLUG.log"; exit 1; }; sleep 5; done
"${CMD[@]}" || exit 1
kill $SRV; wait $SRV 2>/dev/null; trap - EXIT
"$P" "$KB/build_subaru_kb.py" --db "$DB" cards "$OUT" &&
"$P" "$KB/build_subaru_kb.py" --db "$DB" embed --missing --only dtc_card &&
"$P" "$KB/build_subaru_kb.py" --db "$DB" finish && echo "cards for $MAKE $MODEL $YEARS are in $DB"
