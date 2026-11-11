"""teamdeck: the deck's window onto Team mode (team/service.py on the PC). See teamdeck/README.md."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(ROOT, "app"), os.path.join(ROOT, "app", "ssm"), os.path.join(ROOT, "kb"),
           os.path.join(ROOT)):
    if _p not in sys.path:
        sys.path.append(_p)   # appended, so teamdeck's own modules win any name clash
