#!/usr/bin/env python3
"""
Community-knowledge DB (COMMUNITY_DB.md): Reddit + forum threads for one make -> what people tested, read,
ruled out, suggested and finally fixed, each item quoting the thread. Cards (card_baking/) stay separate.

Steps (each takes --make <db name> or --all; state lives in kb/community/work/<make>.work.db, all resumable):
  prefilter  pass 0, Python only: keep threads that are about diagnosing/repairing a car and have a reply.
             Prints keep/drop per rule per subreddit/forum, next to what the card filter would keep.
  pilot      mark --n threads (default 300) picked at random, spread over the makes' subreddits/forums.
             Run with --makes a,b,c (no --make); then `extract --pilot` and `report --pilot`.
  extract    pass 1, local Qwen through Ollama (strict JSON, thinking off), one record per thread. Python checks
             every quote is in the thread and drops items whose quote fails. Checkpointed per thread; stats to
             the log every 2,000 threads. Refuses while tools/gpu_guard.sh says the GPU is busy (--force skips).
  tables     pass 2: kb/community/<make>.community.db (comm_* tables, counts, links to the make DB's manual
             sections through kb_retriever's own search). CPU.
  embed      Qwen3-Embedding-0.6B int8 vectors (comm_doc_vec) for thread summaries, symptoms, tests, chains.
  merge      copy comm_* into kb/<make>.db (previous copy saved under kb/community/backup/).
  report     throughput, JSON/quote failure rates, fields per thread, projected hours per make.
  sample     write --n random records with their thread to a folder, for the spot inspection.
  raw        every thread pass 0 kept -> kb/<make>.threads.db (full text + FTS), no model. CPU, minutes.
  rawembed   vectors for those threads (first 512 tokens each); GPU when gpu_guard says free, else the CPU.
             Resumable. Runtime lookup: community_ref.raw_threads().

  ~/subaru_cyberdeck/venv/bin/python kb/build_community.py --make subaru prefilter
  setsid nohup ~/subaru_cyberdeck/venv/bin/python kb/build_community.py --make subaru extract \
      > /dev/null 2>&1 &          # log: logs/community_subaru.log
Runtime lookup: app/community_ref.py. Community data suggests; the manual confirms.
"""
import argparse
import fcntl
import glob
import itertools
import json
import math
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
KB = os.path.join(ROOT, "kb")
OUT = os.path.join(KB, "community")
WORK = os.path.join(OUT, "work")
REDDIT = os.path.join(ROOT, "raw/reddit")
FORUMS = os.path.join(ROOT, "raw/forums")
GROUPS = os.path.join(KB, "charm_make_groups.txt")
sys.path.insert(0, os.path.join(ROOT, "card_baking"))
sys.path.insert(0, KB)
from extract_fixes import prefilter_score  # noqa: E402  (the card filter, for the side-by-side count)

# Forum dumps -> the make DB their threads go to (the record's own vehicle field says the exact model)
FORUM_MAKE = {"ultimatesubaru": "subaru", "gmtnation": "chevrolet"}
EXTRA_REDDIT = {"chevrolet": ["reddit_chevrolet_tahoe.db"]}
DEFAULT_MODEL = "qwen3-next:80b-a3b-instruct-q4_K_M"     # Home "deep" (app/cyberdeck_gui.py)
OLLAMA = "http://127.0.0.1:11434"
SEG_CHARS, MAX_CHARS, MAX_REPLIES = 1500, 9000, 14

LOG = None


def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    if LOG:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    if not LOG or sys.stdout.isatty():
        print(line, flush=True)


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)


def makes_with_data():
    out = []
    for f in sorted(glob.glob(os.path.join(REDDIT, "reddit_*.db"))):
        m = os.path.basename(f)[7:-3]
        if m not in ("broad", "chevrolet_tahoe"):
            out.append(m)
    for m in FORUM_MAKE.values():
        if m not in out:
            out.append(m)
    return ["subaru"] + [m for m in out if m != "subaru"]


def sources(make):
    out = []
    for name in [f"reddit_{make}.db"] + EXTRA_REDDIT.get(make, []):
        p = os.path.join(REDDIT, name)
        if os.path.isfile(p):
            out.append(("reddit", name[:-3], p))
    for name, m in FORUM_MAKE.items():
        p = os.path.join(FORUMS, f"{name}.db")
        if m == make and os.path.isfile(p):
            out.append(("forum", name, p))
    return out


# ============================================================================ work DB
WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread (
  src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT, title TEXT, created INTEGER, score INTEGER,
  n_replies INTEGER, segs TEXT NOT NULL, tags TEXT, rules TEXT, card_keep INTEGER, pilot INTEGER DEFAULT 0,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS prefilter_stat (src TEXT, sub TEXT, metric TEXT, n INTEGER, PRIMARY KEY (src, sub, metric));
CREATE TABLE IF NOT EXISTS sub_score (src TEXT, sub TEXT, p90 INTEGER, PRIMARY KEY (src, sub));
CREATE TABLE IF NOT EXISTS extract (
  src TEXT NOT NULL, tid TEXT NOT NULL, status TEXT NOT NULL, raw TEXT, rec TEXT, checks TEXT,
  llm_model TEXT, ms INTEGER, prompt_tokens INTEGER, eval_tokens INTEGER, tries INTEGER DEFAULT 1, at TEXT,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
"""


def work_db(make):
    os.makedirs(WORK, exist_ok=True)
    w = sqlite3.connect(os.path.join(WORK, f"{make}.work.db"), timeout=60)
    w.executescript(WORK_SCHEMA)
    w.execute("PRAGMA journal_mode=WAL")
    return w


# ============================================================================ thread assembly
def _clean(body):
    if not body or body.strip() in ("[deleted]", "[removed]"):
        return ""
    if "i am a bot" in body.lower():
        return ""
    return re.sub(r"\n{3,}", "\n\n", body.strip())


def reddit_threads(path):
    """(tid, sub, url, title, created, score, op_body, [(text, score, is_op, order)], tags) per post,
    merging posts and comments in post-id order (no full comment table in memory)."""
    c = ro(path)
    cc = c.cursor()
    comments = cc.execute("SELECT post_id, body, score, is_op, created_utc FROM reddit_comment ORDER BY post_id")
    groups = itertools.groupby(comments, key=lambda r: r[0])
    cur = next(groups, None)
    for pid, sub, url, title, body, flair, created, score, models, years, engines, dtcs in c.execute(
            "SELECT id, subreddit, url, title, body, flair, created_utc, score, models, years, engines, dtcs "
            "FROM reddit_post ORDER BY id"):
        while cur is not None and cur[0] < pid:
            cur = next(groups, None)
        cs = []
        if cur is not None and cur[0] == pid:
            cs = [(_clean(b), s or 0, op or 0, t or 0) for _, b, s, op, t in cur[1]]
            cur = next(groups, None)
        tags = {"models": models, "years": years, "engines": engines, "dtcs": dtcs, "flair": flair}
        yield pid, sub, url, title, created, score, _clean(body), [x for x in cs if x[0]], tags


def forum_threads(path):
    """Forum dumps: the thread's first post (page, position) is the OP's question; later posts flagged is_op
    are the OP's own replies. No scores."""
    c = ro(path)
    posts = c.cursor().execute("SELECT thread_id, body, is_op, page * 100000 + position FROM post "
                               "ORDER BY thread_id, page, position")
    groups = itertools.groupby(posts, key=lambda r: r[0])
    cur = next(groups, None)
    for tid, url, title, forum, started, models, years, engines, dtcs in c.execute(
            "SELECT id, url, title, forum, started, models, years, engines, dtcs FROM thread ORDER BY id"):
        while cur is not None and cur[0] < tid:
            cur = next(groups, None)
        if cur is None or cur[0] != tid:
            continue
        ps = [(_clean(b), 0, op or 0, o) for _, b, op, o in cur[1]]
        cur = next(groups, None)
        if not ps:
            continue
        op_body, rest = ps[0][0], [x for x in ps[1:] if x[0]]
        tags = {"models": models, "years": years, "engines": engines, "dtcs": dtcs}
        yield str(tid), forum, url, title, None, None, op_body, rest, tags


def segments(title, op_body, cs):
    """S0 = title + OP post. Then the OP's replies (all) and the best-scored other replies (up to MAX_REPLIES),
    shown in thread order and cut to MAX_CHARS. Each seg: {k: op|op_reply|reply, s: score, t: text}."""
    ops = [c for c in cs if c[2]]
    others = sorted([c for c in cs if not c[2]], key=lambda c: (-c[1], c[3]))[:MAX_REPLIES]
    chosen = sorted(ops + others, key=lambda c: c[3])
    segs = [{"k": "op", "s": None, "t": (f"{title or ''}\n\n{op_body or ''}").strip()[:SEG_CHARS * 2]}]
    total = len(segs[0]["t"])
    for body, score, is_op, _ in chosen:
        t = body[:SEG_CHARS]
        if total + len(t) > MAX_CHARS:
            break
        total += len(t)
        segs.append({"k": "op_reply" if is_op else "reply", "s": score, "t": t})
    return segs


def thread_text(segs):
    lab = {"op": "OP", "op_reply": "OP reply"}
    out = []
    for i, s in enumerate(segs):
        tag = lab.get(s["k"]) or (f"reply, score {s['s']}" if s["s"] is not None else "reply")
        out.append(f"[S{i} {tag}]\n{s['t']}")
    return "\n\n".join(out)


# ============================================================================ pass 0: prefilter
# Vocabulary that says a thread is about diagnosing/repairing a vehicle. Detection only: none of these words
# is tied to a cause (CLAUDE.md rule 1).
DTC_RE = re.compile(r"\b[PCBU][0-3][0-9A-F]{3}\b", re.I)
SYMPTOM_RE = re.compile(
    r"\b(misfir\w*|check engine|cel|mil|warning light|light (is )?on|rough|idl\w+|stall\w*|hesitat\w*|overheat\w*|"
    r"knock\w*|tick\w*|rattl\w*|whin\w*|grind\w*|clunk\w*|squeal\w*|squeak\w*|hum\w*|whistl\w*|clicking|leak\w*|"
    r"drip\w*|smoke|smok\w+|smell\w*|vibrat\w*|shak\w+|shudder\w*|won'?t start|no start|crank\w*|surg\w*|bog\w*|"
    r"stutter\w*|lean|rich|limp|slip\w*|hard shift\w*|clunk|pull(s|ing)? to|noise|sound\w*|dies|died|dead|"
    r"won'?t (turn|shift|go|move)|doesn'?t (start|shift|work)|not working|stopped working|fail\w*|broke\w*)\b", re.I)
PART_RE = re.compile(
    r"\b(sensor|pump|valve|coil|plugs?|gasket|seal|belt|bearing|alternator|battery|starter|transmission|trans|"
    r"cvt|clutch|brakes?|rotor|caliper|pads?|injector|thermostat|radiator|hose|axle|cv|strut|shock|spring|"
    r"control arm|tie rod|ball joint|relay|fuse|wiring|harness|connector|ground|ecu|pcm|ecm|tcm|module|"
    r"throttle|maf|map|o2|oxygen|catalytic|cat|exhaust|manifold|egr|pcv|purge|evap|head|cylinder|piston|"
    r"timing|chain|tensioner|pulley|compressor|condenser|heater core|blower|actuator|solenoid|switch|motor|"
    r"differential|diff|transfer case|driveshaft|u-?joint|hub|abs|steering|rack|fuel|coolant|oil|filter)\b", re.I)
TEST_RE = re.compile(
    r"\b(multimeter|voltmeter|ohm\w*|volts?|voltage|amps?|continuity|resistance|compression|leak[- ]?down|"
    r"smoke test\w*|scan ?tool|scanner|obd\w*|live data|freeze frame|fuel trims?|stft|ltft|pressure test\w*|"
    r"back ?prob\w*|tested|checked|measured|swapped|reading|readings|psi|kpa|mv|vacuum gauge|test light|"
    r"noid|scope|torque spec)\b|\b\d+(\.\d+)?\s?(v|psi|kpa|ohms?|mv|ma|a|%|in\.? ?hg|hz)\b", re.I)
OUTCOME_RE = re.compile(
    r"\b(fixed|solved|resolved|turned out|was the (culprit|problem|issue|cause)|that did it|problem gone|"
    r"update:|edit:|replaced (the|it|my)|swapped (the|out)|after replacing|haven'?t had|culprit|works now|"
    r"runs (great|fine|good) now|no more)\b", re.I)
# Obvious non-diagnostic threads (dropped only when they also show no trouble code and no test)
NONDIAG = {
    "sale": re.compile(r"\b(for sale|wts|wtb|fs:|selling (my|a)|price check|what'?s it worth|trade[- ]in|"
                       r"should i buy|buying (a|my)|just bought|new to me|delivery day|finally got|picked up my)\b", re.I),
    "insurance": re.compile(r"\b(insurance|claim|adjuster|totaled|at fault|hit and run)\b", re.I),
    "meme": re.compile(r"\b(meme|shitpost|humou?r|funny|circlejerk|appreciation post|spotted|photo ?shoot|"
                       r"wallpaper|rate my|car show)\b", re.I),
}


def rule_flags(title, segs, tags):
    head = segs[0]["t"]
    allt = "\n".join(s["t"] for s in segs)
    replies = len(segs) - 1
    f = {"reply": replies > 0,
         "code": bool(DTC_RE.search(allt)) or bool(DTC_RE.search(str(tags.get("dtcs") or ""))),
         "symptom": bool(SYMPTOM_RE.search(head)),
         "part": bool(PART_RE.search(allt)),
         "test": bool(TEST_RE.search(allt)),
         "outcome": bool(OUTCOME_RE.search(allt))}
    title_flair = f"{title or ''} {tags.get('flair') or ''}"
    for k, rx in NONDIAG.items():
        f[k] = bool(rx.search(title_flair if k != "insurance" else head))
    op_body = head[len(title or ""):].strip()
    f["photo_only"] = len(op_body) < 15 and not (f["code"] or SYMPTOM_RE.search(title or "") or TEST_RE.search(title or ""))
    signal = f["code"] or f["symptom"] or f["part"] or f["test"] or f["outcome"]
    nondiag = (f["sale"] or f["insurance"] or f["meme"] or f["photo_only"]) and not (f["code"] or f["test"])
    f["keep"] = bool(f["reply"] and signal and not nondiag)
    return f


def step_prefilter(make):
    w = work_db(make)
    for kind, name, path in sources(make):
        t0 = time.time()
        it = reddit_threads(path) if kind == "reddit" else forum_threads(path)
        stat, scores, rows = {}, {}, []
        w.execute("DELETE FROM prefilter_stat WHERE src=?", (name,))
        sql = ("INSERT INTO thread(src, tid, sub, url, title, created, score, n_replies, segs, tags, rules, card_keep) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(src, tid) DO UPDATE SET segs=excluded.segs, "
               "rules=excluded.rules, card_keep=excluded.card_keep, n_replies=excluded.n_replies")
        for tid, sub, url, title, created, score, op_body, cs, tags in it:
            segs = segments(title, op_body, cs)
            f = rule_flags(title, segs, tags)
            text = thread_text(segs)
            op_cs = [(s["t"],) for s in segs if s["k"] == "op_reply"]
            card = prefilter_score(title, text, op_cs) + 3 * bool(DTC_RE.search(str(tags.get("dtcs") or "")))
            card_keep = int(card >= 5)                       # extract_fixes.py's default --min-score
            st = stat.setdefault(sub or "", {})
            st["total"] = st.get("total", 0) + 1
            for k, v in f.items():
                st[k] = st.get(k, 0) + int(bool(v))
            st["card_keep"] = st.get("card_keep", 0) + card_keep
            st["both_keep"] = st.get("both_keep", 0) + int(card_keep and f["keep"])
            for c in cs:
                if not c[2]:
                    scores.setdefault(sub or "", []).append(c[1])
            if f["keep"]:
                if url and url.startswith("/r/"):
                    url = "https://www.reddit.com" + url
                rows.append((name, str(tid), sub, url, title, created, score, len(segs) - 1,
                             json.dumps(segs, ensure_ascii=False), json.dumps(tags), json.dumps(f), card_keep))
            if len(rows) >= 2000:
                w.executemany(sql, rows)
                w.commit()
                rows = []
        w.executemany(sql, rows)
        for sub, st in stat.items():
            w.executemany("INSERT OR REPLACE INTO prefilter_stat VALUES (?,?,?,?)", [(name, sub, k, v) for k, v in st.items()])
        for sub, sc in scores.items():
            sc.sort()
            w.execute("INSERT OR REPLACE INTO sub_score VALUES (?,?,?)", (name, sub, sc[int(len(sc) * 0.9)] if sc else 0))
        w.commit()
        tot = sum(s["total"] for s in stat.values())
        log(f"prefilter {make}/{name}: {tot} threads, kept {sum(s['keep'] for s in stat.values())} "
            f"(card filter {sum(s['card_keep'] for s in stat.values())}) in {time.time() - t0:.0f} s")
    prefilter_table(make)


def prefilter_table(make):
    w = work_db(make)
    rows = w.execute("SELECT src, sub, metric, n FROM prefilter_stat").fetchall()
    by = {}
    for src, sub, k, n in rows:
        by.setdefault((src, sub), {})[k] = n
    cols = ["total", "reply", "code", "symptom", "part", "test", "outcome", "sale", "insurance", "meme",
            "photo_only", "keep", "card_keep", "both_keep"]
    print(f"\n{make}: threads matching each rule (keep = reply AND any signal AND NOT obvious non-diagnostic)")
    print(f"{'source/sub':34s}" + "".join(f"{c[:9]:>10s}" for c in cols))
    tot = {c: 0 for c in cols}
    for (src, sub), st in sorted(by.items(), key=lambda x: -x[1].get("total", 0)):
        for c in cols:
            tot[c] += st.get(c, 0)
        if st.get("total", 0) >= 200:
            print(f"{(src + '/' + sub)[:34]:34s}" + "".join(f"{st.get(c, 0):>10d}" for c in cols))
    print(f"{'ALL':34s}" + "".join(f"{tot[c]:>10d}" for c in cols))
    if tot["total"]:
        print(f"kept {tot['keep']} ({100 * tot['keep'] / tot['total']:.0f}%) vs card filter {tot['card_keep']} "
              f"({100 * tot['card_keep'] / tot['total']:.0f}%); card-kept threads also kept here: "
              f"{tot['both_keep']} of {tot['card_keep']}")


# ============================================================================ pilot pick
def step_pilot(makes, n, seed):
    """Random threads spread over sources/subs: each (make, sub) gets a share proportional to sqrt(its kept
    count), so big subs dominate less and small forums still appear. No thread is chosen by hand."""
    rnd = random.Random(seed)
    groups = []
    for make in makes:
        w = work_db(make)
        w.execute("UPDATE thread SET pilot=0")
        w.commit()
        for src, sub, cnt in w.execute("SELECT src, sub, count(*) FROM thread GROUP BY src, sub"):
            groups.append((make, src, sub, cnt))
    if not groups:
        sys.exit("pilot: run prefilter for these makes first")
    wsum = sum(math.sqrt(c) for *_, c in groups)
    picked = 0
    for make, src, sub, cnt in groups:
        k = min(cnt, max(0, round(n * math.sqrt(cnt) / wsum)))
        if not k:
            continue
        w = work_db(make)
        tids = [r[0] for r in w.execute("SELECT tid FROM thread WHERE src=? AND sub IS ?", (src, sub))]
        chosen = rnd.sample(tids, k)
        w.executemany("UPDATE thread SET pilot=1 WHERE src=? AND tid=?", [(src, t) for t in chosen])
        w.commit()
        picked += k
    log(f"pilot: {picked} threads marked over {len(groups)} subs/forums in {', '.join(makes)} (seed {seed})")


# ============================================================================ pass 1: extraction
def _q(desc):
    return {"type": "string", "description": desc}


QS = {"seg": {"type": "integer"}, "quote": _q("exact words copied from that segment")}
SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "vehicle": {"type": "object", "properties": {
            "model": {"type": "string"}, "year": {"type": "string"}, "engine": {"type": "string"},
            "transmission": {"type": "string"}, "mileage": {"type": "string"}, **QS},
            "required": ["model", "year", "engine", "transmission", "mileage", "seg", "quote"]},
        "codes": {"type": "array", "items": {"type": "object", "properties": {"code": {"type": "string"}, **QS},
                                             "required": ["code", "seg", "quote"]}},
        "symptoms": {"type": "array", "items": {"type": "object", "properties": {
            "symptom": {"type": "string"}, "condition": {"type": "string"}, **QS},
            "required": ["symptom", "condition", "seg", "quote"]}},
        "tests": {"type": "array", "items": {"type": "object", "properties": {
            "test": {"type": "string"}, "tool": {"type": "string"}, "component": {"type": "string"},
            "result": {"type": "string"}, "status": {"type": "string", "enum": ["ran", "suggested"]}, **QS},
            "required": ["test", "tool", "component", "result", "status", "seg", "quote"]}},
        "ruled_out": {"type": "array", "items": {"type": "object", "properties": {
            "component": {"type": "string"}, "evidence": {"type": "string"}, **QS},
            "required": ["component", "evidence", "seg", "quote"]}},
        "suggestions": {"type": "array", "items": {"type": "object", "properties": {
            "component": {"type": "string"}, "reason": {"type": "string"}, **QS},
            "required": ["component", "reason", "seg", "quote"]}},
        "outcome": {"type": "object", "properties": {
            "fix": {"type": "string"}, "component": {"type": "string"},
            "status": {"type": "string", "enum": ["confirmed", "claimed", "none"]},
            "came_back": {"type": "string", "enum": ["yes", "no", "unknown"]}, **QS},
            "required": ["fix", "component", "status", "came_back", "seg", "quote"]},
        "chains": {"type": "array", "items": {"type": "object", "properties": {
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "step": {"type": "string"}, **QS}, "required": ["step", "seg", "quote"]}}},
            "required": ["steps"]}},
        "refs": {"type": "array", "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["tsb", "recall", "part_number", "cost", "labor_time"]},
            "value": {"type": "string"}, **QS}, "required": ["kind", "value", "seg", "quote"]}},
    },
    "required": ["relevant", "vehicle", "codes", "symptoms", "tests", "ruled_out", "suggestions", "outcome",
                 "chains", "refs"],
}

SYSTEM = """You read one thread from a car-repair subreddit or forum about {make} vehicles and record what the thread says, as JSON.
The thread is split into segments labeled [S<number> OP], [S<number> OP reply] or [S<number> reply, score N].

Rules:
- Record only what the thread itself says. Do not add your own diagnosis, knowledge or guesses. If the thread does not say it, leave the field empty ("" or []).
- Every item has "seg" (the segment number it comes from) and "quote": words copied exactly, character for character, from that segment (3 to 30 words, one continuous piece of text). Never reword a quote.
- relevant: true when the thread is about diagnosing or repairing a vehicle problem.
- vehicle: model, year, engine, transmission and mileage of the original poster's vehicle, as written. One quote that shows them.
- codes: each diagnostic trouble code written in the thread.
- symptoms: what the vehicle does, in the poster's words; condition = when it happens (cold or warm engine, idle or load, speed, weather) if stated.
- tests: each test or measurement someone ran or suggested: what test, which tool, which component, the reading or result as written (with units), status "ran" or "suggested".
- ruled_out: causes the thread eliminated, with the evidence that eliminated them.
- suggestions: causes that replies proposed, with the reason they gave.
- outcome: the final fix if one is stated. status "confirmed" only when the original poster says it fixed the problem; "claimed" when someone else states it; "none" when no fix is stated. came_back: did the problem return.
- chains: at most 3 short chains of reasoning that the thread actually shows (observation, then conclusion, then test, then result), one quoted step per link. Skip when the thread shows none.
- refs: TSB or recall numbers, part numbers, costs and labor times exactly as written.
- Keep text fields short (under 15 words)."""


class Ollama:
    def __init__(self, model, url=OLLAMA, num_ctx=8192, timeout=900):
        self.model, self.url, self.num_ctx, self.timeout = model, url.rstrip("/"), num_ctx, timeout

    def chat(self, system, user):
        body = {"model": self.model, "stream": False, "think": False, "format": SCHEMA, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": 2048},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read())
        return d["message"]["content"], d.get("prompt_eval_count") or 0, d.get("eval_count") or 0


# ---------------------------------------------------------------- quote checks (all Python)
STOP = set("the a an and or of to in on for with is are was were it this that at by from be as my car i me "
           "you your but not so if its it's has have had do did does just like also then than there their they "
           "we he she his her them what when which who will would could should can may might very really bad "
           "new old faulty failed failing broken replaced replace replacing".split())


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.%/ ]", " ", (s or "").lower())).strip()


def quote_in(quote, text):
    """Exact (normalised) substring; quotes of 8+ words may differ slightly (60% of their 4-word runs found),
    the same test as extract_fixes.evidence_ok."""
    q, t = norm(quote), norm(text)
    if not q:
        return False
    if q in t:
        return True
    words = q.split()
    if len(words) < 8:
        return False
    grams = [" ".join(words[i:i + 4]) for i in range(len(words) - 3)]
    return sum(g in t for g in grams) / len(grams) >= 0.6


def locate(item, segs, nsegs_norm):
    """Segment index that holds the item's quote: the one it names, else any other (Python fixes the label)."""
    q = item.get("quote") or ""
    s = item.get("seg")
    if isinstance(s, int) and 0 <= s < len(segs) and quote_in(q, segs[s]["t"]):
        return s, False
    for i, seg in enumerate(segs):
        if quote_in(q, seg["t"]):
            return i, True
    return None, False


def content_tokens(s):
    return {w[:4] for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 3 and w not in STOP}


def supported(value, quote):
    """The item's main words share at least one content word (4-letter stem) with its quote."""
    v = content_tokens(value)
    return not v or bool(v & content_tokens(quote))


def numbers_in(value, quote):
    nums = re.findall(r"\d+(?:\.\d+)?", value or "")
    qn = norm(quote).replace(",", "")
    return all(re.search(rf"(?<![0-9.]){re.escape(n)}(?![0-9])", qn) for n in nums)


def year_ok(year, quote):
    y = re.findall(r"\d{4}|\d{2}", year or "")
    if not y:
        return False
    q = norm(quote)
    return any(re.search(rf"(?<![0-9]){re.escape(v)}(?![0-9])", q) or
               (len(v) == 4 and re.search(rf"(?<![0-9])'?{v[2:]}(?![0-9])", q)) for v in y)


def validate(raw, segs, p90):
    """Checked record + counters. Items whose quote isn't in the thread, or whose main words aren't in their quote,
    are dropped. 'who' (op / reply) and reply score come from the segment, never from the model."""
    ck = {"items": 0, "kept": 0, "quote_fail": 0, "unsupported": 0, "seg_fixed": 0, "num_fail": 0}
    nsegs = [norm(s["t"]) for s in segs]

    def who(i):
        return "op" if segs[i]["k"] in ("op", "op_reply") else "reply"

    def take(item, main=None):
        ck["items"] += 1
        i, moved = locate(item, segs, nsegs)
        if i is None:
            ck["quote_fail"] += 1
            return None
        if main is not None and not supported(main, item["quote"]):
            ck["unsupported"] += 1
            return None
        ck["kept"] += 1
        ck["seg_fixed"] += moved
        out = dict(item, seg=i, who=who(i))
        if segs[i]["s"] is not None and who(i) == "reply":
            out["score"] = segs[i]["s"]
            out["high_score"] = int(p90 is not None and segs[i]["s"] >= max(p90, 2))
        return out

    rec = {"relevant": bool(raw.get("relevant"))}
    v = raw.get("vehicle") or {}
    veh = {}
    if any((v.get(k) or "").strip() for k in ("model", "year", "engine", "transmission", "mileage")):
        t = take(v)
        if t:
            q = t["quote"]
            for k in ("model", "engine", "transmission"):
                if (v.get(k) or "").strip():
                    if supported(v[k], q) and content_tokens(v[k]):
                        veh[k] = v[k].strip()
                    else:
                        ck["unsupported"] += 1
            if (v.get("year") or "").strip():
                if year_ok(v["year"], q):
                    veh["year"] = v["year"].strip()
                else:
                    ck["num_fail"] += 1
            if (v.get("mileage") or "").strip():
                if numbers_in(v["mileage"], q):
                    veh["mileage"] = v["mileage"].strip()
                else:
                    ck["num_fail"] += 1
            if veh:
                veh.update(seg=t["seg"], quote=q, who=t["who"])
    rec["vehicle"] = veh

    codes = {}
    for c in raw.get("codes") or []:
        code = (c.get("code") or "").strip().upper()
        if not DTC_RE.fullmatch(code):
            continue
        t = take(c)
        if t and code.lower() in norm(t["quote"]) and code not in codes:
            t["code"] = code
            t["op_had"] = int(any(code.lower() in nsegs[i] for i, s in enumerate(segs) if s["k"] != "reply"))
            codes[code] = t
        elif t:
            ck["unsupported"] += 1
            ck["kept"] -= 1
    rec["codes"] = list(codes.values())
    rec["symptoms"] = [x for x in (take(s, s.get("symptom")) for s in raw.get("symptoms") or []
                                   if (s.get("symptom") or "").strip()) if x]
    tests = []
    for s in raw.get("tests") or []:
        if not ((s.get("test") or "").strip() or (s.get("component") or "").strip()):
            continue
        t = take(s, f"{s.get('test', '')} {s.get('component', '')}")
        if t and t.get("result") and not numbers_in(t["result"], t["quote"]):
            ck["num_fail"] += 1
            t["result"] = ""
        if t:
            tests.append(t)
    rec["tests"] = tests
    rec["ruled_out"] = [x for x in (take(s, s.get("component")) for s in raw.get("ruled_out") or []
                                    if (s.get("component") or "").strip()) if x]
    rec["suggestions"] = [x for x in (take(s, s.get("component")) for s in raw.get("suggestions") or []
                                      if (s.get("component") or "").strip()) if x]
    o = raw.get("outcome") or {}
    rec["outcome"] = {}
    if o.get("status") in ("confirmed", "claimed") and ((o.get("fix") or "").strip() or (o.get("component") or "").strip()):
        t = take(o, f"{o.get('fix', '')} {o.get('component', '')}")
        if t:
            t["op_confirmed"] = int(t["status"] == "confirmed" and t["who"] == "op")
            if t["status"] == "confirmed" and t["who"] != "op":
                t["status"] = "claimed"
            rec["outcome"] = t
    chains = []
    for ch in (raw.get("chains") or [])[:3]:
        steps = [take(s, s.get("step")) for s in ch.get("steps") or [] if (s.get("step") or "").strip()]
        if len(steps) >= 2 and all(steps):
            chains.append({"steps": steps})
    rec["chains"] = chains
    refs = []
    for r in raw.get("refs") or []:
        t = take(r)
        if t and t.get("value") and (norm(t["value"]) in norm(t["quote"]) or
                                     (re.search(r"\d", t["value"]) and numbers_in(t["value"], t["quote"]))):
            refs.append(t)
        elif t:
            ck["unsupported"] += 1
            ck["kept"] -= 1
    rec["refs"] = refs
    rec["n_fields"] = sum(bool(rec[k]) for k in ("vehicle", "codes", "symptoms", "tests", "ruled_out",
                                                  "suggestions", "outcome", "chains", "refs"))
    return rec, ck


def gpu_free():
    r = subprocess.run(["bash", "-c", f"source {ROOT}/tools/gpu_guard.sh && gpu_free"], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def take_gpu(model):
    """gpu_guard.sh's lock for this process; unload other Ollama models so ours fits."""
    os.makedirs(ROOT, exist_ok=True)
    fd = open(os.path.join(ROOT, ".gpu.lock"), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("GPU lock taken by another step")
    ps = subprocess.run(["ollama", "ps"], capture_output=True, text=True).stdout.splitlines()[1:]
    for ln in ps:
        name = ln.split()[0] if ln.split() else ""
        if name and name != model:
            subprocess.run(["ollama", "stop", name])
            log(f"unloaded Ollama {name}")
    return fd


def stats_line(w, where, args, t_start, n_session):
    r = w.execute(f"SELECT count(*), sum(status='ok'), sum(status='json_fail'), sum(status='error'), "
                  f"sum(ms), sum(prompt_tokens), sum(eval_tokens) FROM extract e JOIN thread t USING (src, tid) "
                  f"WHERE {where}", args).fetchone()
    done, ok, jf, er, ms, pt, et = [x or 0 for x in r]
    items = kept = qf = uns = 0
    fields = 0
    for ck, rec in w.execute(f"SELECT checks, rec FROM extract e JOIN thread t USING (src, tid) "
                             f"WHERE status='ok' AND {where}", args):
        c = json.loads(ck)
        items += c["items"]
        kept += c["kept"]
        qf += c["quote_fail"]
        uns += c["unsupported"]
        fields += json.loads(rec)["n_fields"]
    todo = w.execute(f"SELECT count(*) FROM thread t WHERE {where} AND NOT EXISTS "
                     f"(SELECT 1 FROM extract e WHERE e.src=t.src AND e.tid=t.tid AND e.status='ok')", args).fetchone()[0]
    per_h = 3600 * done / (ms / 1000) if ms else 0
    return (f"{done} done ({ok} ok, json fail {100 * jf / max(done, 1):.1f}%, errors {er}); "
            f"quote fail {100 * qf / max(items, 1):.1f}% of {items} items, unsupported {100 * uns / max(items, 1):.1f}%; "
            f"{fields / max(ok, 1):.1f} fields/thread; {per_h:.0f} threads/h; "
            f"tokens in/out per thread {pt // max(done, 1)}/{et // max(done, 1)}; "
            f"{todo} left, ~{todo / per_h if per_h else 0:.0f} h")


def step_extract(make, a):
    if not a.force:
        ok, why = gpu_free()
        if not ok:
            log(f"extract {make}: {why}; not started (re-run later, or --force)")
            return
    lock = take_gpu(a.model)  # noqa: F841  (held until exit)
    w = work_db(make)
    where, args = ("t.pilot=1", []) if a.pilot else ("1", [])
    p90 = {(s, sub): p for s, sub, p in w.execute("SELECT src, sub, p90 FROM sub_score")}
    todo = w.execute(f"SELECT t.src, t.tid, t.sub, t.segs FROM thread t LEFT JOIN extract e "
                     f"ON e.src=t.src AND e.tid=t.tid WHERE {where} AND (e.status IS NULL OR "
                     f"(e.status!='ok' AND e.tries<3))", args).fetchall()
    if a.limit:
        todo = todo[:a.limit]
    llm = Ollama(a.model, num_ctx=a.num_ctx)
    system = SYSTEM.format(make=make.replace("_", " ").title())
    log(f"extract {make}: {len(todo)} threads to do with {a.model}{' (pilot)' if a.pilot else ''}")
    t0, n = time.time(), 0
    for src, tid, sub, segs_json in todo:
        segs = json.loads(segs_json)
        t1 = time.time()
        raw = rec = ck = None
        try:
            content, pt, et = llm.chat(system, thread_text(segs))
            try:
                raw = json.loads(content)
                rec, ck = validate(raw, segs, p90.get((src, sub)))
                status = "ok"
            except (ValueError, TypeError, AttributeError, KeyError) as e:
                status, ck = "json_fail", {"error": str(e)[:200]}
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            status, content, pt, et, ck = "error", None, 0, 0, {"error": str(e)[:200]}
            time.sleep(10)
        ms = int((time.time() - t1) * 1000)
        w.execute("INSERT INTO extract(src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(src, tid) DO UPDATE SET "
                  "status=excluded.status, raw=excluded.raw, rec=excluded.rec, checks=excluded.checks, "
                  "llm_model=excluded.llm_model, ms=excluded.ms, prompt_tokens=excluded.prompt_tokens, "
                  "eval_tokens=excluded.eval_tokens, at=excluded.at, tries=extract.tries+1",
                  (src, tid, status, content, json.dumps(rec) if rec else None, json.dumps(ck), a.model, ms, pt, et))
        w.commit()
        n += 1
        if n % (a.stats_every) == 0 or n == len(todo):
            log(f"extract {make} stats: " + stats_line(w, where, args, t0, n))
    log(f"extract {make}: finished {n} threads in {(time.time() - t0) / 3600:.1f} h")


# ============================================================================ pass 2: tables
COMM_SCHEMA = """
CREATE TABLE IF NOT EXISTS comm_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS comm_thread (
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT, title TEXT, created INTEGER,
  score INTEGER, n_replies INTEGER, quality TEXT NOT NULL, model TEXT, year INTEGER, engine TEXT,
  transmission TEXT, mileage TEXT, vehicle_quote TEXT, tag_models TEXT, tag_years TEXT, llm_model TEXT,
  UNIQUE (src, tid));
CREATE INDEX IF NOT EXISTS comm_thread_car ON comm_thread(model, year);
CREATE TABLE IF NOT EXISTS comm_code (thread_id INTEGER NOT NULL, code TEXT NOT NULL, op_had INTEGER, seg INTEGER,
  who TEXT, quote TEXT, PRIMARY KEY (code, thread_id));
CREATE TABLE IF NOT EXISTS comm_symptom (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, symptom TEXT,
  condition TEXT, seg INTEGER, who TEXT, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_test (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, test TEXT, tool TEXT,
  component TEXT, result TEXT, status TEXT, seg INTEGER, who TEXT, score INTEGER, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_ruled_out (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, component TEXT,
  evidence TEXT, seg INTEGER, who TEXT, score INTEGER, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_suggestion (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, component TEXT,
  reason TEXT, seg INTEGER, who TEXT, score INTEGER, high_score INTEGER, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_outcome (thread_id INTEGER PRIMARY KEY, fix TEXT, component TEXT, status TEXT,
  op_confirmed INTEGER, came_back TEXT, seg INTEGER, who TEXT, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_chain (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, steps TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS comm_ref (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, kind TEXT, value TEXT,
  seg INTEGER, quote TEXT);
CREATE TABLE IF NOT EXISTS comm_cause (   -- one row per (thread, cause, role): what counts are made of
  thread_id INTEGER NOT NULL, cause_key TEXT NOT NULL, label TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (cause_key, role, thread_id));
CREATE TABLE IF NOT EXISTS comm_count (   -- per code and model: separate threads per cause (Python-computed)
  code TEXT NOT NULL, model TEXT, year_min INTEGER, year_max INTEGER, cause_key TEXT NOT NULL, label TEXT NOT NULL,
  threads INTEGER NOT NULL, op_confirmed INTEGER NOT NULL, fix_claimed INTEGER NOT NULL, suggested INTEGER NOT NULL,
  ruled_out INTEGER NOT NULL, tested INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS comm_count_i ON comm_count(code, model);
CREATE TABLE IF NOT EXISTS comm_link (    -- community item -> manual section in kb/<make>.db
  kind TEXT NOT NULL, row_id INTEGER NOT NULL, section_id INTEGER NOT NULL, rank INTEGER NOT NULL, method TEXT NOT NULL,
  PRIMARY KEY (kind, row_id, rank));
CREATE TABLE IF NOT EXISTS comm_doc (id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, kind TEXT NOT NULL,
  ref_id INTEGER, text TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS comm_doc_fts USING fts5(text, content='comm_doc', content_rowid='id',
  tokenize="porter unicode61 remove_diacritics 2");
"""
COMM_TABLES = ["comm_meta", "comm_thread", "comm_code", "comm_symptom", "comm_test", "comm_ruled_out",
               "comm_suggestion", "comm_outcome", "comm_chain", "comm_ref", "comm_cause", "comm_count",
               "comm_link", "comm_doc"]
CAUSE_DROP = STOP | set("issue issues problem problems part parts possibly probably likely maybe check".split())


def cause_key(s):
    """Wording-only key: lower case, no punctuation, filler words out, plural s off. No cause vocabulary."""
    ws = []
    for w_ in re.findall(r"[a-z0-9/]+", (s or "").lower()):
        if w_ in CAUSE_DROP:
            continue
        if len(w_) > 3 and w_.endswith("s") and not w_.endswith("ss"):
            w_ = w_[:-1]
        ws.append(w_)
    return " ".join(ws)


def cdb_path(make, pilot=False):
    """The pilot gets its own file so it never replaces a full build."""
    return os.path.join(OUT, f"{make}.community{'.pilot' if pilot else ''}.db")


def _year(s):
    m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", s or "")
    if m:
        return int(m.group(1))
    m = re.search(r"(?<![0-9])'?(\d{2})(?![0-9])", s or "")
    if m:
        y = int(m.group(1))
        return 2000 + y if y <= 40 else 1900 + y
    return None


def step_tables(make, a):
    w = work_db(make)
    os.makedirs(OUT, exist_ok=True)
    tmp = cdb_path(make, a.pilot) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript(COMM_SCHEMA)
    where = "t.pilot=1" if a.pilot else "1"
    rows = w.execute(f"SELECT t.src, t.tid, t.sub, t.url, t.title, t.created, t.score, t.n_replies, t.tags, e.rec, "
                     f"e.llm_model FROM thread t JOIN extract e ON e.src=t.src AND e.tid=t.tid "
                     f"WHERE e.status='ok' AND {where}").fetchall()
    n = 0
    for src, tid, sub, url, title, created, score, nrep, tags, rec, llm_model in rows:
        r = json.loads(rec)
        if not r.get("relevant") or not r["n_fields"]:
            continue
        v, o, tg = r["vehicle"], r["outcome"], json.loads(tags or "{}")
        if o.get("op_confirmed"):
            quality = "op_confirmed"
        elif any(s.get("high_score") for s in r["suggestions"]):
            quality = "high_score_reply"
        elif nrep == 1:
            quality = "single_reply"
        else:
            quality = "discussion"
        cur = c.execute("INSERT INTO comm_thread(src, tid, sub, url, title, created, score, n_replies, quality, model, "
                        "year, engine, transmission, mileage, vehicle_quote, tag_models, tag_years, llm_model) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (src, tid, sub, url, title, created, score, nrep, quality, v.get("model"), _year(v.get("year")),
                         v.get("engine"), v.get("transmission"), v.get("mileage"), v.get("quote"),
                         tg.get("models"), tg.get("years"), llm_model))
        th = cur.lastrowid
        n += 1
        for x in r["codes"]:
            c.execute("INSERT OR IGNORE INTO comm_code VALUES (?,?,?,?,?,?)",
                      (th, x["code"], x["op_had"], x["seg"], x["who"], x["quote"]))
        for x in r["symptoms"]:
            c.execute("INSERT INTO comm_symptom(thread_id, symptom, condition, seg, who, quote) VALUES (?,?,?,?,?,?)",
                      (th, x["symptom"], x.get("condition"), x["seg"], x["who"], x["quote"]))
        roles = []
        for x in r["tests"]:
            c.execute("INSERT INTO comm_test(thread_id, test, tool, component, result, status, seg, who, score, quote) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?)", (th, x["test"], x.get("tool"), x.get("component"), x.get("result"),
                                                       x.get("status"), x["seg"], x["who"], x.get("score"), x["quote"]))
            roles.append((x.get("component"), "tested"))
        for x in r["ruled_out"]:
            c.execute("INSERT INTO comm_ruled_out(thread_id, component, evidence, seg, who, score, quote) "
                      "VALUES (?,?,?,?,?,?,?)", (th, x["component"], x.get("evidence"), x["seg"], x["who"],
                                                 x.get("score"), x["quote"]))
            roles.append((x["component"], "ruled_out"))
        for x in r["suggestions"]:
            c.execute("INSERT INTO comm_suggestion(thread_id, component, reason, seg, who, score, high_score, quote) "
                      "VALUES (?,?,?,?,?,?,?,?)", (th, x["component"], x.get("reason"), x["seg"], x["who"],
                                                   x.get("score"), x.get("high_score"), x["quote"]))
            roles.append((x["component"], "suggested"))
        if o:
            c.execute("INSERT INTO comm_outcome VALUES (?,?,?,?,?,?,?,?,?)",
                      (th, o.get("fix"), o.get("component"), o["status"], o["op_confirmed"], o.get("came_back"),
                       o["seg"], o["who"], o["quote"]))
            roles.append((o.get("component") or o.get("fix"), "fix_confirmed" if o["op_confirmed"] else "fix_claimed"))
        for ch in r["chains"]:
            c.execute("INSERT INTO comm_chain(thread_id, steps) VALUES (?,?)",
                      (th, json.dumps([{k: s[k] for k in ("step", "seg", "who", "quote")} for s in ch["steps"]])))
        for x in r["refs"]:
            c.execute("INSERT INTO comm_ref(thread_id, kind, value, seg, quote) VALUES (?,?,?,?,?)",
                      (th, x["kind"], x["value"], x["seg"], x["quote"]))
        for label, role in roles:
            k = cause_key(label)
            if k:
                c.execute("INSERT OR IGNORE INTO comm_cause VALUES (?,?,?,?)", (th, k, label.strip(), role))
    c.commit()
    counts(c)
    docs(c)
    links(c, make, a)
    c.execute("INSERT OR REPLACE INTO comm_meta VALUES ('built', datetime('now'))")
    c.execute("INSERT OR REPLACE INTO comm_meta VALUES ('make', ?)", (make,))
    c.commit()
    c.close()
    old = cdb_path(make, a.pilot)
    os.replace(tmp, old)
    log(f"tables {make}: {n} threads -> {old}")


def counts(c):
    """Per code and model: separate threads naming each cause, by role. Labels = the most common wording."""
    rows = c.execute("SELECT k.code, t.model, t.year, a.cause_key, a.label, a.role, a.thread_id FROM comm_code k "
                     "JOIN comm_thread t ON t.id=k.thread_id JOIN comm_cause a ON a.thread_id=k.thread_id "
                     "WHERE k.op_had=1").fetchall()
    agg = {}
    for code, model, year, key, label, role, th in rows:
        for m in (model, None):
            e = agg.setdefault((code, (m or "").strip().title() or None, key),
                               {"threads": set(), "labels": {}, "years": [], **{r: set() for r in
                                ("fix_confirmed", "fix_claimed", "suggested", "ruled_out", "tested")}})
            e["threads"].add(th)
            e[role].add(th)
            e["labels"][label] = e["labels"].get(label, 0) + 1
            if year:
                e["years"].append(year)
    c.execute("DELETE FROM comm_count")
    for (code, model, key), e in agg.items():
        c.execute("INSERT INTO comm_count VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (code, model, min(e["years"]) if e["years"] else None, max(e["years"]) if e["years"] else None,
                   key, max(e["labels"], key=e["labels"].get), len(e["threads"]), len(e["fix_confirmed"]),
                   len(e["fix_claimed"]), len(e["suggested"]), len(e["ruled_out"]), len(e["tested"])))


def docs(c):
    """Texts for search (FTS now, vectors in `embed`), built in Python from checked fields only."""
    c.execute("DELETE FROM comm_doc")
    for th, model, year, engine, title in c.execute(
            "SELECT id, model, year, engine, title FROM comm_thread").fetchall():
        car = " ".join(str(x) for x in (year, model, engine) if x)
        sy = "; ".join(r[0] for r in c.execute("SELECT symptom FROM comm_symptom WHERE thread_id=?", (th,)))
        cd = " ".join(r[0] for r in c.execute("SELECT code FROM comm_code WHERE thread_id=?", (th,)))
        o = c.execute("SELECT fix, component FROM comm_outcome WHERE thread_id=?", (th,)).fetchone()
        text = f"{car}: {title or ''}. Codes: {cd}. Symptoms: {sy}." + (f" Fix: {o[0] or o[1]}." if o else "")
        c.execute("INSERT INTO comm_doc(thread_id, kind, ref_id, text) VALUES (?,?,?,?)", (th, "thread", th, text))
    for i, th, sy, cond in c.execute("SELECT id, thread_id, symptom, condition FROM comm_symptom").fetchall():
        c.execute("INSERT INTO comm_doc(thread_id, kind, ref_id, text) VALUES (?,?,?,?)",
                  (th, "symptom", i, f"{sy}" + (f" ({cond})" if cond else "")))
    for i, th, test, comp, res in c.execute("SELECT id, thread_id, test, component, result FROM comm_test").fetchall():
        c.execute("INSERT INTO comm_doc(thread_id, kind, ref_id, text) VALUES (?,?,?,?)",
                  (th, "test", i, f"{test} on {comp}" + (f": {res}" if res else "")))
    for i, th, steps in c.execute("SELECT id, thread_id, steps FROM comm_chain").fetchall():
        c.execute("INSERT INTO comm_doc(thread_id, kind, ref_id, text) VALUES (?,?,?,?)",
                  (th, "chain", i, " -> ".join(s["step"] for s in json.loads(steps))))
    c.execute("INSERT INTO comm_doc_fts(comm_doc_fts) VALUES ('rebuild')")


def links(c, make, a):
    """Tests, ruled-out and suggested components and fixes -> manual sections of this make, through the app's
    own retriever (FTS, plus vectors with --vec), scoped to the thread's model/year when the DB knows it."""
    master = os.path.join(KB, f"{make}.db")
    if not os.path.isfile(master):
        log(f"tables {make}: no {master}, manual links skipped")
        return
    from kb_retriever import Retriever
    embed = None
    if a.vec:
        from embedder import QueryEmbedder
        embed = QueryEmbedder()
    rets = {}
    items = []
    for kind, sql in (("test", "SELECT x.id, coalesce(x.test,'') || ' ' || coalesce(x.component,''), t.model, t.year "
                               "FROM comm_test x JOIN comm_thread t ON t.id=x.thread_id"),
                      ("ruled_out", "SELECT x.id, x.component, t.model, t.year FROM comm_ruled_out x "
                                    "JOIN comm_thread t ON t.id=x.thread_id"),
                      ("suggestion", "SELECT x.id, x.component, t.model, t.year FROM comm_suggestion x "
                                     "JOIN comm_thread t ON t.id=x.thread_id"),
                      ("outcome", "SELECT x.thread_id, coalesce(x.component, x.fix), t.model, t.year FROM comm_outcome x "
                                  "JOIN comm_thread t ON t.id=x.thread_id")):
        items += [(kind, *r) for r in c.execute(sql).fetchall()]
    cache, t0 = {}, time.time()
    for kind, rid, text, model, year in items:
        text = (text or "").strip()
        if not text:
            continue
        key = (cause_key(text), (model or "").title(), year)
        if key not in cache:
            sk = ((model or "").title() or None, year)
            if sk not in rets:
                rets[sk] = Retriever(master, model=sk[0], year=sk[1], embed=embed)
            try:
                cache[key] = rets[sk].search_ids(text, k=3)
            except sqlite3.Error:
                cache[key] = []
        for rank, sid in enumerate(cache[key]):
            c.execute("INSERT OR REPLACE INTO comm_link VALUES (?,?,?,?,?)",
                      (kind, rid, sid, rank, "fts+vec" if embed else "fts"))
    log(f"tables {make}: {len(items)} items linked ({len(cache)} distinct searches) in {time.time() - t0:.0f} s")


# ============================================================================ embed / merge
def step_embed(make, device="auto"):
    c = sqlite3.connect(cdb_path(make))
    rows = c.execute("SELECT id, text FROM comm_doc").fetchall()
    if not rows:
        log(f"embed {make}: no docs")
        return
    if device == "auto":
        ok, why = gpu_free()
        device = "cuda" if ok else "cpu"
        log(f"embed {make}: {'GPU free' if ok else why + '; using the CPU'}")
    import numpy as np
    import sqlite_vec
    from sentence_transformers import SentenceTransformer
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device=device)
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.execute("DROP TABLE IF EXISTS comm_doc_vec")
    c.execute("CREATE VIRTUAL TABLE comm_doc_vec USING vec0(id INTEGER PRIMARY KEY, embedding int8[1024])")
    t0 = time.time()
    for b in range(0, len(rows), 64):
        chunk = rows[b:b + 64]
        v = model.encode([t for _, t in chunk], convert_to_numpy=True, normalize_embeddings=True)[:, :1024]
        q = np.clip(np.round(v * 127), -127, 127).astype(np.int8)
        c.executemany("INSERT INTO comm_doc_vec(id, embedding) VALUES (?, vec_int8(?))",
                      [(i, vec.tobytes()) for (i, _), vec in zip(chunk, q)])
    c.commit()
    log(f"embed {make}: {len(rows)} docs on {device} in {time.time() - t0:.0f} s")


def build_in_progress(make):
    if make == "subaru" or os.path.exists(os.path.join(KB, f".{make}.done")):
        return False
    return subprocess.run(["pgrep", "-f", "build_charm_all.sh"], capture_output=True).returncode == 0


def step_merge(make):
    if build_in_progress(make):
        log(f"merge {make}: SKIPPED, the CHARM build hasn't finished kb/{make}.db yet")
        return
    src, dst = cdb_path(make), os.path.join(KB, f"{make}.db")
    if not os.path.isfile(src) or not os.path.isfile(dst):
        sys.exit(f"merge needs {src} and {dst}")
    m = sqlite3.connect(dst, timeout=120)
    try:
        import sqlite_vec
        m.enable_load_extension(True)
        sqlite_vec.load(m)
        vec = True
    except Exception:
        vec = False
    have = [r[0] for r in m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'comm\\_%' "
                                    "ESCAPE '\\' AND name NOT LIKE 'comm\\_doc\\_fts\\_%' ESCAPE '\\' "
                                    "AND name NOT LIKE 'comm\\_doc\\_vec\\_%' ESCAPE '\\'")]
    if have:
        os.makedirs(os.path.join(OUT, "backup"), exist_ok=True)
        bk = os.path.join(OUT, "backup", f"{make}.comm.{time.strftime('%Y%m%d_%H%M')}.db")
        m.execute("ATTACH ? AS bk", (bk,))
        for t in have:
            if t in COMM_TABLES:
                m.execute(f"CREATE TABLE bk.{t} AS SELECT * FROM main.{t}")
        m.commit()
        m.execute("DETACH bk")
        log(f"merge {make}: previous comm_* copy saved to {bk}")
    m.execute("ATTACH ? AS c", (src,))
    with m:
        for t in ("comm_doc_fts", "comm_doc_vec"):
            m.execute(f"DROP TABLE IF EXISTS main.{t}")
        for t in have:
            m.execute(f"DROP TABLE IF EXISTS main.{t}")
        for t in COMM_TABLES:
            sql = m.execute("SELECT sql FROM c.sqlite_master WHERE name=?", (t,)).fetchone()[0]
            m.execute(sql)
            m.execute(f"INSERT INTO main.{t} SELECT * FROM c.{t}")
        for (sql,) in m.execute("SELECT sql FROM c.sqlite_master WHERE type='index' AND name LIKE 'comm%' "
                                "AND sql IS NOT NULL").fetchall():
            m.execute(sql)
        m.execute("CREATE VIRTUAL TABLE main.comm_doc_fts USING fts5(text, content='comm_doc', content_rowid='id', "
                  "tokenize=\"porter unicode61 remove_diacritics 2\")")
        m.execute("INSERT INTO main.comm_doc_fts(comm_doc_fts) VALUES ('rebuild')")
        if vec and m.execute("SELECT 1 FROM c.sqlite_master WHERE name='comm_doc_vec'").fetchone():
            m.execute("CREATE VIRTUAL TABLE main.comm_doc_vec USING vec0(id INTEGER PRIMARY KEY, embedding int8[1024])")
            m.execute("INSERT INTO main.comm_doc_vec(id, embedding) SELECT id, embedding FROM c.comm_doc_vec")
    m.execute("DETACH c")
    n = m.execute("SELECT count(*) FROM comm_thread").fetchone()[0]
    m.close()
    log(f"merge {make}: comm_* tables replaced in {dst} ({n} threads)")


# ============================================================================ raw threads: every kept thread, searchable
# Step 2 of the community data (10-31): the LLM extraction reads only some threads, so every thread
# pass 0 kept also goes into kb/<make>.threads.db as plain text with FTS and vectors. No model reads them at build
# time and nothing is summarised: the app finds the threads and shows/quotes them with their link. A separate file
# so the make DB stays as it is and the deck can skip it.
RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS raw_thread (
  id INTEGER PRIMARY KEY, src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT, title TEXT, created INTEGER,
  score INTEGER, n_replies INTEGER, tag_models TEXT, tag_years TEXT, tag_dtcs TEXT, card_keep INTEGER,
  text TEXT NOT NULL, UNIQUE (src, tid));
CREATE VIRTUAL TABLE IF NOT EXISTS raw_thread_fts USING fts5(title, text, content='raw_thread', content_rowid='id',
  tokenize="porter unicode61 remove_diacritics 2");
"""
RAW_EMBED_TOKENS = 512      # title + OP + first replies; the full text stays in FTS and is what the app shows


def raw_path(make):
    return os.path.join(KB, f"{make}.threads.db")


def _tag(v):
    if v is None:
        return None
    return ",".join(str(x) for x in v) if isinstance(v, (list, tuple)) else str(v)


def step_raw(make):
    w = work_db(make)
    if not w.execute("SELECT 1 FROM thread LIMIT 1").fetchone():
        log(f"raw {make}: no threads in the work DB; run prefilter first")
        return
    r = sqlite3.connect(raw_path(make), timeout=60)
    r.executescript(RAW_SCHEMA)
    have = {(s, t) for s, t in r.execute("SELECT src, tid FROM raw_thread")}
    sql = ("INSERT INTO raw_thread(src, tid, sub, url, title, created, score, n_replies, tag_models, tag_years, "
           "tag_dtcs, card_keep, text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    t0, n, rows = time.time(), 0, []
    for src, tid, sub, url, title, created, score, nrep, segs, tags, card in w.execute(
            "SELECT src, tid, sub, url, title, created, score, n_replies, segs, tags, card_keep FROM thread"):
        if (src, tid) in have:
            continue
        tg = json.loads(tags or "{}")
        rows.append((src, tid, sub, url, title, created, score, nrep, _tag(tg.get("models")), _tag(tg.get("years")),
                     _tag(tg.get("dtcs")), card, thread_text(json.loads(segs))))
        if len(rows) >= 5000:
            r.executemany(sql, rows)
            r.commit()
            n += len(rows)
            rows = []
    r.executemany(sql, rows)
    n += len(rows)
    r.execute("INSERT INTO raw_thread_fts(raw_thread_fts) VALUES ('rebuild')")
    r.execute("INSERT OR REPLACE INTO raw_meta VALUES ('make', ?)", (make,))
    r.commit()
    total = r.execute("SELECT count(*) FROM raw_thread").fetchone()[0]
    log(f"raw {make}: {n} new threads, {total} in {raw_path(make)} ({time.time() - t0:.0f} s)")


def step_rawembed(make, device="auto"):
    path = raw_path(make)
    if not os.path.isfile(path):
        log(f"rawembed {make}: no {path}; run the raw step first")
        return
    import sqlite_vec
    r = sqlite3.connect(path, timeout=60)
    r.enable_load_extension(True)
    sqlite_vec.load(r)
    r.execute("CREATE VIRTUAL TABLE IF NOT EXISTS raw_thread_vec USING vec0(id INTEGER PRIMARY KEY, "
              "embedding int8[1024])")
    done = {i for (i,) in r.execute("SELECT id FROM raw_thread_vec")}
    rows = [(i, t) for i, t in r.execute("SELECT id, coalesce(title,'') || '\n' || text FROM raw_thread")
            if i not in done]
    if not rows:
        log(f"rawembed {make}: all {len(done)} threads already have vectors")
        return
    lock = None
    if device == "auto":
        ok, why = gpu_free()
        device = "cuda" if ok else "cpu"
        log(f"rawembed {make}: {'GPU free' if ok else why + '; using the CPU'}")
    if device.startswith("cuda"):
        lock = take_gpu(None)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from embedder import DocEncoder
    enc = DocEncoder(device=device, max_tokens=RAW_EMBED_TOKENS, log=lambda m, **k: log(m))
    t0, n = time.time(), 0
    for ids, vecs in enc.encode_rows(rows):     # one commit per chunk: Ctrl-C and rerun resumes
        r.executemany("INSERT INTO raw_thread_vec(id, embedding) VALUES (?, vec_int8(?))",
                      [(i, v.tobytes()) for i, v in zip(ids, vecs)])
        r.commit()
        n += len(ids)
        el = time.time() - t0
        log(f"rawembed {make}: {n}/{len(rows)} threads, {n / el:.0f}/s, "
            f"ETA {(len(rows) - n) / max(n / el, 1e-9) / 3600:.1f} h")
    log(f"rawembed {make}: {n} threads on {device} in {(time.time() - t0) / 60:.0f} min "
        f"({enc.truncated} cut at {RAW_EMBED_TOKENS} tokens)")
    if lock:
        lock.close()


# ============================================================================ report / sample
def step_report(make, a):
    w = work_db(make)
    where, args = ("t.pilot=1", []) if a.pilot else ("1", [])
    print(f"{make}{' pilot' if a.pilot else ''}: " + stats_line(w, where, args, 0, 0))
    kept = w.execute("SELECT count(*), sum(card_keep) FROM thread").fetchone()
    pk = w.execute(f"SELECT count(*), sum(card_keep) FROM thread t WHERE {where}", args).fetchone()
    print(f"  kept by pass 0: {kept[0]} threads (card filter would keep {kept[1] or 0} of them); "
          f"this set: {pk[0]}, card filter keeps {pk[1] or 0}")
    rel = w.execute(f"SELECT rec FROM extract e JOIN thread t USING (src, tid) WHERE status='ok' AND {where}", args)
    recs = [json.loads(r[0]) for r in rel]
    if recs:
        k = ("vehicle", "codes", "symptoms", "tests", "ruled_out", "suggestions", "outcome", "chains", "refs")
        print(f"  relevant {sum(r['relevant'] for r in recs)} of {len(recs)}; share with field filled: " +
              ", ".join(f"{f} {100 * sum(bool(r[f]) for r in recs) / len(recs):.0f}%" for f in k))
        print(f"  outcome confirmed by OP: {sum(bool(r['outcome'].get('op_confirmed')) for r in recs)}")


def step_sample(make, a):
    w = work_db(make)
    where = "t.pilot=1" if a.pilot else "1"
    rows = w.execute(f"SELECT t.src, t.tid, t.url, t.segs, e.rec, e.checks FROM thread t JOIN extract e "
                     f"ON e.src=t.src AND e.tid=t.tid WHERE e.status='ok' AND {where}").fetchall()
    rnd = random.Random(a.seed)
    pick = rnd.sample(rows, min(a.n, len(rows)))
    os.makedirs(a.out, exist_ok=True)
    for i, (src, tid, url, segs, rec, ck) in enumerate(pick, 1):
        with open(os.path.join(a.out, f"{i:02d}_{make}_{src}_{tid}.txt"), "w") as f:
            f.write(f"{src} {tid} {url}\nchecks {ck}\n\n=== RECORD\n{json.dumps(json.loads(rec), indent=1)}\n\n"
                    f"=== THREAD\n{thread_text(json.loads(segs))}\n")
    print(f"sample: {len(pick)} files in {a.out}")


def main():
    global LOG, MAX_CHARS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+", choices=["prefilter", "pilot", "extract", "tables", "embed", "merge",
                                                  "report", "sample", "raw", "rawembed"])
    ap.add_argument("--make")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--makes", default="subaru,chevrolet,toyota,ford", help="pilot: makes to draw from")
    ap.add_argument("--n", type=int, default=300, help="pilot size / sample size")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--pilot", action="store_true", help="extract/tables/report/sample: pilot threads only")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="Ollama tag")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--stats-every", type=int, default=2000)
    ap.add_argument("--force", action="store_true", help="extract: skip the GPU-busy check")
    ap.add_argument("--vec", action="store_true", help="tables: add vector search to the manual links (CPU embedder)")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--out", default=os.path.join(OUT, "sample"))
    a = ap.parse_args()
    if a.seed is None:
        a.seed = int(time.time())
    if "pilot" in a.steps:
        makes = a.makes.split(",")
        LOG = os.path.join(ROOT, "logs", "community_pilot.log")
        step_pilot(makes, a.n, a.seed)
        return
    makes = makes_with_data() if a.all else ([a.make] if a.make else [])
    if not makes and a.pilot:
        makes = a.makes.split(",")
    if not makes:
        sys.exit("--make <db name>, --all, or --pilot (uses --makes)")
    for make in makes:
        LOG = os.path.join(ROOT, "logs", f"community_{'pilot' if a.pilot else make}.log")
        for s in a.steps:
            {"prefilter": lambda: step_prefilter(make), "extract": lambda: step_extract(make, a),
             "tables": lambda: step_tables(make, a), "embed": lambda: step_embed(make, a.device),
             "merge": lambda: step_merge(make), "report": lambda: step_report(make, a),
             "sample": lambda: step_sample(make, a), "raw": lambda: step_raw(make),
             "rawembed": lambda: step_rawembed(make, a.device)}[s]()


if __name__ == "__main__":
    main()
