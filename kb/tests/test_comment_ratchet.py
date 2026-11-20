import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_no_new_comments():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "comment_ratchet.py")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
