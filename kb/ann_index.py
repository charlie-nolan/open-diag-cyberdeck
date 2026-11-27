"""Approximate vector index for a make DB, kept in SQLite (sqlite-vec), for the deck's ARM CPU as well as the PC.

sqlite-vec 0.1.9 has no graph/IVF index; its fast path is binary quantization: 1 bit per dimension (128 bytes a row
instead of 1024), searched by Hamming distance in C. The retriever takes the ANN_RESCORE x k nearest by bits, then
scores those exactly with the stored int8 vectors (numpy dot product), so the order it returns is exact over the
shortlist. No car matrix is loaded into Python memory and an all-cars search on a 1.2M-row make is no longer skipped.

The index is a sidecar file next to the DB, so the make DB itself is not rewritten (and the deck copy is small):
    kb/<make>.db  ->  kb/<make>.ann.db  (tables section_vec_bin, knowledge_vec_bin if the DB has knowledge_vec)

    python3 kb/ann_index.py build kb/acura.db [kb/ford.db ...]     # or --all
    python3 kb/ann_index.py check kb/acura.db                        # row counts vs the DB
Rebuild after `embed` adds rows (build skips a table whose row count already matches).
"""
import argparse
import glob
import os
import sqlite3
import sys
import time

import sqlite_vec

TABLES = ("section_vec", "knowledge_vec", "comm_doc_vec")
BATCH = 20000


def ann_path(db):
    return os.path.splitext(os.path.abspath(db))[0] + ".ann.db"


def _open(db):
    src = sqlite3.connect(f"file:{os.path.abspath(db)}?mode=ro", uri=True)
    src.enable_load_extension(True)
    sqlite_vec.load(src)
    return src


def build(db, log=print):
    src = _open(db)
    out_path = ann_path(db)
    dst = sqlite3.connect(out_path)
    dst.enable_load_extension(True)
    sqlite_vec.load(dst)
    for t in TABLES:
        if not src.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            continue
        n = src.execute(f"SELECT count(*) FROM {t}_rowids").fetchone()[0]
        have = dst.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"{t}_bin",)).fetchone()
        if have and dst.execute(f"SELECT count(*) FROM {t}_bin_rowids").fetchone()[0] == n:
            log(f"{os.path.basename(db)} {t}: {n} rows, index up to date")
            continue
        dst.execute(f"DROP TABLE IF EXISTS {t}_bin")
        dst.execute(f"CREATE VIRTUAL TABLE {t}_bin USING vec0(id INTEGER PRIMARY KEY, embedding bit[1024])")
        t0, done, last = time.time(), 0, -1
        while True:
            rows = src.execute(f"SELECT id, vec_quantize_binary(vec_int8(embedding)) FROM {t} WHERE id > ? "
                               f"ORDER BY id LIMIT ?", (last, BATCH)).fetchall()
            if not rows:
                break
            dst.executemany(f"INSERT INTO {t}_bin(id, embedding) VALUES (?, vec_bit(?))", rows)
            dst.commit()
            done += len(rows)
            last = rows[-1][0]
        log(f"{os.path.basename(db)} {t}: {done} rows indexed in {time.time() - t0:.0f} s")
    dst.execute("CREATE TABLE IF NOT EXISTS ann_meta (k TEXT PRIMARY KEY, v TEXT)")
    dst.execute("INSERT OR REPLACE INTO ann_meta VALUES ('source', ?), ('built', datetime('now'))",
                (os.path.basename(db),))
    dst.commit()
    dst.close()
    return out_path


def check(db):
    src = _open(db)
    p = ann_path(db)
    if not os.path.isfile(p):
        return f"{os.path.basename(db)}: no index"
    dst = _open(p)
    bits = []
    for t in TABLES:
        if src.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone():
            n = src.execute(f"SELECT count(*) FROM {t}_rowids").fetchone()[0]
            m = (dst.execute(f"SELECT count(*) FROM {t}_bin_rowids").fetchone()[0]
                 if dst.execute("SELECT 1 FROM sqlite_master WHERE name=?", (f"{t}_bin",)).fetchone() else 0)
            bits.append(f"{t} {m}/{n}" + ("" if m == n else " STALE"))
    return f"{os.path.basename(db)}: " + ", ".join(bits)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("build", "check"))
    ap.add_argument("dbs", nargs="*")
    ap.add_argument("--all", action="store_true", help="every kb/<make>.db that has section_vec")
    a = ap.parse_args()
    dbs = a.dbs
    if a.all:
        here = os.path.dirname(os.path.abspath(__file__))
        dbs = []
        for p in sorted(glob.glob(os.path.join(here, "*.db"))):
            if p.endswith(".ann.db"):
                continue
            try:
                c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='section_vec'").fetchone():
                    dbs.append(p)
            except sqlite3.Error:
                pass
    for db in dbs:
        print(build(db) if a.cmd == "build" else check(db), flush=True)


if __name__ == "__main__":
    sys.exit(main())
