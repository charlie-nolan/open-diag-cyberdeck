"""diagent: manual-led diagnostic agent with a human in the loop (test app, see DIAG_AGENT_APP.md).

The factory manual's step table is the backbone. Python owns the tree walk, unit checks, spec
comparisons and safety preconditions; the model words the instructions, looks things up and keeps
the hypothesis board. Nothing here writes to the car.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(ROOT, "app"), os.path.join(ROOT, "app", "ssm"), os.path.join(ROOT, "kb")):
    if _p not in sys.path:
        sys.path.append(_p)   # appended, so diagent's own modules win any name clash
