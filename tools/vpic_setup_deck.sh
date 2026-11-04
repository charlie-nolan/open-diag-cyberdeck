#!/bin/bash
# Offline NHTSA VIN decoder on the deck (Orange Pi, Ubuntu 24.04 arm64): NHTSA's standalone vPIC database in a
# local PostgreSQL, used by app/vpic_local.py (vehicle_id.nhtsa_decode asks it before going online).
# Ubuntu's PostgreSQL is 16 and NHTSA builds its dumps on 17+, so this loads the plain-SQL dump with psql
# (a custom-format dump from a newer pg_dump won't pg_restore on 16) and drops the few 17-only SET lines.
# Safe to re-run: each run replaces the vpic database.
#   bash tools/vpic_setup_deck.sh            # 2026_10
#   bash tools/vpic_setup_deck.sh 2026_11    # a newer month
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
set -e
MONTH=${1:-2026_10}
ZIP=vPICList_lite_${MONTH}.plain.zip
DIR=$CK/raw/vpic

echo "== 1/5 PostgreSQL from apt (asks for your password once)"
sudo apt-get install -y postgresql postgresql-client unzip curl
sudo systemctl enable --now postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$USER'" | grep -q 1 ||
    sudo -u postgres createuser --createdb "$USER"      # peer login: OS user -> same-named role, no password

echo "== 2/5 get $ZIP"
mkdir -p "$DIR" && cd "$DIR"
if [ ! -f "$ZIP" ]; then
    if [ -f $CK/tools/"$ZIP" ]; then cp $CK/tools/"$ZIP" .
    else curl -fL --retry 4 -o "$ZIP" "https://vpic.nhtsa.dot.gov/downloads/$ZIP"; fi
fi
rm -rf unpacked && mkdir unpacked && unzip -q "$ZIP" -d unpacked
SQL=$(find unpacked -type f \( -name '*.sql' -o -name '*.plain' -o -name '*.txt' \) | head -1)
[ -n "$SQL" ] || SQL=$(find unpacked -type f | head -1)
echo "   dump: $SQL"

echo "== 3/5 load into database 'vpic' (a few minutes on the Pi)"
dropdb --if-exists vpic
createdb vpic
# 17+-only settings and ownership lines would only print errors on 16: leave them out
grep -vE '^SET (transaction_timeout|default_table_access_method)|^ALTER .* OWNER TO |^(GRANT|REVOKE) ' "$SQL" |
    psql -X -q -d vpic -v ON_ERROR_STOP=0 2>&1 | grep -viE '^(SET|CREATE|ALTER|COPY|COMMENT)' | tail -5 || true

echo "== 4/5 test decode (a 2000 Forester pattern)"
OUT=$(psql -X -d vpic -Atc "SELECT row_to_json(t) FROM vpic.spVinDecode('JF1SF6557YH712345') t" 2>&1 || true)
if ! echo "$OUT" | grep -qi '"make"'; then
    echo "!! spVinDecode failed on PostgreSQL $(psql -XAtc 'SHOW server_version' -d vpic):"; echo "$OUT" | head -5
    exit 1
fi
echo "$OUT" | grep -iE '"(make|model|model year|displacement \(l\)|transmission style)"' | head -8

echo "== 5/5 app check"
python3 -c "
import sys; sys.path.insert(0, '$CK/app')
import vpic_local; print(vpic_local.vpic_decode('JF1SF6557YH712345'))"
echo "Done. The deck now decodes VINs offline."
