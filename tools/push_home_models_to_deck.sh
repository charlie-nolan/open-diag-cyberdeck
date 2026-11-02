#!/bin/sh
# Bring the deck's app up to date from this PC: every app/*.py (Cyberdeck Home models, PAST VISITS, VIN decode,
# vpic_local.py ...), kb/romid.db, and the deck's vPIC setup script. Files it replaces are kept as *.bak on the
# deck; config.json and the deck's cache are never touched. Also stops the app launching at boot.
#   sh ~/subaru_cyberdeck/tools/push_home_models_to_deck.sh [deck address]   (default: $CYBERDECK_DECK)
set -e
DECK=${1:-${CYBERDECK_DECK:?give the deck address or set CYBERDECK_DECK}}
CK=~/subaru_cyberdeck
rsync -a --backup --suffix=.bak "$CK"/app/*.py "$DECK":subaru_cyberdeck/app/
rsync -a "$CK/kb/romid.db" "$DECK":subaru_cyberdeck/kb/
rsync -a "$CK/tools/vpic_setup_deck.sh" "$DECK":subaru_cyberdeck/tools/
# no autostart: keep deck_setup.sh's entry as .off (rename back to turn it on again)
ssh "$DECK" 'A=~/.config/autostart/cyberdeck.desktop; if [ -f "$A" ]; then mv "$A" "$A.off"; echo "autostart off"; fi
cd ~/subaru_cyberdeck/app && python3 -m py_compile *.py && sha256sum cyberdeck_gui.py vehicle_id.py vpic_local.py'
echo "Done. Open Cyberdeck Home on the deck to use them."
