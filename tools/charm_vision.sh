#!/bin/bash
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
echo "charm_vision.sh is not built yet. See RUNBOOK.md, 'Vision pass: design'."
echo "Pictures waiting per make:  $CK/venv/bin/python $CK/kb/charm_images.py stats --all"
exit 1
