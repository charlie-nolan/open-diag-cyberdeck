#!/bin/bash
# After kb/chevrolet.db is built: the Tahoe extras the app uses. DTC names from CHARM pages + PCMHammer's GM
# code list, and the GM Mode 22 PID file (kb/gm_mode22_pids.json) for live data. CPU only, minutes.
#   setsid nohup bash tools/chevy_extras.sh >> logs/chevy_extras.log 2>&1 &
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
P=$CK/venv/bin/python
MP=$CK/chevrolet/make_pipeline.py
until [ -f $CK/kb/.chevrolet.done ]; do sleep 300; done
$P "$MP" chevrolet_tahoe_gmt800.json dtc-names
$P "$MP" chevrolet_tahoe_gmt800.json gm-pids $CK/chevrolet/GMT_PIDS_Rev_5.xlsx
echo "=== chevy extras done $(date -Is)"
