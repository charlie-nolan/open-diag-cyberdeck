#!/bin/bash
# Offline NHTSA VIN decoder on this PC: NHTSA's own standalone vPIC database (PostgreSQL build, ~70 MB, VIN
# decoding only) restored into a local PostgreSQL. After this, the app's VIN decode (vehicle_id.nhtsa_decode)
# asks this database first: year, make, model, trim, engine, transmission, drive for any 1981+ US-market VIN,
# no internet needed. Safe to re-run (it skips what's done).
#   bash ~/subaru_cyberdeck/tools/vpic_setup.sh            # newest month NHTSA lists
#   bash ~/subaru_cyberdeck/tools/vpic_setup.sh 2026_10    # a given month
set -e
MONTH=${1:-2026_10}
DIR=~/subaru_cyberdeck/raw/vpic
ZIP=vPICList_lite_${MONTH}.custom.zip

echo "== 1/5 PostgreSQL (asks for your password once)"
sudo pacman -S --needed --noconfirm postgresql
if ! sudo test -f /var/lib/postgres/data/PG_VERSION; then
    sudo -u postgres initdb -D /var/lib/postgres/data
fi
sudo systemctl enable --now postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$USER'" | grep -q 1 ||
    sudo -u postgres createuser --createdb "$USER"

echo "== 2/5 download $ZIP from vpic.nhtsa.dot.gov"
mkdir -p "$DIR" && cd "$DIR"
[ -f "$ZIP" ] || curl -fL --retry 4 -o "$ZIP" "https://vpic.nhtsa.dot.gov/downloads/$ZIP"
rm -rf unpacked && mkdir unpacked && unzip -q "$ZIP" -d unpacked
DUMP=$(find unpacked -type f | head -1)
echo "   dump: $DUMP"

echo "== 3/5 restore into database 'vpic' (replaces an older restore)"
dropdb --if-exists vpic
createdb vpic
pg_restore --no-owner --no-privileges -d vpic "$DUMP"

echo "== 4/5 test decode (a 2000 Forester pattern)"
psql -d vpic -Atc "SELECT row_to_json(t) FROM vpic.spVinDecode('JF1SF6557YH712345') t" |
    grep -iE '"(make|model|model year|displacement \(l\)|transmission style)"' | head -8 || true

echo "== 5/5 app check"
~/subaru_cyberdeck/venv/bin/python -c "
import sys; sys.path.insert(0, '$HOME/subaru_cyberdeck/app')
import vpic_local; print(vpic_local.vpic_decode('JF1SF6557YH712345'))"
echo "Done. The app now decodes VINs offline on this PC."
