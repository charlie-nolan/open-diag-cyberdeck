#!/usr/bin/env python3
"""Scrape Subaru repair threads from the Arctic Shift Reddit archive into raw/reddit/reddit_subaru.db.

Personal, offline use for the cyberdeck. Resumable: each subreddit keeps a cursor in crawl_state,
so just re-run the script after an interruption.

Per subreddit:
  1. posts    page every submission (sort=asc by created_utc, limit=auto) and keep the
              repair-relevant ones (DTC codes or repair keywords), tagged with model / year /
              chassis / engine / DTCs.
  2. comments page every comment in the subreddit the same way and keep the ones that belong to
              a kept post. (Paging the subreddit in bulk is ~10x fewer requests than fetching one
              comment tree per post.) Usernames are never stored; only an is_op flag.

API docs: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md

  python3 scrape_reddit_subaru.py                     # all subreddits, resume where it left off
  python3 scrape_reddit_subaru.py --subs SubaruForester
  python3 scrape_reddit_subaru.py --stats
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "raw", "reddit", "reddit_subaru.db")
API = "https://arctic-shift.photon-reddit.com/api"
REDDIT_START = 1104537600  # 2005-01-01; the API rejects after=0
SALT_PATH = os.path.join(HERE, "raw", "reddit", ".op_salt")
UA = "subaru-cyberdeck-research/1.0 (personal offline repair knowledge base; ~1 req/s)"

# Most relevant first. r/Impreza is empty in the archive; r/subaruimpreza is the real one.
# value = model implied by the subreddit when the text names none
SUBREDDITS = {
    "SubaruForester": "Forester", "subaru": None, "subaruoutback": "Outback",
    "subarulegacy": "Legacy", "subaruimpreza": "Impreza", "SubaruBaja": "Baja",
    "Crosstrek": "Crosstrek", "WRX": "WRX", "SubaruAscent": "Ascent",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS reddit_post (
    id TEXT PRIMARY KEY, subreddit TEXT, url TEXT, title TEXT, body TEXT, flair TEXT,
    created_utc INT, score INT, num_comments INT,
    models TEXT, years TEXT, chassis TEXT, engines TEXT, dtcs TEXT, model_implied INT);
CREATE INDEX IF NOT EXISTS ix_post_sub ON reddit_post(subreddit, created_utc);
CREATE TABLE IF NOT EXISTS reddit_post_dtc (post_id TEXT, dtc TEXT, PRIMARY KEY (post_id, dtc));
CREATE INDEX IF NOT EXISTS ix_post_dtc ON reddit_post_dtc(dtc);
CREATE TABLE IF NOT EXISTS reddit_comment (
    id TEXT PRIMARY KEY, post_id TEXT, parent_id TEXT, body TEXT, score INT,
    created_utc INT, is_op INT);
CREATE INDEX IF NOT EXISTS ix_comment_post ON reddit_comment(post_id);
CREATE TABLE IF NOT EXISTS crawl_state (
    subreddit TEXT PRIMARY KEY, last_created_utc INT, done INT DEFAULT 0,
    comments_last_utc INT, comments_done INT DEFAULT 0, seen_posts INT DEFAULT 0,
    seen_comments INT DEFAULT 0);
-- transient: keyed hash of post author (op_hash), only to compute reddit_comment.is_op;
-- emptied when a subreddit is done. Plaintext usernames are never written.
CREATE TABLE IF NOT EXISTS _op (post_id TEXT PRIMARY KEY, author TEXT);
"""

_salt = None


def op_hash(author):
    """Keyed hash of a username, enough to test comment author == post author. None for deleted."""
    global _salt
    if not author or author == "[deleted]":
        return None
    if _salt is None:
        if not os.path.exists(SALT_PATH):
            os.makedirs(os.path.dirname(SALT_PATH), exist_ok=True)
            fd = os.open(SALT_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(16))
        with open(SALT_PATH, "rb") as f:
            _salt = f.read()
    return hashlib.blake2b(author.lower().encode(), key=_salt, digest_size=12).hexdigest()


# ---------------------------------------------------------------- relevance + tagging
DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
KEYWORDS_RE = re.compile(r"""\b(?:
    check\ engine | CEL | misfir\w* | head\ gaskets? | HG | overheat\w* | coolant |
    stall(?:s|ed|ing)? | idl(?:e|es|ing) | no\ start | won'?t\ start | doesn'?t\ start |
    rough | knock(?:s|ing)? | tick(?:s|ing)? | leak(?:s|ing|ed)? | wheel\ bearings? |
    CV | clutch | transmission | tranny | CVT | slipping | shudder\w* | rust(?:ed|ing|y)? |
    timing\ belt | EJ2[025]\d? | FB2[05] | FA2[04] | EZ3[06] |
    diagnos\w* | fixed | solved
)\b""", re.I | re.X)

MODELS = {
    "Forester": r"forester|foz", "Outback": r"outback", "Legacy": r"legacy", "Impreza": r"impreza",
    "WRX": r"wrx", "STI": r"sti", "Baja": r"baja", "Tribeca": r"tribeca", "BRZ": r"brz",
    "Crosstrek": r"crosstrek|xv", "SVX": r"svx", "Justy": r"justy", "Loyale": r"loyale",
    "Ascent": r"ascent",
}
MODEL_RE = {m: re.compile(rf"\b(?:{p})\b", re.I) for m, p in MODELS.items()}
# "Ascent"/"XV"/"STI" as plain words are noisy; require the capitalised spelling for those
CASE_SENSITIVE = {"Ascent": re.compile(r"\bAscent\b"), "STI": re.compile(r"\bST[Ii]\b"),
                  "Crosstrek": re.compile(r"\b(?:[Cc]rosstrek|XV)\b")}
YEAR4_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")
YEAR2_RE = re.compile(r"(?:\b|['’])(\d{2})\s*(?:subaru\s+)?$", re.I)  # "'02 " / "02 " right before a model
CHASSIS_RE = re.compile(r"\b(S[FGHJK]|G[CDEHRVP]|B[CDEGHJLMNPRST])\b")   # uppercase, standalone only
ENGINE_RE = re.compile(r"\b(EJ2[025]\d?|EJ18|EJ15|EJ16|FB2[05]|FA2[04]|FA24F|EZ3[06]|EG33|ER27)\b", re.I)


def relevant(text):
    return bool(DTC_RE.search(text) or KEYWORDS_RE.search(text))


def _y2(two):
    n = int(two)
    return 1900 + n if n >= 80 else (2000 + n if n <= 29 else None)


def tag(title, body, implied_model):
    text = f"{title}\n{body}"
    models, years = [], set()
    for m, rx in MODEL_RE.items():
        rx = CASE_SENSITIVE.get(m, rx)
        for hit in rx.finditer(text):
            if m not in models:
                models.append(m)
            window = text[max(0, hit.start() - 30): hit.end() + 30]
            years.update(int(y) for y in YEAR4_RE.findall(window))
            y2 = YEAR2_RE.search(text[max(0, hit.start() - 12): hit.start()])
            if y2 and _y2(y2.group(1)):
                years.add(_y2(y2.group(1)))
    implied = 0
    if not models and implied_model:
        models, implied = [implied_model], 1
    if not years and models:  # "2003 - won't start" style titles
        years.update(int(y) for y in YEAR4_RE.findall(title))
    years = {y for y in years if 1985 <= y <= 2027}
    letters = [c for c in text if c.isalpha()]
    shouting = letters and sum(c.isupper() for c in letters) / len(letters) > 0.5
    chassis = [] if shouting else sorted(set(CHASSIS_RE.findall(text)))
    engines = sorted({e.upper() for e in ENGINE_RE.findall(text)})
    dtcs = sorted({d.upper() for d in DTC_RE.findall(text)})
    return models, sorted(years), chassis, engines, dtcs, implied


def clean(s):
    s = (s or "").strip()
    return None if s in ("", "[deleted]", "[removed]") else s


# ---------------------------------------------------------------- HTTP
class Api:
    def __init__(self, delay, log):
        self.delay, self.log, self.last, self.n = delay, log, 0.0, 0

    def get(self, path, **params):
        url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
        backoff = 5
        while True:
            wait = self.delay - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            self.n += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=120) as r:
                    d = json.load(r)
                if d.get("error"):
                    raise RuntimeError(d["error"])
                return d.get("data") or []
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    reset = float(e.headers.get("X-RateLimit-Reset") or 30) + 1
                    self.log(f"  429 rate limited, sleeping {reset:.0f}s")
                    time.sleep(reset)
                    continue
                err = f"HTTP {e.code}"
                try:
                    err += " " + e.read().decode()[:200]
                except Exception:
                    pass
                if e.code in (400, 404):  # a bad request will not fix itself (422 = server timeout, retry)
                    self.log(f"  giving up: {err}")
                    raise
            except Exception as e:  # timeouts, "Timeout. Maybe slow down a bit", resets
                err = str(e)[:200]
            self.log(f"  retry in {backoff}s: {err}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 600)


# ---------------------------------------------------------------- crawl
def crawl_posts(conn, api, sub, log):
    st = conn.execute("SELECT last_created_utc, done, seen_posts FROM crawl_state WHERE subreddit=?", (sub,)).fetchone()
    if st and st[1]:
        return
    after, seen = (st[0] or REDDIT_START, st[2] or 0) if st else (REDDIT_START, 0)
    implied_model = SUBREDDITS.get(sub)
    log(f"[{sub}] posts from {time.strftime('%Y-%m-%d', time.gmtime(after))}")
    while True:
        rows = api.get("posts/search", subreddit=sub, sort="asc", after=after, limit="auto",
                       fields="id,author,title,selftext,created_utc,score,num_comments,link_flair_text")
        rows = [r for r in rows if r.get("created_utc") is not None]
        if not rows:
            break
        kept = []
        for r in rows:
            title, body = r.get("title") or "", clean(r.get("selftext"))
            if not relevant(f"{title}\n{body or ''}"):
                continue
            models, years, chassis, engines, dtcs, implied = tag(title, body or "", implied_model)
            kept.append((r["id"], sub, f"https://www.reddit.com/r/{sub}/comments/{r['id']}/", title, body,
                         r.get("link_flair_text"), int(r["created_utc"]), r.get("score"), r.get("num_comments"),
                         ",".join(models), ",".join(map(str, years)), ",".join(chassis), ",".join(engines),
                         ",".join(dtcs), implied, op_hash(r.get("author"))))
        conn.executemany("INSERT OR REPLACE INTO reddit_post VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         [k[:-1] for k in kept])
        conn.executemany("INSERT OR IGNORE INTO reddit_post_dtc VALUES(?,?)",
                         [(k[0], d) for k in kept for d in k[13].split(",") if d])
        conn.executemany("INSERT OR REPLACE INTO _op VALUES(?,?)",
                         [(k[0], k[-1]) for k in kept if k[-1]])
        last = max(int(r["created_utc"]) for r in rows)
        # a full page sharing one timestamp would loop forever; step past it
        after = last if last > after else after + 1
        seen += len(rows)
        conn.execute("INSERT INTO crawl_state(subreddit, last_created_utc, seen_posts) VALUES(?,?,?) "
                     "ON CONFLICT(subreddit) DO UPDATE SET last_created_utc=excluded.last_created_utc, "
                     "seen_posts=excluded.seen_posts", (sub, after, seen))
        conn.commit()
        n_kept = conn.execute("SELECT count(*) FROM reddit_post WHERE subreddit=?", (sub,)).fetchone()[0]
        log(f"[{sub}] posts {seen} seen, {n_kept} kept, at {time.strftime('%Y-%m-%d', time.gmtime(last))}")
    conn.execute("UPDATE crawl_state SET done=1 WHERE subreddit=?", (sub,))
    conn.commit()


def crawl_comments(conn, api, sub, log):
    st = conn.execute("SELECT comments_last_utc, comments_done, seen_comments FROM crawl_state WHERE subreddit=?",
                      (sub,)).fetchone()
    if st[1]:
        return
    after, seen = st[0] or 0, st[2] or 0
    first = conn.execute("SELECT min(created_utc) FROM reddit_post WHERE subreddit=? AND num_comments>0",
                         (sub,)).fetchone()[0]
    if first is None:
        conn.execute("UPDATE crawl_state SET comments_done=1 WHERE subreddit=?", (sub,))
        conn.commit()
        return
    after = max(after, first - 1)
    kept_ids = {r[0] for r in conn.execute("SELECT id FROM reddit_post WHERE subreddit=?", (sub,))}
    ops = dict(conn.execute("SELECT o.post_id, o.author FROM _op o JOIN reddit_post p ON p.id=o.post_id "
                            "WHERE p.subreddit=?", (sub,)))
    log(f"[{sub}] comments from {time.strftime('%Y-%m-%d', time.gmtime(after))} "
        f"for {len(kept_ids)} kept posts")
    while True:
        rows = api.get("comments/search", subreddit=sub, sort="asc", after=after, limit="auto",
                       fields="id,author,body,score,created_utc,link_id,parent_id")
        rows = [r for r in rows if r.get("created_utc") is not None]
        if not rows:
            break
        batch = []
        for r in rows:
            pid = (r.get("link_id") or "").removeprefix("t3_")
            body = clean(r.get("body"))
            author = r.get("author")
            if pid not in kept_ids or not body or author == "AutoModerator":
                continue
            batch.append((r["id"], pid, r.get("parent_id"), body, r.get("score"), int(r["created_utc"]),
                          int(op_hash(author) is not None and ops.get(pid) == op_hash(author))))
        conn.executemany("INSERT OR REPLACE INTO reddit_comment VALUES(?,?,?,?,?,?,?)", batch)
        last = max(int(r["created_utc"]) for r in rows)
        after = last if last > after else after + 1
        seen += len(rows)
        conn.execute("UPDATE crawl_state SET comments_last_utc=?, seen_comments=? WHERE subreddit=?",
                     (after, seen, sub))
        conn.commit()
        if api.n % 10 == 0:
            n = conn.execute("SELECT count(*) FROM reddit_comment c JOIN reddit_post p ON p.id=c.post_id "
                             "WHERE p.subreddit=?", (sub,)).fetchone()[0]
            log(f"[{sub}] comments {seen} seen, {n} kept, at {time.strftime('%Y-%m-%d', time.gmtime(last))}")
    conn.execute("UPDATE crawl_state SET comments_done=1 WHERE subreddit=?", (sub,))
    conn.execute("DELETE FROM _op WHERE post_id IN (SELECT id FROM reddit_post WHERE subreddit=?)", (sub,))
    conn.commit()


def stats(conn):
    out = []
    for sub, posts, cm in conn.execute(
            "SELECT p.subreddit, count(*), (SELECT count(*) FROM reddit_comment c JOIN reddit_post q "
            "ON q.id=c.post_id WHERE q.subreddit=p.subreddit) FROM reddit_post p GROUP BY 1 ORDER BY 2 DESC"):
        st = conn.execute("SELECT seen_posts, done, comments_done FROM crawl_state WHERE subreddit=?", (sub,)).fetchone()
        out.append(f"  {sub:16} posts kept {posts:>7} of {st[0] if st else '?':>7} seen | comments {cm:>8} | "
                   f"posts {'done' if st and st[1] else 'running'}, comments {'done' if st and st[2] else 'pending'}")
    models = {}
    for (m,) in conn.execute("SELECT models FROM reddit_post"):
        for x in (m or "").split(","):
            if x:
                models[x] = models.get(x, 0) + 1
    out.append("  per model: " + ", ".join(f"{k} {v}" for k, v in sorted(models.items(), key=lambda kv: -kv[1])))
    top = conn.execute("SELECT dtc, count(*) FROM reddit_post_dtc GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()
    out.append("  top DTCs: " + ", ".join(f"{d} {n}" for d, n in top))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--subs", nargs="+", default=list(SUBREDDITS))
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.db)), exist_ok=True)
    conn = sqlite3.connect(a.db)
    conn.executescript(SCHEMA)
    if a.stats:
        print(stats(conn))
        return

    def log(msg):
        print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)

    api = Api(a.delay, log)
    for sub in a.subs:
        crawl_posts(conn, api, sub, log)
        crawl_comments(conn, api, sub, log)
        log(f"[{sub}] DONE\n" + stats(conn))
    log(f"all done, {api.n} requests, {os.path.getsize(a.db) / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
