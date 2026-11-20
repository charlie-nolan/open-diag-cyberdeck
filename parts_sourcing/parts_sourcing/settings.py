"""sourcing.json, the parts-sourcing settings shared by the package and its modules."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def settings(path=None):
    """sourcing.json; its relative DB paths are relative to the checkout."""
    with open(path or os.path.join(HERE, "sourcing.json")) as f:
        cfg = json.load(f)
    for k in ("kb_db", "yards_db"):
        if cfg.get(k):
            cfg[k] = os.path.join(ROOT, os.path.expanduser(cfg[k]))
    return cfg
