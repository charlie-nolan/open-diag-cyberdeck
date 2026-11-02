#!/usr/bin/env python3
"""Shrink the manual figures that subaru.db sections point at, for the Pi.

Every converted manual keeps its figures next to its markdown (test_batch_out/<doc title>/_page_N_Picture_M.jpeg)
and each section lists its figure names in section.images. This writes one small grayscale WebP per figure that
some section actually uses:

  kb/images/<doc id>/<figure name without extension>.webp

so the app finds a figure as images/{section.doc_id}/{stem}.webp next to subaru.db. subaru.db itself is only read.
Re-running skips figures already done.

  python3 ship_images.py                 # all manuals
  python3 ship_images.py --count         # only count figures and their current size
"""
import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ProcessPoolExecutor

CK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root


def shrink(job):
    src, dst, max_px, quality = job
    if os.path.exists(dst):
        return os.path.getsize(dst)
    from PIL import Image
    try:
        with Image.open(src) as im:
            im = im.convert("L")                      # manual figures are line art; colour only adds bytes
            im.thumbnail((max_px, max_px))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            im.save(dst + ".tmp", "WEBP", quality=quality, method=6)
        os.replace(dst + ".tmp", dst)
        return os.path.getsize(dst)
    except Exception as e:
        print(f"  skipped {src}: {e}", file=sys.stderr)
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", default=f"{CK}/kb/subaru.db")
    ap.add_argument("--src", default=f"{CK}/test_batch_out", help="folder holding one folder per converted manual")
    ap.add_argument("--out", help="default: images/ next to --kb")
    ap.add_argument("--max-px", type=int, default=1000, help="longest side in pixels")
    ap.add_argument("--quality", type=int, default=60)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--count", action="store_true")
    a = ap.parse_args()

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.kb)), "images")
    db = sqlite3.connect(f"file:{a.kb}?mode=ro", uri=True)
    jobs, missing, raw = [], 0, 0
    for doc_id, title in db.execute("SELECT id, title FROM doc WHERE kind='fsm' ORDER BY id").fetchall():
        names = set()
        for (imgs,) in db.execute("SELECT images FROM section WHERE doc_id=? AND images IS NOT NULL", (doc_id,)):
            names.update(json.loads(imgs))
        found = 0
        for n in sorted(names):
            src = os.path.join(a.src, title, n)
            if not os.path.isfile(src):
                missing += 1
                continue
            found += 1
            raw += os.path.getsize(src)
            jobs.append((src, os.path.join(out, str(doc_id), os.path.splitext(n)[0] + ".webp"), a.max_px, a.quality))
        print(f"{title}: {found} figures" + (f", {len(names) - found} not found" if len(names) > found else ""))
    print(f"images: {len(jobs)} figures, {raw / 1e6:.0f} MB as converted, {missing} listed but not found")
    if a.count or not jobs:
        return
    with ProcessPoolExecutor(a.jobs) as ex:
        total = sum(ex.map(shrink, jobs, chunksize=64))
    print(f"images: done, {total / 1e6:.0f} MB in {out}")


if __name__ == "__main__":
    main()
