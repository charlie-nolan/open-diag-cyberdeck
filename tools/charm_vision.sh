#!/bin/bash
# NOT BUILT YET: vision pass over the CHARM pictures (diagnostic charts + spec tables -> text rows).
# The design is in RUNBOOK.md ("Vision pass: design"). What exists already:
#   kb/charm_images.py link/stats   which pictures each make has, sorted by kind, with the hours estimate
#   kb/ocr_bench.py                 the tested reader + number cross-check (qwen3.6:35b vs tesseract vs text)
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
echo "charm_vision.sh is not built yet. See RUNBOOK.md, 'Vision pass: design'."
echo "Pictures waiting per make:  $CK/venv/bin/python $CK/kb/charm_images.py stats --all"
exit 1
