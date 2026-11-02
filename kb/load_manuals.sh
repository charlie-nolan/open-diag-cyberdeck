#!/usr/bin/env bash
# Load the converted factory service manuals into subaru.db (one `fsm` run each; re-running replaces that manual).
# Manuals whose markdown isn't there yet are skipped, so this can be run again after more finish converting.
# Then: cards (if cards.db exists), embed (desktop GPU, after marker is done), finish.
set -u
cd "$(dirname "$0")"
OUT=../test_batch_out
load() {   # load <folder name> <models> <chassis> <years> <engines>
  local md="$OUT/$1/$1.md"
  [ -f "$OUT/$1/$1.fixed.md" ] && md="$OUT/$1/$1.fixed.md"
  if [ ! -f "$md" ]; then echo "skip (not converted yet): $1"; return; fi
  echo "== $1"
  python3 build_subaru_kb.py fsm "$md" --model "$2" --chassis "$3" --years "$4" --engines "$5" --title "$1"
}
# Forester SF / SG
load "USDM Forester FSM 1999 MSA5TCD99F (SF)" Forester SF 1999 EJ251
load "USDM Forester FSM 2000 G8030BE_V1 (SF)" Forester SF 2000 EJ251
load "USDM Forester FSM 2001 G8040BE_V1 (SF)" Forester SF 2001 EJ251
load "USDM Forester FSM 2002 G8050BE_V1 (SF)" Forester SF 2002 EJ251
load "USDM Forester FSM 2004 G8080BE_V14 (SG)" Forester SG 2004 EJ253,EJ255
# Impreza GD (2.5 RS / WRX)
load "Subaru Impreza 2002" Impreza GD 2002 EJ251,EJ205
# Legacy / Outback (2004 = BE/BH, 2005+ = BP)
load "2004 Service Manual USDM" Legacy,Outback BH 2004 EJ251,EZ30
load "2005 Service Manual USDM" Legacy,Outback BP 2005 EJ253,EJ255,EZ30
load "2008 Service Manual" Legacy,Outback BP 2008 EJ253,EJ255,EZ30
load "2009 Service Manual USDM" Legacy,Outback BP 2009 EJ253,EJ255,EZ30
