#!/bin/bash
# Bring one car up to the Forester's level, start to finish, unattended. Every step is skipped when already
# done, so it's safe to re-run or resume after Ctrl-C.
#   bash ~/subaru_cyberdeck/tools/car_full.sh chevrolet Tahoe 2003            # the friend's Tahoe
#   bash ~/subaru_cyberdeck/tools/car_full.sh toyota Camry 2005 --dry-run      # say what would run
#   setsid nohup bash ~/subaru_cyberdeck/tools/car_full.sh chevrolet Tahoe 2003 >/dev/null 2>&1 &   # detached
# Steps: 1 make_full.sh (make DB layers)  2 community threads -> <make>.threads.db (CPU)
#        3 thread vectors (GPU)  4 baked DTC cards for that model (~6 h GPU)  5 queue its chart pictures
# GPU steps wait (checking every 5 min) until no other GPU job runs, then go. Cyberdeck Home's PC models are
# slow while they run. Log: ~/subaru_cyberdeck/logs/car_full_<make>.log
CK=~/subaru_cyberdeck
P=$CK/venv/bin/python
KB=$CK/kb
. "$CK/tools/gpu_guard.sh"
export HF_HUB_OFFLINE=1

DRY=0; A=()
for x in "$@"; do [ "$x" = "--dry-run" ] && DRY=1 || A+=("$x"); done
SLUG=${A[0]}; CAR=${A[1]}; YEAR=${A[2]}
[ -n "$SLUG" ] && [ -n "$CAR" ] && [ -n "$YEAR" ] || { sed -n 2,11p "$0"; exit 2; }
DF=(); [ $DRY = 1 ] && DF=(--dry-run)

exec > >(tee -a "$CK/logs/car_full_$SLUG.log") 2>&1
say() { echo "$(date '+%F %T') [$SLUG $CAR $YEAR] $*"; }
wait_gpu() {   # $1 = step name
    [ $DRY = 1 ] && return 0
    local first=1
    until why=$(gpu_free); do
        [ $first = 1 ] && say "$1: waiting for the GPU ($why)"; first=0; sleep 300
    done
}

say "start"
# 1. the make's DB layers (CHARM, NHTSA, Reddit, forum fixes, embed, finish, score)
wait_gpu "1. make_full"
bash "$CK/tools/make_full.sh" "$SLUG" "${DF[@]}"
DB=$KB/$SLUG.db
[ -f "$DB" ] || { say "1. no $DB after make_full; stopping"; exit 1; }

# which CHARM model names cover this car in that year (e.g. 'C/K Tahoe' and 'Tahoe' can both exist)
# (only names that have manual DTC procedures, so cards and charts have something to work from)
mapfile -t MODELS < <(sqlite3 -readonly "$DB" "SELECT DISTINCT v.model FROM vehicle v
    WHERE upper(v.model) LIKE '%'||upper('$CAR')||'%' AND v.year=$YEAR
    AND EXISTS (SELECT 1 FROM dtc_procedure d WHERE d.vehicle_id=v.id) ORDER BY v.model")
[ ${#MODELS[@]} -gt 0 ] || { say "no model matching '$CAR' for $YEAR in $DB (if the names below are single letters
    like C or K, step b2 'car names' didn't run: see logs/make_full_$SLUG.log). Models there:";
    sqlite3 -readonly "$DB" "SELECT DISTINCT model FROM vehicle WHERE year=$YEAR ORDER BY 1" | column; exit 1; }
say "CHARM models for this car: ${MODELS[*]}"

# 2. community threads (CPU, minutes)
if [ ! -f "$KB/community/work/$SLUG.work.db" ]; then
    say "2. community pass 0 (prefilter)"
    [ $DRY = 1 ] || "$P" "$KB/build_community.py" prefilter --make "$SLUG" || { say "2. prefilter FAILED"; exit 1; }
fi
say "2. community raw threads"
[ $DRY = 1 ] || "$P" "$KB/build_community.py" raw --make "$SLUG" || { say "2. raw FAILED"; exit 1; }

# 3. thread vectors on the GPU (resumable; skips threads already done)
wait_gpu "3. thread vectors"
say "3. thread vectors"
[ $DRY = 1 ] || "$P" "$KB/build_community.py" rawembed --make "$SLUG" --device cuda || { say "3. rawembed FAILED"; exit 1; }

# 4. baked DTC cards, one bake per CHARM model name (cards already built are skipped)
for M in "${MODELS[@]}"; do
    wait_gpu "4. cards for $M"
    say "4. cards for $M"
    bash "$CK/tools/bake_cards.sh" "$SLUG" "$M" "${DF[@]}" || { say "4. cards for $M FAILED (see logs/bake_cards_$SLUG.log)"; exit 1; }
done

# 5. chart pictures for this year go on the picture server's background queue (read when the GPU is free)
for M in "${MODELS[@]}"; do
    say "5. chart pictures for $M $YEAR"
    "$P" "$CK/tools/charm_pic_server.py" queue --db "$DB" --car "$M" --year "$YEAR" "${DF[@]}"
done
systemctl --user is-active --quiet charm-pics || say "5. note: the charm-pics service isn't running, so the queue won't be read until: systemctl --user start charm-pics"

say "done. For the deck: copy kb/$SLUG.db and kb/$SLUG.threads.db (runbook step 9-11)."
