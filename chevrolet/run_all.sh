#!/bin/bash
# Tahoe knowledge pipeline: downloads and light steps only. Embedding and the card bake wait for the Subaru bake.
# Start detached:  setsid nohup chevrolet/run_all.sh [VIN] > logs/chevrolet_pipeline.log 2>&1 &
# Watch:           tail -f logs/chevrolet_pipeline.log      (ends with ALL DONE)
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
cd "$(dirname "$0")" || exit 1
PROFILE=${PROFILE:-chevrolet_tahoe_gmt800.json}
PY=$CK/venv/bin/python
"$PY" -c "import zstandard, bs4, lxml, lmdb" 2>/dev/null || PY=python3
echo "python: $PY  profile: $PROFILE"
P="$PY make_pipeline.py $PROFILE"
step() { echo; echo "===== $* ($(date '+%F %T'))"; $P "$@" || echo "!!!!! step '$1' FAILED (later steps still run)"; }
step check
step nhtsa
if [ -n "$1" ]; then step vin --vin "$1"; else step vin; fi
step charm-list
step kb
step reddit-get
step reddit --topup
step forums
step status
echo; echo "ALL DONE $(date '+%F %T')"
