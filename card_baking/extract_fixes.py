#!/usr/bin/env python3
"""
Job 1 of card baking: read forum threads and pull out "car, code/symptom, what fixed it, was it confirmed".

Steps (each resumable, all state in the work DB):
  inspect   print the tables and columns the source DBs have, and how they were mapped
  filter    copy candidate threads into the work DB (cheap keyword filter, no LLM)
  run       send candidates to llama-server (small model, -np slots) and store one JSON extract each
  stats     what was extracted so far
  export    write confirmed fixes into subaru_diag.db forum_fix (then run the kb builder's `diag` step)

Typical:
  python3 extract_fixes.py filter --reddit ~/subaru_cyberdeck/raw/reddit/reddit_subaru.db
  python3 extract_fixes.py run --limit 100          # timing sample, prints a projected total
  python3 extract_fixes.py run --parallel 4         # the rest (match llama-server -np)
  python3 extract_fixes.py export --diag-db ~/subaru_cyberdeck/subaru_diag.db

Standard library only.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sqlite3
import sys
import time

from llm import LLM

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORK = os.path.expanduser("~/subaru_cyberdeck/kb/bake_work.db")

DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
SYMPTOM_RE = re.compile(
    r"\b(misfire|cel|check engine|rough idle|stall|hesitat|overheat|head gasket|knock|tick|rattle|whine|"
    r"grind|clunk|squeal|leak|smoke|smell|vibrat|shudder|no start|won'?t start|crank|surge|bog|"
    r"lean|rich|p0\d\d\d|code|limp|flash|slip|hard shift|abs light|tcs|cruise light|stutter)", re.I)
FIX_RE = re.compile(
    r"\b(fixed|solved|resolved|turned out|was the (culprit|problem|issue|cause)|that did it|problem gone|"
    r"no more (codes?|misfire|cel)|update:|edit:|replaced the|swapped (the|out)|after replacing|"
    r"cleared (it|the code)|haven'?t had|culprit)", re.I)

SUB_MODEL = {  # subreddit -> model, when the post doesn't say
    "subaruforester": "Forester", "subaruoutback": "Outback", "subarulegacy": "Legacy",
    "subaruimpreza": "Impreza", "subarubaja": "Baja", "crosstrek": "Crosstrek", "wrx": "WRX",
    "subaruascent": "Ascent", "subarutribeca": "Tribeca", "brz": "BRZ", "wrxsti": "WRX STI",
}
MODELS = ["Forester", "Outback", "Legacy", "Impreza", "WRX", "WRX STI", "Crosstrek", "XV", "Baja",
          "Tribeca", "Ascent", "BRZ", "SVX", "Justy", "Loyale", "GL", "Other", "Unknown"]

# Cause buckets so fixes can be counted with SQL. 'part' keeps the specific wording.
CATEGORIES = [
    "spark plugs", "ignition coil", "spark plug wires", "fuel injector", "fuel pump", "fuel filter",
    "fuel pressure regulator", "vacuum leak", "intake gasket", "throttle body", "idle air control valve",
    "mass air flow sensor", "front o2/air-fuel sensor", "rear o2 sensor", "catalytic converter",
    "exhaust manifold/header gasket", "exhaust leak other", "egr valve/passages", "pcv valve/hose",
    "purge valve/evap", "gas cap", "head gasket", "valve adjustment", "timing belt/tensioner/idler",
    "camshaft position sensor", "crankshaft position sensor", "knock sensor", "coolant temp sensor",
    "thermostat", "water pump", "valve cover gasket/plug tube seals", "radiator/hoses", "battery/charging", "wiring/connector/ground", "ecu",
    "low compression/engine internal", "wheel bearing", "cv axle", "brakes", "suspension", "transmission",
    "clutch", "differential/viscous coupling", "torque bind/tires", "abs sensor", "body/electrical other",
    "other", "unknown",
]

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "model": {"type": "string", "enum": MODELS},
        "year": {"type": ["integer", "null"]},
        "engine": {"type": ["string", "null"]},
        "codes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "symptoms": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "category": {"type": "string", "enum": CATEGORIES},
        "part": {"type": ["string", "null"]},
        "fix": {"type": ["string", "null"]},
        "confirmed": {"type": "string", "enum": ["op_confirmed", "other_confirmed", "suggested_only", "unresolved"]},
        "evidence": {"type": ["string", "null"]},
    },
    "required": ["relevant", "model", "year", "engine", "codes", "symptoms", "category", "part", "fix",
                 "confirmed", "evidence"],
}

SYSTEM = """You read one car-forum thread and record what fixed the problem. Rules:
- relevant=false if it is not about diagnosing or repairing a fault on a Subaru (buying advice, mods, photos, jokes).
- model/year/engine: only what the thread says (engine codes like EJ251, EJ253, EJ255, EJ257, FB25, FA20). Unknown if not stated.
- codes: trouble codes mentioned for this car, written as letter + 4 digits.
- fix: what actually solved it, in one short sentence. category: the closest bucket. part: the specific part or job.
- confirmed: op_confirmed if the original poster ([OP]) says the fix worked; other_confirmed if another person with the same problem says so; suggested_only if people only suggested fixes; unresolved otherwise.
- evidence: copy, word for word, the sentence from the thread that shows the fix (max 200 characters). null if none.
- Never guess. If the thread does not say, use null, Unknown, unresolved."""


YEAR_MODEL_RE = (r"(?:^|\s)'?([890]\d|1[0-9])\s+(?:subaru\s+)?(forester|outback|legacy|impreza|wrx|sti|baja|"
                 r"crosstrek|tribeca|xv)\b")


def configure_make(make, models):
    """Any make other than Subaru: the prompt, the model list and the title year pattern name that make
    (models: the make's model names, e.g. from its make_pipeline profile). Subaru keeps the lists above."""
    global MODELS, SUB_MODEL, SYSTEM, YEAR_MODEL_RE
    MODELS = list(dict.fromkeys(m for m in models if m)) + ["Other", "Unknown"]
    SUB_MODEL = {}
    EXTRACT_SCHEMA["properties"]["model"]["enum"] = MODELS
    SYSTEM = (SYSTEM.replace("on a Subaru", f"on {'an' if make[:1].upper() in 'AEIOU' else 'a'} {make}")
              .replace("(engine codes like EJ251, EJ253, EJ255, EJ257, FB25, FA20)",
                       "(engine code or size as written, e.g. 5.3L, 2JZ, LS1)"))
    alts = "|".join(re.escape(m.lower()) for m in MODELS[:-2])
    YEAR_MODEL_RE = rf"(?:^|\s)'?([890]\d|1[0-9])\s+(?:{re.escape(make.lower())}\s+)?({alts})\b"


# ------------------------------------------------------------------ work DB
WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread (
  src TEXT NOT NULL, tid TEXT NOT NULL, url TEXT, title TEXT, sub TEXT, created INTEGER,
  text TEXT NOT NULL, score INTEGER, tags TEXT, PRIMARY KEY (src, tid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS extract (
  src TEXT NOT NULL, tid TEXT NOT NULL,
  relevant INTEGER, model TEXT, year INTEGER, engine TEXT, codes TEXT, symptoms TEXT,
  category TEXT, part TEXT, fix TEXT, confirmed TEXT, evidence TEXT, evidence_ok INTEGER,
  llm_model TEXT, ms INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, at TEXT,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS extract_model ON extract(model, year);
CREATE TABLE IF NOT EXISTS extract_code (   -- one row per (thread, code), for counting
  src TEXT NOT NULL, tid TEXT NOT NULL, code TEXT NOT NULL, PRIMARY KEY (code, src, tid)) WITHOUT ROWID;
"""


def work_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(WORK_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ro(path):
    return sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True)


# ------------------------------------------------------------------ source mapping
def cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]


def pick(names, *cands):
    low = {n.lower(): n for n in names}
    for c in cands:
        if c in low:
            return low[c]
    return None


def map_source(conn, post_table=None, comment_table=None):
    """Find the post/thread table and the comment/reply table and the columns we need.
    Works for the Pushshift-style reddit_subaru.db and for most forum dumps; override with flags."""
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'")]
    # Reddit: posts + comments. Forums: threads/topics + posts (the posts are the replies).
    thread_t = next((t for t in tables if re.search(r"(thread|topic|submission)s?$", t, re.I)), None)
    if thread_t:
        pt = post_table or thread_t
        ct = comment_table or next((t for t in tables if t != pt and
                                    re.search(r"(post|comment|reply|replies|message)s?$", t, re.I)), None)
    else:
        pt = post_table or next((t for t in tables if re.search(r"posts?$", t, re.I)), None)
        ct = comment_table or next((t for t in tables if re.search(r"(comment|reply|replies|message)s?$", t, re.I)), None)
    if not pt:
        return None
    pc = cols(conn, pt)
    m = {"post_table": pt, "comment_table": ct,
         "p_id": pick(pc, "id", "post_id", "thread_id", "topic_id", "tid"),
         "p_title": pick(pc, "title", "subject"),
         "p_body": pick(pc, "selftext", "body", "text", "content", "first_post"),
         "p_sub": pick(pc, "subreddit", "forum", "board", "section"),
         "p_created": pick(pc, "created_utc", "created", "date", "posted_at", "timestamp"),
         "p_url": pick(pc, "permalink", "url", "link"),
         "p_score": pick(pc, "score", "ups"),
         "p_flair": pick(pc, "link_flair_text", "flair"),
         # regex tags the scrapers already added (reddit_subaru.db, ultimatesubaru.db): fallbacks only
         "p_models": pick(pc, "models"), "p_years": pick(pc, "years"), "p_dtcs": pick(pc, "dtcs")}
    if ct:
        cc = cols(conn, ct)
        own_pk = {r[1] for r in conn.execute(f"PRAGMA table_info('{ct}')") if r[5]}   # never the reply's own id
        parents = (("thread_id", "topic_id", "tid", "submission_id", "link_id", "post_id") if thread_t else
                   ("link_id", "post_id", "submission_id", "thread_id", "topic_id"))
        m.update({
            "c_parent": pick([c for c in cc if c not in own_pk], *parents),
            "c_body": pick(cc, "body", "text", "content"),
            "c_score": pick(cc, "score", "ups"),
            "c_op": pick(cc, "is_op", "op", "is_submitter", "by_op", "author_is_op"),
            "c_order": pick(cc, "position", "created_utc", "created", "posted_at", "date", "timestamp", "id")})
        if m["c_order"] == pick(cc, "position") and pick(cc, "page"):   # forum position may restart per page
            m["c_order"] = f"{pick(cc, 'page')} * 100000 + {m['c_order']}"
    return m


def norm_parent(v):
    v = str(v)
    return v[3:] if v.startswith(("t3_", "t1_")) else v


def iter_threads(conn, m, max_chars, max_comments):
    p = m
    sel = [p[k] or "NULL" for k in ("p_id", "p_title", "p_body", "p_sub", "p_created", "p_url", "p_score",
                                     "p_models", "p_years", "p_dtcs")]
    comments = {}
    if m.get("comment_table") and m.get("c_parent") and m.get("c_body"):
        csel = [m["c_parent"], m["c_body"], m["c_score"] or "0", m["c_op"] or "0", m["c_order"] or "0"]
        print(f"reading comments from {m['comment_table']} ...", file=sys.stderr)
        for parent, body, score, op, order in conn.execute(f"SELECT {', '.join(csel)} FROM {m['comment_table']}"):
            if body and body not in ("[deleted]", "[removed]"):
                comments.setdefault(norm_parent(parent), []).append((body, score or 0, op or 0, order or 0))
    for pid, title, body, sub, created, url, score, t_models, t_years, t_dtcs in conn.execute(
            f"SELECT {', '.join(sel)} FROM {p['post_table']}"):
        cs = sorted(comments.get(norm_parent(pid), []), key=lambda c: c[3])     # thread order
        if not body and cs and cs[0][2]:      # forums: the thread's first post is the OP's question
            body, cs = cs[0][0], cs[1:]
        op_cs = [c for c in cs if c[2]]                                          # OP's own replies, in order
        others = sorted([c for c in cs if not c[2]], key=lambda c: -c[1])       # best-scored first (stable)
        tags = {"models": t_models, "years": t_years, "dtcs": t_dtcs}
        lines = [f"TITLE: {title or ''}", f"[OP] {body or ''}".strip()]
        for c in op_cs[:max_comments]:
            lines.append(f"[OP reply] {c[0]}")
        for c in others[:max_comments]:
            lines.append(f"[reply, score {c[1]}] {c[0]}")
        text = "\n\n".join(lines)[:max_chars]
        yield str(pid), title, sub, created, url, score, text, op_cs, json.dumps(tags)


def prefilter_score(title, text, op_cs):
    s = 0
    s += 3 * bool(DTC_RE.search(text))
    s += 2 * bool(SYMPTOM_RE.search((title or "") + " " + text[:1500]))
    op_text = " ".join(c[0] for c in op_cs)
    s += 4 * bool(FIX_RE.search(op_text))       # OP came back and said what fixed it
    s += 1 * bool(FIX_RE.search(text))
    return s


def step_inspect(a):
    for src, path in (("reddit", a.reddit), ("forum", a.forum)):
        if not path:
            continue
        conn = ro(path)
        print(f"== {src}: {path}")
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            print(f"  {t}: {', '.join(cols(conn, t))}")
        print("  mapping:", json.dumps(map_source(conn, a.post_table, a.comment_table), indent=2))


def step_filter(a):
    w = work_db(a.work)
    total = kept = 0
    for src, path in (("reddit", a.reddit), (a.forum_name, a.forum)):
        if not path:
            continue
        conn = ro(path)
        m = map_source(conn, a.post_table, a.comment_table)
        if not m or not m["p_id"] or not (m["p_title"] or m["p_body"]):
            sys.exit(f"can't map {path}; run `inspect` and pass --post-table/--comment-table")
        rows = []
        sql = "INSERT OR REPLACE INTO thread VALUES (?,?,?,?,?,?,?,?,?)"   # REPLACE: re-filter picks up new pages
        for tid, title, sub, created, url, score, text, op_cs, tags in iter_threads(conn, m, a.max_chars, a.max_comments):
            total += 1
            # the scraper's dtcs tag can be the string "[]": count it only if it holds a real code
            sc = prefilter_score(title, text, op_cs) + 3 * bool(DTC_RE.search(str(json.loads(tags)["dtcs"] or "")))
            if a.all or sc >= a.min_score:
                if url and url.startswith("/r/"):
                    url = "https://www.reddit.com" + url
                rows.append((src, tid, url, title, sub, created, text, sc, tags))
            if len(rows) >= 2000:
                w.executemany(sql, rows)
                kept += len(rows)
                rows = []
        w.executemany(sql, rows)
        kept += len(rows)
        w.commit()
    print(f"filter: {kept} of {total} threads kept (min score {a.min_score}{', --all' if a.all else ''})")


# ------------------------------------------------------------------ LLM extraction
def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def evidence_ok(evidence, text):
    """True when the quoted evidence really is in the thread (exactly, or 60%+ of its 4-word runs)."""
    e, t = _norm(evidence), _norm(text)
    if len(e.split()) < 5:   # "fixed" or "it worked" is in almost every thread: too short to prove anything
        return 0
    if e in t:
        return 1
    words = e.split()
    grams = [" ".join(words[i:i + 4]) for i in range(max(1, len(words) - 3))]
    return int(sum(g in t for g in grams) / len(grams) >= 0.6)


def _one_tag(v, pattern):
    """A scraper tag like 'Forester' or '2001,2002': use it only when it names exactly one value."""
    vals = list(dict.fromkeys(re.findall(pattern, str(v or ""))))
    return vals[0] if len(vals) == 1 else None


def guess_model_year(ex, title, sub, tags):
    if ex["model"] in ("Unknown", "Other"):
        tagged = _one_tag(tags.get("models"), r"[A-Za-z][A-Za-z ]+")
        tagged = next((m for m in MODELS[:-2] if tagged and m.lower() == tagged.strip().lower()), None)
        named = next((m for m in MODELS[:-2] if sub and m.lower() in sub.lower()), None)  # board name
        ex["model"] = tagged or SUB_MODEL.get((sub or "").lower()) or named or ex["model"]
    if not ex["year"] and _one_tag(tags.get("years"), r"\b(?:19|20)\d\d\b"):
        ex["year"] = int(_one_tag(tags.get("years"), r"\b(?:19|20)\d\d\b"))
    if not ex["year"]:
        y = re.search(r"\b(19[89]\d|20[0-2]\d)\b", title or "")
        y2 = re.search(YEAR_MODEL_RE, title or "", re.I)        # "01 Forester", "'98 Legacy"
        if y:
            ex["year"] = int(y.group(1))
        elif y2:
            n = int(y2.group(1))
            ex["year"] = 1900 + n if n >= 80 else 2000 + n
    if ex["year"] and not 1980 <= ex["year"] <= 2030:
        ex["year"] = None
    return ex


def extract_one(llm, row):
    src, tid, title, sub, text, tags = row
    tags = json.loads(tags or "{}")
    t0 = time.time()
    ex, usage = llm.json(SYSTEM, text, EXTRACT_SCHEMA, max_tokens=400)
    ms = int((time.time() - t0) * 1000)
    ex = guess_model_year(ex, title, sub, tags)
    ex["codes"] = sorted({c.strip().upper() for c in ex["codes"] if DTC_RE.fullmatch(c.strip())})
    if not ex["codes"] and tags.get("dtcs"):
        ex["codes"] = sorted({c.upper() for c in DTC_RE.findall(str(tags["dtcs"]))})
    if ex.get("evidence"):
        ex["evidence"] = re.sub(r"^\s*\[(OP|OP reply|reply[^\]]*)\]\s*", "", ex["evidence"])
    ok = evidence_ok(ex.get("evidence"), text)
    if not ok and ex["confirmed"] in ("op_confirmed", "other_confirmed"):
        ex["confirmed"] = "suggested_only"     # can't point at the sentence: don't count it as confirmed
    return src, tid, ex, ok, ms, usage


def step_run(a):
    w = work_db(a.work)
    llm = LLM(a.url, temperature=0.0)
    if not llm.ping():
        sys.exit(f"llama-server not answering at {a.url} (start serve_extract.sh first)")
    todo_sql = ("SELECT t.src, t.tid, t.title, t.sub, t.text, t.tags FROM thread t LEFT JOIN extract e "
                "ON e.src=t.src AND e.tid=t.tid WHERE e.tid IS NULL ORDER BY t.score DESC, t.tid")
    remaining = w.execute(f"SELECT count(*) FROM ({todo_sql})").fetchone()[0]
    todo = w.execute(todo_sql + (f" LIMIT {int(a.limit)}" if a.limit else "")).fetchall()
    print(f"run: {len(todo)} threads now, {remaining} left in total, {a.parallel} parallel")
    t0, done, ptok, ctok, failed, streak = time.time(), 0, 0, 0, 0, 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with cf.ThreadPoolExecutor(a.parallel) as pool:
        futs = [pool.submit(extract_one, llm, r) for r in todo]
        for f in cf.as_completed(futs):
            try:
                src, tid, ex, ok, ms, usage = f.result()
                streak = 0
            except Exception as e:  # one bad thread is skipped (retried next run), the run goes on
                failed += 1
                streak += 1
                print("  skip:", e, file=sys.stderr)
                if streak >= 20 and not llm.ping():   # the server died: stop instead of skipping everything
                    for x in futs:
                        x.cancel()
                    w.commit()
                    sys.exit(f"run: llama-server stopped answering after {done} threads; rerun to continue")
                continue
            w.execute("INSERT OR REPLACE INTO extract VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                src, tid, int(ex["relevant"]), ex["model"], ex["year"], ex["engine"], json.dumps(ex["codes"]),
                json.dumps(ex["symptoms"]), ex["category"], ex["part"], ex["fix"], ex["confirmed"],
                ex["evidence"], ok, a.model_name, ms, usage.get("prompt_tokens"), usage.get("completion_tokens"), now))
            w.execute("DELETE FROM extract_code WHERE src=? AND tid=?", (src, tid))
            w.executemany("INSERT OR IGNORE INTO extract_code VALUES (?,?,?)", [(src, tid, c) for c in ex["codes"]])
            done += 1
            ptok += usage.get("prompt_tokens") or 0
            ctok += usage.get("completion_tokens") or 0
            if done % 25 == 0 or done == len(todo):
                w.commit()
                el = time.time() - t0
                rate = done / el
                left = remaining - done
                print(f"  {done}/{len(todo)}  {rate * 60:.1f} threads/min  {(ptok + ctok) / el:.0f} tok/s  "
                      f"ETA for all {left}: {left / rate / 3600:.1f} h", flush=True)
    w.commit()
    if failed:
        print(f"run: {failed} threads failed and will be retried on the next run")


def step_stats(a):
    w = work_db(a.work)
    q = lambda s: w.execute(s).fetchall()
    print("threads:", q("SELECT src, count(*) FROM thread GROUP BY src"))
    print("extracted:", q("SELECT count(*), sum(relevant), sum(evidence_ok) FROM extract")[0],
          "(rows, relevant, evidence found)")
    print("confirmed:", q("SELECT confirmed, count(*) FROM extract WHERE relevant GROUP BY confirmed"))
    print("speed:", q("SELECT avg(ms), avg(prompt_tokens), avg(completion_tokens) FROM extract")[0], "(ms, in, out)")
    print("top codes:", q("SELECT code, count(*) n FROM extract_code GROUP BY code ORDER BY n DESC LIMIT 15"))
    if a.code:
        print(f"{a.code} fixes:", q(f"""SELECT e.model, e.category, count(*) n FROM extract_code c JOIN extract e USING(src, tid)
            WHERE c.code='{a.code.upper()}' AND e.confirmed IN ('op_confirmed','other_confirmed')
            GROUP BY e.model, e.category ORDER BY n DESC LIMIT 20"""))


# The model picks 'category' before it writes 'part' and 'fix' (JSON key order), so its bucket is often a guess
# (cam sensors and O2 sensors filed under 'differential/viscous coupling'). recat re-derives the bucket from the
# part and fix the owner actually named. Word lists only translate part names into bucket names; first match wins,
# so the more specific names come first.
RECAT_RULES = [
    ("other", r"\bavcs\b|\bvvt\b|oil control valve|\btgv\b|tumble"),
    ("spark plug wires", r"plug wires?|ignition wires?|plug leads?|spark plug connectors?|plug boots?"),
    ("valve cover gasket/plug tube seals", r"valve cover|tube seals?|plug tube|grommets?"),
    ("low compression/engine internal", r"compression|burn(ed|t)? (exhaust |intake )?valve|piston|\brings?\b|rod bearing|"
                                        r"spun bearing|cylinder wall|short block|engine (rebuild|replace)|rod knock|tear ?down|valve springs?|\bvalves\b|"
                                        r"(intake|exhaust) cam\b|camshaft(?! (position )?sensor)"),
    ("head gasket", r"head gaskets?|\bhg\b"),
    ("ignition coil", r"\bcoils?\b|coil packs?|igniter"),
    ("spark plugs", r"spark ?plugs?|\bplugs\b|iridium"),
    ("fuel injector", r"injectors?"),
    ("fuel pressure regulator", r"pressure regulator|\bfpr\b"),
    ("fuel pump", r"fuel pump"),
    ("fuel filter", r"fuel filter"),
    ("pcv valve/hose", r"\bpcv\b"),
    ("intake gasket", r"intake (manifold )?gaskets?|intake manifold|collector"),
    ("vacuum leak", r"vacuum|boost leak"),
    ("throttle body", r"throttle body|throttle position|\btps\b"),
    ("idle air control valve", r"idle air|\biac\b|\biacv\b"),
    ("mass air flow sensor", r"mass air|\bmaf\b"),
    ("rear o2 sensor", r"(rear|downstream|secondary|post-?cat) (o2|oxygen)"),
    ("front o2/air-fuel sensor", r"\ba/?f sensor|air.fuel|(front|upstream|primary) (o2|oxygen)|\b(o2|oxygen) sensor"),
    ("transmission", r"torque converter|valve body|\bcvt\b|neutral (safety )?switch|park/neutral"),
    ("catalytic converter", r"catalytic|\bcats?\b|catless|cat converter"),
    ("exhaust manifold/header gasket", r"exhaust manifold|header|up[- ]?pipe|j[- ]?pipe|crossover|y[- ]?pipe"),
    ("exhaust leak other", r"exhaust|muffler|flex pipe|down[- ]?pipe"),
    ("egr valve/passages", r"\begr\b"),
    ("purge valve/evap", r"purge|evap|canister"),
    ("gas cap", r"(gas|fuel) cap"),
    ("valve adjustment", r"valve (adjust|clearance|lash)|lash adjusters?|tappets?|lifters?"),
    ("timing belt/tensioner/idler", r"timing|tensioner|idler"),
    ("camshaft position sensor", r"cam(shaft)? (position )?sensor"),
    ("crankshaft position sensor", r"crank(shaft)? (position )?sensor"),
    ("knock sensor", r"knock sensor"),
    ("coolant temp sensor", r"coolant temp|\bect\b|temp(erature)? sensor"),
    ("thermostat", r"thermostat|thermo(stat|al)? control valve|\btcv\b"),
    ("water pump", r"water pump"),
    ("radiator/hoses", r"radiator|coolant hose|(upper|lower) hose|heater hose"),
    ("battery/charging", r"batter(y|ies)|alternator|charging"),
    ("ecu", r"\becu\b|\becm\b|computer|reflash|\btune\b|tuned"),
    ("wiring/connector/ground", r"ground|wiring|harness|connector|\bwires?\b|relay|fuse|plugged? (back )?in|unplugged"),
    ("wheel bearing", r"wheel bearing|hub"),
    ("cv axle", r"\baxle|\bcv\b"),
    ("abs sensor", r"\babs\b"),
    ("brakes", r"brake|caliper|rotor|\bpads?\b"),
    ("suspension", r"strut|shock|control arm|sway|bushing|suspension|tie rod|ball joint"),
    ("clutch", r"clutch|flywheel|throw[- ]?out"),
    ("differential/viscous coupling", r"differential|viscous|center diff|\bdiff\b|transfer"),
    ("transmission", r"transmission|\btrans\b|\batf\b|torque converter|\btcu\b|\btcm\b|solenoid"),
    ("torque bind/tires", r"tire (size|wear|mismatch|rotation)|mismatched tires|torque bind"),
]
RECAT_RE = [(cat, re.compile(rx, re.I)) for cat, rx in RECAT_RULES]


def recat_one(part, fix, old):
    for text in (part or "", fix or ""):
        for cat, rx in RECAT_RE:
            if rx.search(text):
                return cat
    return old if old in ("unknown", "other") else "other"


def step_recat(a):
    w = work_db(a.work)
    if "category_llm" not in {r[1] for r in w.execute("PRAGMA table_info(extract)")}:
        w.execute("ALTER TABLE extract ADD COLUMN category_llm TEXT")
        w.execute("UPDATE extract SET category_llm=category")
    rows = w.execute("SELECT src, tid, part, fix, category_llm FROM extract").fetchall()
    changed = 0
    for src, tid, part, fix, old in rows:
        cat = recat_one(part, fix, old)
        changed += cat != old
        w.execute("UPDATE extract SET category=? WHERE src=? AND tid=?", (cat, src, tid))
    w.commit()
    print(f"recat: {len(rows)} rows, {changed} buckets differ from the model's pick (kept in category_llm)")


def step_export(a):
    w = work_db(a.work)
    rows = w.execute("""
        SELECT e.src, t.url, t.title, e.model, e.year, e.codes, e.symptoms, e.fix, e.part, e.category,
               e.confirmed, e.evidence, t.tid
        FROM extract e JOIN thread t USING(src, tid)
        WHERE e.relevant AND e.evidence_ok AND e.fix IS NOT NULL
          AND e.confirmed IN ('op_confirmed', 'other_confirmed')""").fetchall()
    d = sqlite3.connect(os.path.expanduser(a.diag_db))
    d.execute("""CREATE TABLE IF NOT EXISTS forum_fix (
        id INTEGER PRIMARY KEY, site TEXT, url TEXT UNIQUE, title TEXT, model TEXT,
        year_from INT, year_to INT, summary TEXT, confirmed INT, retrieved_at TEXT)""")
    sites = sorted({r[0] for r in rows})
    d.executemany("DELETE FROM forum_fix WHERE site=?", [(s,) for s in sites])
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for src, url, title, model, year, codes, symptoms, fix, part, cat, conf, ev, tid in rows:
        codes, symptoms = json.loads(codes), json.loads(symptoms)
        summary = "\n".join(x for x in (
            f"Codes: {', '.join(codes)}" if codes else "",
            f"Symptoms: {'; '.join(symptoms)}" if symptoms else "",
            f"Fix: {fix}" + (f" ({part})" if part else ""),
            f"Cause bucket: {cat}",
            f"Confirmed by: {'original poster' if conf == 'op_confirmed' else 'another owner'}",
            f'Quote: "{ev}"') if x)
        out.append((src, url or f"{src}:{tid}", title, None if model in ("Unknown", "Other") else model.upper(),
                    year, year, summary, 1, now))
    d.executemany("INSERT OR REPLACE INTO forum_fix(site,url,title,model,year_from,year_to,summary,confirmed,"
                  "retrieved_at) VALUES (?,?,?,?,?,?,?,?,?)", out)
    d.commit()
    print(f"export: {len(out)} confirmed fixes -> {a.diag_db} forum_fix (sites: {', '.join(sites)}). "
          f"Next: python3 build_subaru_kb.py diag {a.diag_db}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default=DEFAULT_WORK, help="work DB (default %(default)s)")
    ap.add_argument("--make", help="another make than Subaru (prompt and model list name it); use its own --work")
    ap.add_argument("--models", help="with --make: comma list of that make's model names")
    sub = ap.add_subparsers(dest="step", required=True)
    for name in ("inspect", "filter"):
        p = sub.add_parser(name)
        p.add_argument("--reddit", help="reddit_subaru.db")
        p.add_argument("--forum", help="another forum DB, e.g. ultimatesubaru.db")
        p.add_argument("--forum-name", default="ultimatesubaru")
        p.add_argument("--post-table")
        p.add_argument("--comment-table")
        if name == "filter":
            p.add_argument("--min-score", type=int, default=5,
                           help="keyword score to keep a thread (code 3, symptom 2, OP says fixed 4, fix words 1)")
            p.add_argument("--all", action="store_true", help="keep every thread (slow: ~4x the LLM time)")
            p.add_argument("--max-chars", type=int, default=6000, help="thread text sent to the model (~1500 tokens)")
            p.add_argument("--max-comments", type=int, default=8)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8080")
    r.add_argument("--parallel", type=int, default=4, help="match llama-server -np")
    r.add_argument("--limit", type=int, help="only this many (timing sample)")
    r.add_argument("--model-name", default="Qwen3-8B-Q4_K_M", help="recorded with each row")
    s = sub.add_parser("stats")
    s.add_argument("--code")
    sub.add_parser("recat", help="re-derive each fix's cause bucket from its part/fix text (no model needed)")
    e = sub.add_parser("export")
    e.add_argument("--diag-db", default="~/subaru_cyberdeck/subaru_diag.db")
    a = ap.parse_args()
    if a.make and a.make.lower() != "subaru":
        configure_make(a.make, (a.models or "").split(","))
    {"inspect": step_inspect, "filter": step_filter, "run": step_run, "stats": step_stats,
     "recat": step_recat, "export": step_export}[a.step](a)


if __name__ == "__main__":
    main()
