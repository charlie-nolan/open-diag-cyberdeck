#!/usr/bin/env python3
"""Load the Pushshift per-subreddit dumps (Watchful1 "subreddits24", 2005-06 .. 2024-12) into
raw/reddit/reddit_subaru.db, using the same schema, relevance filter and tags as
scrape_reddit_subaru.py. Rows are deduped by post/comment id (dump rows replace API rows,
since dump scores are final). Usernames are never stored; comments get an is_op flag.

  python3 load_pushshift_subaru.py                  # every *_submissions.zst in raw/reddit/pushshift
  python3 load_pushshift_subaru.py --subs SubaruForester subaru
"""
import argparse
import glob
import io
import json
import os
import sqlite3
import sys
import time

import zstandard

import scrape_reddit_subaru as s

HERE = os.path.dirname(os.path.abspath(__file__))
DUMP_DIR = os.path.join(HERE, "raw", "reddit", "pushshift", "reddit", "subreddits24")

# subreddit -> model implied when the text names none (adds to scrape_reddit_subaru.SUBREDDITS)
IMPLIED = dict(s.SUBREDDITS, Subaru_Outback="Outback", Subaru_Crosstrek="Crosstrek",
               XVcrosstrek="Crosstrek", WRXSTi="WRX", wrx_vb="WRX", **{"2022wrx": "WRX"})


def read_ndjson(path):
    with open(path, "rb") as fh:
        dctx = zstandard.ZstdDecompressor(max_window_size=2 ** 31)
        for line in io.TextIOWrapper(dctx.stream_reader(fh), encoding="utf-8", errors="replace"):
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_sub(conn, sub, log):
    sub_path = os.path.join(DUMP_DIR, f"{sub}_submissions.zst")
    com_path = os.path.join(DUMP_DIR, f"{sub}_comments.zst")
    done = conn.execute("SELECT done FROM dump_state WHERE subreddit=?", (sub,)).fetchone()
    if done and done[0]:
        log(f"[{sub}] already loaded")
        return
    implied_model = IMPLIED.get(sub)
    ops, seen, batch = {}, 0, []

    def flush_posts():
        conn.executemany("INSERT OR REPLACE INTO reddit_post VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        conn.executemany("INSERT OR IGNORE INTO reddit_post_dtc VALUES(?,?)",
                         [(b[0], d) for b in batch for d in b[13].split(",") if d])
        batch.clear()

    for r in read_ndjson(sub_path):
        seen += 1
        if r.get("created_utc") is None or not r.get("id"):
            continue
        title, body = r.get("title") or "", s.clean(r.get("selftext"))
        if not s.relevant(f"{title}\n{body or ''}"):
            continue
        models, years, chassis, engines, dtcs, implied = s.tag(title, body or "", implied_model)
        name = r.get("subreddit") or sub
        batch.append((r["id"], name, f"https://www.reddit.com/r/{name}/comments/{r['id']}/", title, body,
                      r.get("link_flair_text"), int(float(r["created_utc"])), r.get("score"), r.get("num_comments"),
                      ",".join(models), ",".join(map(str, years)), ",".join(chassis), ",".join(engines),
                      ",".join(dtcs), implied))
        a = r.get("author")
        ops[r["id"]] = a if a and a != "[deleted]" else None
        if len(batch) >= 5000:
            flush_posts()
    flush_posts()
    conn.commit()
    log(f"[{sub}] submissions: {seen} read, {len(ops)} kept")

    cseen = ckept = 0
    cbatch = []
    for r in read_ndjson(com_path) if os.path.exists(com_path) else ():
        cseen += 1
        pid = (r.get("link_id") or "").removeprefix("t3_")
        if pid not in ops:
            continue
        body, author = s.clean(r.get("body")), r.get("author")
        if not body or author == "AutoModerator" or r.get("created_utc") is None:
            continue
        op = ops[pid]
        cbatch.append((r["id"], pid, r.get("parent_id"), body, r.get("score"), int(float(r["created_utc"])),
                       int(op is not None and author == op)))
        ckept += 1
        if len(cbatch) >= 20000:
            conn.executemany("INSERT OR REPLACE INTO reddit_comment VALUES(?,?,?,?,?,?,?)", cbatch)
            conn.commit()
            cbatch.clear()
    conn.executemany("INSERT OR REPLACE INTO reddit_comment VALUES(?,?,?,?,?,?,?)", cbatch)
    conn.execute("INSERT OR REPLACE INTO dump_state VALUES(?,?,?,?,?,1)", (sub, seen, len(ops), cseen, ckept))
    conn.commit()
    log(f"[{sub}] comments: {cseen} read, {ckept} kept")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=s.DEFAULT_DB)
    ap.add_argument("--subs", nargs="+")
    a = ap.parse_args()
    subs = a.subs or sorted(os.path.basename(p)[:-len("_submissions.zst")]
                            for p in glob.glob(os.path.join(DUMP_DIR, "*_submissions.zst")) if os.path.getsize(p))
    conn = sqlite3.connect(a.db)
    conn.executescript(s.SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS dump_state (subreddit TEXT PRIMARY KEY, posts_read INT, posts_kept INT, "
                 "comments_read INT, comments_kept INT, done INT)")

    def log(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)

    for sub in subs:
        load_sub(conn, sub, log)
    print(s.stats(conn))
    log(f"done, {os.path.getsize(a.db) / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
