#!/usr/bin/env python3
"""
Link each make DB's CHARM pages to the pictures in the CHARM images LMDB (the Seagate download), and sort the
pictures by what they are, so the vision pass can be aimed (diagnostic charts and spec tables first).

  python3 charm_images.py link acura          # one make DB (kb/<make>.db); also subaru
  python3 charm_images.py link --all          # every finished make (kb/.<make>.done) + subaru
  python3 charm_images.py sort acura          # re-derive kind only (no LMDB needed)
  python3 charm_images.py stats [--all|make]  # counts per kind + vision-time estimate

Adds one table to the make DB (nothing else touched):
  charm_image(img_key, section_id, page_title, bytes, width, height, fmt, kind)
img_key is the LMDB key (the page's "/images/..." path without "/images"); section_id is the first page that
shows it. kind = chart | wiring | spec_table | location | exploded | other, from the page's path (CHARM's own
headings) first, picture shape second. No model involved.

The LMDB is opened read-only, lock-free, never written (it sits on NTFS). CPU/disk only, ~5 ms per picture.
Makes still building (no .done marker, or the build has the DB open) are skipped and listed.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LMDB_DIR = os.path.join(os.environ.get("CYBERDECK_ARCHIVE", os.path.expanduser("~/cyberdeck_archive")), "charm", "operation-charm", "lmdb-images")
VISION_SEC = 50          # qwen3.6:35b per page, measured 2026-10-21 (logs/ocr_bench)

RULES = [  # first match wins; checked against the page path (CHARM's headings, " > " separated)
    ("spec_table", re.compile(r"specification", re.I)),
    ("chart", re.compile(r"code charts?|diagnostic chart|trouble code tests|symptom related diagnostic|"
                         r"diagnostic procedure|diagnostic trouble code", re.I)),
    ("wiring", re.compile(r"electrical diagram|wiring diagram|circuit diagram|system diagram|diagrams by number|"
                          r"connector view|connector id|pin ?out|schematic|vacuum and vapor hose diagram", re.I)),
    ("location", re.compile(r"location", re.I)),
    ("exploded", re.compile(r"exploded|component(s)? view|disassembl|assembly view|\bcomponents$", re.I)),
]


def kind_of(path, width, height):
    for k, rx in RULES:
        if rx.search(path or ""):
            return k
    # no telling heading: under a testing/diagnosis heading, a tall narrow picture is usually a flowchart
    if width and height and height > 1.6 * width and width > 400 and re.search(r"testing and inspection|diagnos",
                                                                               path or "", re.I):
        return "chart"
    return "other"


def dims(buf):
    """(fmt, width, height) from the first bytes of a PNG, JPEG or GIF."""
    b = bytes(buf[:65536])
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        w, h = struct.unpack(">II", b[16:24])
        return "png", w, h
    if b[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", b[6:10])
        return "gif", w, h
    if b[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            m = b[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return "jpeg", w, h
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
        return "jpeg", None, None
    return "unknown", None, None


SCHEMA = """CREATE TABLE IF NOT EXISTS charm_image (img_key TEXT PRIMARY KEY, section_id INTEGER, page_title TEXT,
    bytes INTEGER, width INTEGER, height INTEGER, fmt TEXT, kind TEXT);
CREATE INDEX IF NOT EXISTS charm_image_kind ON charm_image(kind);
CREATE INDEX IF NOT EXISTS charm_image_section ON charm_image(section_id);"""


def db_path(name):
    for p in (os.path.join(HERE, f"{name}.db"), os.path.join(os.environ.get("CYBERDECK_ARCHIVE", os.path.expanduser("~/cyberdeck_archive")), "kb_build", f"{name}.db")):
        if os.path.isfile(p):
            return p
    return None


def ready(name):
    """A finished make (or subaru) the CHARM build isn't writing."""
    if name != "subaru" and not os.path.isfile(os.path.join(HERE, f".{name}.done")):
        return False
    return os.system(f"pgrep -f 'build_subaru_kb.py --db .*/{name}\\.db' >/dev/null") != 0


def link(name):
    import lmdb
    db = db_path(name)
    if not db or not ready(name):
        print(f"{name}: skipped (still building or no DB); run again later", flush=True)
        return False
    env = lmdb.open(LMDB_DIR, readonly=True, lock=False, subdir=True,
                    map_size=os.path.getsize(os.path.join(LMDB_DIR, "data.mdb")) + (1 << 20))
    c = sqlite3.connect(db)
    c.executescript(SCHEMA)
    have = {r[0] for r in c.execute("SELECT img_key FROM charm_image")}      # resumable
    seen, rows, n, missing, t0 = set(have), [], 0, 0, time.time()
    q = ("SELECT s.id, s.path, s.title, s.images FROM section s JOIN doc d ON d.id=s.doc_id "
         "WHERE d.kind='charm' AND s.images IS NOT NULL AND s.images<>'[]' ORDER BY s.id")
    with env.begin(buffers=True) as t:
        for sid, path, title, images in c.execute(q).fetchall():
            for src in json.loads(images):
                key = src[len("/images"):] if src.startswith("/images/") else src
                if key in seen:
                    continue
                seen.add(key)
                v = t.get(key.encode())
                if v is None:
                    missing += 1
                    rows.append((key, sid, title, None, None, None, "missing", kind_of(path, None, None)))
                else:
                    fmt, w, h = dims(v)
                    rows.append((key, sid, title, len(v), w, h, fmt, kind_of(path, w, h)))
                n += 1
                if len(rows) >= 5000:
                    c.executemany("INSERT OR IGNORE INTO charm_image VALUES (?,?,?,?,?,?,?,?)", rows)
                    c.commit()
                    rows = []
                    print(f"  {name}: {n} pictures ({n / (time.time() - t0):.0f}/s)", flush=True)
    c.executemany("INSERT OR IGNORE INTO charm_image VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    print(f"{name}: {n} new pictures linked ({missing} not in the LMDB), {len(seen)} total, "
          f"{time.time() - t0:.0f} s", flush=True)
    return True


def sort(name):
    db = db_path(name)
    c = sqlite3.connect(db)
    rows = c.execute("SELECT i.img_key, s.path, i.width, i.height FROM charm_image i JOIN section s ON s.id=i.section_id").fetchall()
    c.executemany("UPDATE charm_image SET kind=? WHERE img_key=?", [(kind_of(p, w, h), k) for k, p, w, h in rows])
    c.commit()
    print(f"{name}: {len(rows)} pictures re-sorted")


def stats(names):
    tot, out = {}, []
    for name in names:
        db = db_path(name)
        if not db:
            continue
        try:
            rows = dict(sqlite3.connect(f"file:{db}?mode=ro", uri=True).execute(
                "SELECT kind, count(*) FROM charm_image GROUP BY kind").fetchall())
        except sqlite3.OperationalError:
            continue
        for k, v in rows.items():
            tot[k] = tot.get(k, 0) + v
        todo = rows.get("chart", 0) + rows.get("spec_table", 0)
        out.append((name, rows, todo))
    for name, rows, todo in sorted(out, key=lambda x: -x[2]):
        print(f"{name:15} " + " ".join(f"{k}={rows.get(k, 0)}" for k in ("chart", "spec_table", "wiring", "location",
              "exploded", "other")) + f"  vision(chart+spec) {todo} = {todo * VISION_SEC / 3600:.0f} h")
    todo = tot.get("chart", 0) + tot.get("spec_table", 0)
    print(f"TOTAL {sum(tot.values())} pictures: {tot}; chart+spec_table {todo} = {todo * VISION_SEC / 3600:.0f} h "
          f"at {VISION_SEC} s each")


def all_names():
    names = ["subaru"] + sorted(os.path.basename(p)[1:-5] for p in glob.glob(os.path.join(HERE, ".*.done")))
    return list(dict.fromkeys(names))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["link", "sort", "stats"])
    ap.add_argument("make", nargs="?")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    names = all_names() if a.all or not a.make else [a.make]
    if a.cmd == "link":
        later = [n for n in names if not link(n)]
        if later:
            print("not linked yet (run `link --all` again later):", " ".join(later))
    elif a.cmd == "sort":
        for n in names:
            sort(n)
    else:
        stats(names)


if __name__ == "__main__":
    main()
