"""
TEST DRIVE files: one JSON line per reading in drives/, flushed to disk as each reading arrives, so a crash or a pulled
plug loses at most the reading in progress. First line is the header (car, start time), the rest {t, codes, live}.
"""
import datetime
import glob
import json
import os
import re

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "drives")


def _stamp(drive_id):
    return datetime.datetime.fromtimestamp(float(drive_id)).strftime("%Y%m%d_%H%M%S")


def _write(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")
        f.flush()
        os.fsync(f.fileno())


def start(drive_id, car="", folder=None):
    """Create the file for a drive. Returns its path."""
    folder = folder or DIR
    os.makedirs(folder, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", car).strip("_")
    path = os.path.join(folder, f"drive_{_stamp(drive_id)}" + (f"_{slug}" if slug else "") + ".jsonl")
    _write(path, {"drive_id": drive_id, "car": car})
    return path


def add(path, t, codes, live):
    _write(path, {"t": t, "codes": codes, "live": live})


def has(drive_id, folder=None):
    return bool(glob.glob(os.path.join(folder or DIR, f"drive_{_stamp(drive_id)}*.jsonl")))


def read(path):
    """(header, [(t, codes, live)], oldest first). A last line cut off by a crash is skipped."""
    head, rows = {}, []
    with open(path) as f:
        for line in f:
            try:
                x = json.loads(line)
            except ValueError:
                continue
            if "t" in x:
                rows.append((x["t"], x.get("codes", ""), x.get("live", "")))
            else:
                head = x
    return head, sorted(rows, key=lambda r: r[0])
