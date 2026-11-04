#!/usr/bin/env bash
# Download the public sources for kb/general.db to the Seagate, build it, then embed + finish
# once make_full / the GPU lock are gone. Log: logs/general_kb.log
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
set -u
ROOT=$CK
cd "$ROOT" || exit 1
SG="${CYBERDECK_ARCHIVE:-$HOME/cyberdeck_archive}"
RAW=""
for d in "$SG/general_kb" "$SG/charm/_general_kb_raw" "${CYBERDECK_SCRATCH:-$HOME/cyberdeck_scratch}/general_kb"; do
  if mkdir -p "$d" 2>/dev/null && touch "$d/.w" 2>/dev/null; then rm -f "$d/.w"; RAW="$d"; break; fi
done
[ -n "$RAW" ] || { echo "no writable raw folder"; exit 1; }
ln -sfn "$RAW" "$ROOT/raw/general" 2>/dev/null || true
echo "== $(date -u +%FT%TZ) raw folder: $RAW"
PY=python3
"$ROOT/venv/bin/python" -c "import pymupdf" 2>/dev/null && PY="$ROOT/venv/bin/python"
$PY -c "import pymupdf" 2>/dev/null || { echo "installing pymupdf into venv"; "$ROOT/venv/bin/pip" install -q pymupdf && PY="$ROOT/venv/bin/python"; }
echo "== fetch ($PY)"
$PY kb/fetch_general.py --raw "$RAW"
echo "== build"
$PY kb/build_general_kb.py --raw "$RAW" --db kb/general.db || exit 1
echo "== $(date -u +%FT%TZ) waiting for the GPU (make_full / .gpu.lock) before embed"
while pgrep -f make_full.sh >/dev/null || ls "$ROOT"/.gpu.lock "$ROOT"/tools/.gpu.lock "$ROOT"/kb/.gpu.lock >/dev/null 2>&1; do
  sleep 600
done
echo "== $(date -u +%FT%TZ) embed"
"$ROOT/venv/bin/python" kb/build_subaru_kb.py --db kb/general.db embed --only section || \
  "$ROOT/venv/bin/python" kb/build_subaru_kb.py --db kb/general.db embed
python3 kb/build_subaru_kb.py --db kb/general.db finish
echo "== $(date -u +%FT%TZ) ALL DONE"
