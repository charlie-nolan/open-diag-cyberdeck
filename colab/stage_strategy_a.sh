#!/usr/bin/env bash
set -euo pipefail

CK="${CYBERDECK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
OUT="${STAGE_DIR:-$CK/cache/colab_upload_to_drive}"
IN_DIR="$OUT/inputs"
mkdir -p "$IN_DIR"

MAKES=(
    chevrolet toyota honda ford nissan jeep dodge hyundai kia gmc
    mazda volkswagen acura lexus bmw volvo audi mercedes_benz
    chrysler buick cadillac lincoln scion infiniti mitsubishi mini
)

echo "Staging 26 makes -> $OUT"
cp -f "$CK/colab/colab_worker.py" "$OUT/"
cp -f "$CK/colab/cyberdeck_colab_extract.ipynb" "$OUT/"

for m in "${MAKES[@]}"; do
    if [[ -f "$CK/kb/community/work_9b_run/${m}.work.db" ]]; then
        SRC="$CK/kb/community/work_9b_run/${m}.work.db"
    else
        SRC="$CK/kb/community/work/${m}.work.db"
    fi
    if [[ ! -f "$SRC" ]]; then
        echo "ERROR: missing $SRC" >&2
        exit 1
    fi
    DST="$IN_DIR/${m}.work.db.gz"
    if [[ -f "$DST" && "$DST" -nt "$SRC" ]]; then
        echo "  [skip] $m (up to date)"
        continue
    fi
    echo -n "  [pack] $m ... "
    if command -v pigz >/dev/null 2>&1; then
        pigz -c -6 "$SRC" > "$DST.tmp"
    else
        gzip -c -6 "$SRC" > "$DST.tmp"
    fi
    mv -f "$DST.tmp" "$DST"
    ls -lh "$DST" | awk '{print $5}'
done

echo ""
echo "Staged bundle ready:"
du -sh "$OUT" "$IN_DIR"
