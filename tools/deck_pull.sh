#!/bin/bash
# RUN ON THE DECK. Pulls the app and every finished, useful DB from the PC over Tailscale.
#   bash tools/deck_pull.sh                  # app + Subaru + Tahoe + general + VIN/ECU tables
#   bash tools/deck_pull.sh toyota honda     # also these makes' DBs (file names in kb/)
#   bash tools/deck_pull.sh --all-makes      # also every other finished make (~70 GB)
#   A make the PC is still building is skipped (a copy now could be half-written); everything else is copied.
#   Run it again any time: only new or changed files are sent. --force copies even makes being built.
# rsync writes each file to a temp name and renames it when complete, so a cut connection never leaves a broken DB;
# re-running only sends what changed. Files that don't exist yet on the PC are skipped quietly.
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
PC=${CYBERDECK_PC:?set CYBERDECK_PC=user@your-pc (e.g. its Tailscale name)}
R=$(basename "$CK")           # same folder name in both home directories
L=$CK
FORCE=0; ALL=0; EXTRA=()
for x in "$@"; do case $x in --force) FORCE=1;; --all-makes) ALL=1;; *) EXTRA+=("$x");; esac; done

ssh -o ConnectTimeout=10 "$PC" true || { echo "can't reach the PC at $PC (Tailscale up on both?)"; exit 1; }
busy=$(ssh "$PC" "pgrep -af 'build_subaru_kb.py|build_community.py|bake_cards|make_full.sh|car_full.sh|load_manuals.sh|eval_make.py|add_nhtsa.py|add_investigations.py|add_stackexchange.py' | grep -v pgrep")
[ $FORCE = 1 ] && busy=""
being_built() {
    local m=$1
    [ -z "$busy" ] && return 1
    echo "$busy" | grep -qE -- "--make $m( |$)|/$m(\.threads)?\.db|kb/$m\.db|(car_full|make_full|bake_cards)\.sh $m( |$)" && return 0
    [ "$m" = subaru ] && echo "$busy" | grep -E "load_manuals.sh|build_subaru_kb.py" | grep -qv -- "--db" && return 0
    return 1
}
SKIPPED=()
if [ -n "$busy" ]; then echo "PC jobs running (their DBs are skipped this time):"; echo "$busy" | cut -c1-150; fi

if [ $ALL = 1 ]; then
    mapfile -t -O ${#EXTRA[@]} EXTRA < <(ssh "$PC" "cd $R/kb && ls .*.done" | sed 's/^\.//; s/\.done$//' |
        grep -vxE 'subaru|chevrolet')
    need=$(ssh "$PC" "cd $R/kb && du -cb ${EXTRA[*]/%/.db} 2>/dev/null | tail -1 | cut -f1")
    echo "--all-makes: ${#EXTRA[@]} makes, $(( ${need:-0} / 1000000000 )) GB (deck has $(df -h --output=avail "$L" | tail -1 | tr -d ' ') free)"
fi
mkdir -p "$L/kb/community" "$L/tools"
pull() {
    rsync -a --partial --ignore-missing-args --info=progress2,name1 -e ssh "$PC:$R/$1" "$2"
}

echo "== app, parts lookup, teamdeck (deck-side config.json kept)"
rsync -a --exclude __pycache__ --exclude config.json -e ssh \
    "$PC:$R/app" "$PC:$R/parts_sourcing" "$PC:$R/teamdeck" "$L/" || exit 1
pull tools/deck_pull.sh "$L/tools/"
pull tools/vpic_setup_deck.sh "$L/tools/"

echo "== search code"
for f in kb_retriever.py embedder.py; do pull "kb/$f" "$L/kb/"; done

echo "== embedding model (Qwen3-Embedding-0.6B, ~1.2 GB once; turns on meaning search on the deck, else keywords only)"
SNAP=$(ssh "$PC" 'ls -d ~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/*/ 2>/dev/null | tail -1')
if [ -n "$SNAP" ]; then
    mkdir -p "$L/models/qwen3emb"
    rsync -aL --partial --info=progress2 -e ssh "$PC:$SNAP" "$L/models/qwen3emb/"
    "$L/venv/bin/python" -c "import sentence_transformers, sqlite_vec, numpy" 2>/dev/null ||
        echo "   the model is copied, but the deck's Python lacks its libraries. Once: $L/venv/bin/pip install sentence-transformers sqlite-vec numpy"
else
    echo "   not found on the PC (~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B); skipped"
fi

make_files() {
    local m=$1
    if being_built "$m"; then SKIPPED+=("$m"); echo "   skip $m: the PC is still building it"; return 1; fi
    pull "kb/$m.db" "$L/kb/"; pull "kb/$m.threads.db" "$L/kb/"; pull "${m}_diag.db" "$L/"
}

echo "== Subaru: manuals + CHARM + NHTSA complaints/TSBs/recalls + forum fixes + baked cards (all in subaru.db)"
make_files subaru && pull kb/images "$L/kb/"   # manual figures, if shipped

echo "== Tahoe (Chevrolet): CHARM + NHTSA + forum fixes (+ cards once baked) + GM live-data PIDs"
make_files chevrolet
pull kb/gm_mode22_pids.json "$L/kb/"

echo "== general car knowledge, VIN and ECU-id tables"
for f in general.db vin.db romid.db; do pull "kb/$f" "$L/kb/"; done

echo "== extracted community reports (only the makes that have them)"
rsync -a --ignore-missing-args -e ssh --include '*.community.db' --exclude '*' "$PC:$R/kb/community/" "$L/kb/community/"

for m in "${EXTRA[@]}"; do
    echo "== $m"
    make_files "$m"
done

[ ${#SKIPPED[@]} -gt 0 ] && echo "== not copied yet (still building): ${SKIPPED[*]}. Run this again when they finish."
echo "== done. Deck free space: $(df -h --output=avail "$L" | tail -1 | tr -d ' ')"
ls -lh "$L"/kb/*.db 2>/dev/null | awk '{print "   "$5"  "$9}'
