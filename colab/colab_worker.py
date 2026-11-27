#!/usr/bin/env python3
"""
Standalone Colab GPU extraction worker for Open Diagnostic Cyberdeck Community DB.

Uses the EXACT prompt, JSON schema, quote-checking, validation, priority ordering,
and thread filter from ~/subaru_cyberdeck/kb/build_community.py (2026-11-10),
adapted for high-throughput OpenAI-compatible serving (vLLM / llama-server) on Colab:
  - Runs SQLite strictly on local SSD (never on Google Drive FUSE)
  - Batch-commits SQLite writes and appends resumable JSONL checkpoints to Drive
  - Resumes cleanly from both existing work.db `extract` rows and Drive `.jsonl` checkpoints
  - Supports graceful SIGTERM / --max-minutes stop so Colab sessions never lose work
"""
import argparse
import concurrent.futures as cf
import itertools
import json
import os
import re
import shutil
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request

DTC_RE = re.compile(r"\b[PCBU][0-3][0-9A-F]{3}\b", re.I)
OUTCOME_RE = re.compile(
    r"\b(fixed|solved|resolved|turned out|was the (culprit|problem|issue|cause)|that did it|problem gone|"
    r"update:|edit:|replaced (the|it|my)|swapped (the|out)|after replacing|haven'?t had|culprit|works now|"
    r"runs (great|fine|good) now|no more)\b", re.I)

STOP = set("the a an and or of to in on for with is are was were it this that at by from be as my car i me "
           "you your but not so if its it's has have had do did does just like also then than there their they "
           "we he she his her them what when which who will would could should can may might very really bad "
           "new old faulty failed failing broken replaced replace replacing".split())

BLANK = {"", "unknown", "n/a", "na", "none", "not stated", "not specified", "unspecified", "not mentioned"}

WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread (
  src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT, title TEXT, created INTEGER, score INTEGER,
  n_replies INTEGER, segs TEXT NOT NULL, tags TEXT, rules TEXT, card_keep INTEGER, pilot INTEGER DEFAULT 0,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS prefilter_stat (src TEXT, sub TEXT, metric TEXT, n INTEGER, PRIMARY KEY (src, sub, metric));
CREATE TABLE IF NOT EXISTS sub_score (src TEXT, sub TEXT, p90 INTEGER, PRIMARY KEY (src, sub));
CREATE TABLE IF NOT EXISTS run (
  make TEXT, model TEXT, backend TEXT, parallel INTEGER, lite INTEGER, n INTEGER, seconds REAL, started TEXT);
CREATE TABLE IF NOT EXISTS extract (
  src TEXT NOT NULL, tid TEXT NOT NULL, status TEXT NOT NULL, raw TEXT, rec TEXT, checks TEXT,
  llm_model TEXT, ms INTEGER, prompt_tokens INTEGER, eval_tokens INTEGER, tries INTEGER DEFAULT 1, at TEXT,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
"""

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
                "role": {"type": "string", "enum": ["observation", "conclusion", "test", "result"]}, **QS},
                "required": ["role", "seg", "quote"]}}},
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
- chains: at most 3 chains of reasoning that the thread actually shows, as 2 to 5 steps in order. Each step has a role (observation, conclusion, test or result) and the quote that is that step. Skip when the thread shows no such chain.
- refs: TSB or recall numbers, part numbers, costs and labor times exactly as written.
- Keep text fields short (under 15 words)."""

HALT = threading.Event()

def _stop(signum, frame):
    HALT.set()

def thread_text(segs):
    lab = {"op": "OP", "op_reply": "OP reply"}
    out = []
    for i, s in enumerate(segs):
        tag = lab.get(s["k"]) or (f"reply, score {s['s']}" if s["s"] is not None else "reply")
        out.append(f"[S{i} {tag}]\n{s['t']}")
    return "\n\n".join(out)

def priority(segs, rules):
    op_out = any(OUTCOME_RE.search(sg["t"]) for sg in segs if sg["k"] != "reply")
    return int(op_out and bool(rules.get("code") or rules.get("test")))

def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.%/ ]", " ", (s or "").lower())).strip()

def quote_in(quote, text):
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

def locate(item, segs):
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
    ck = {"items": 0, "kept": 0, "quote_fail": 0, "unsupported": 0, "seg_fixed": 0, "num_fail": 0}
    nsegs = [norm(s["t"]) for s in segs]

    def who(i):
        return "op" if segs[i]["k"] in ("op", "op_reply") else "reply"

    def take(item, main=None):
        ck["items"] += 1
        i, moved = locate(item, segs)
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
    v = {k: ("" if norm(x) in BLANK else x) if isinstance(x, str) else x for k, x in (raw.get("vehicle") or {}).items()}
    veh = {}
    if any((v.get(k) or "").strip() for k in ("model", "year", "engine", "transmission", "mileage")):
        t = take(v)
        if t:
            q = t["quote"]
            for k in ("model", "engine", "transmission"):
                if (v.get(k) or "").strip():
                    ck["items"] += 1
                    if (supported(v[k], q) and content_tokens(v[k])) or \
                            re.search(rf"(?<![a-z0-9]){re.escape(norm(v[k]))}(?![a-z0-9])", " ".join(nsegs)):
                        veh[k] = v[k].strip()
                        ck["kept"] += 1
                    else:
                        ck["unsupported"] += 1
            for k, good in (("year", year_ok), ("mileage", numbers_in)):
                if (v.get(k) or "").strip():
                    ck["items"] += 1
                    if good(v[k], q):
                        veh[k] = v[k].strip()
                        ck["kept"] += 1
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
        steps = [take(s) for s in ch.get("steps") or [] if s.get("role")]
        if len(steps) >= 2 and all(steps) and len({s["role"] for s in steps}) >= 2:
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

class OpenAIClient:
    """OpenAI-compatible client for vLLM or llama-server (includes `model` field required by vLLM)."""
    def __init__(self, model, url="http://127.0.0.1:8000", timeout=300):
        self.model = model
        self.url = url.rstrip("/")
        self.timeout = timeout

    def chat(self, system, user):
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 2048,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "thread", "strict": True, "schema": SCHEMA},
            },
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if "qwen3" in self.model.lower():
            body["chat_template_kwargs"] = {"enable_thinking": False}
        req = urllib.request.Request(
            self.url + "/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            d = json.loads(r.read())
        u = d.get("usage") or {}
        content = d["choices"][0]["message"].get("content") or ""
        return content, u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0

def load_jsonl_into_db(w, jsonl_path):
    """Replay any Drive JSONL checkpoint rows into local SQLite before resuming (skips transport errors)."""
    if not jsonl_path or not os.path.isfile(jsonl_path):
        return 0
    n = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("status") == "error":
                continue
            w.execute(
                "INSERT INTO extract(src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(src, tid) DO UPDATE SET "
                "status=CASE WHEN extract.status='ok' THEN extract.status ELSE excluded.status END, "
                "raw=CASE WHEN extract.status='ok' THEN extract.raw ELSE excluded.raw END, "
                "rec=CASE WHEN extract.status='ok' THEN extract.rec ELSE excluded.rec END, "
                "checks=CASE WHEN extract.status='ok' THEN extract.checks ELSE excluded.checks END, "
                "llm_model=excluded.llm_model, ms=excluded.ms, prompt_tokens=excluded.prompt_tokens, "
                "eval_tokens=excluded.eval_tokens, at=excluded.at, tries=extract.tries+1",
                (r["src"], r["tid"], r["status"], r.get("raw"),
                 json.dumps(r["rec"]) if r.get("rec") is not None else None,
                 json.dumps(r.get("checks") or {}), r.get("llm_model", ""),
                 r.get("ms", 0), r.get("prompt_tokens", 0), r.get("eval_tokens", 0), r.get("at", "")),
            )
            n += 1
    w.commit()
    return n

def thread_filter(min_replies, any_of):
    where, args = ["1"], []
    if min_replies:
        where.append("t.n_replies >= ?")
        args.append(min_replies)
    if any_of:
        flags = [f.strip() for f in any_of.split(",") if f.strip()]
        where.append("(" + " OR ".join(f"json_extract(t.rules, '$.{f}')" for f in flags) + ")")
    return " AND ".join(where), args

def sync_file_atomic(src, dst):
    if not dst:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)

def main():
    ap = argparse.ArgumentParser(description="High-throughput Colab community extraction worker")
    ap.add_argument("--make", required=True)
    ap.add_argument("--work-db", required=True, help="Local SSD path to <make>.work.db")
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="Qwen/Qwen3.5-9B")
    ap.add_argument("--parallel", type=int, default=32)
    ap.add_argument("--min-replies", type=int, default=3)
    ap.add_argument("--any-of", default="test,outcome,code")
    ap.add_argument("--order", choices=("stored", "priority"), default="priority")
    ap.add_argument("--priority-only", action="store_true",
                    help="Only extract priority threads (OP outcome + code/test) — ideal for 2-tier runs with a bigger model")
    ap.add_argument("--limit", type=int, default=0, help="Max threads to extract in this run (0 = all)")
    ap.add_argument("--max-minutes", type=float, default=0, help="Graceful stop after N minutes (0 = no cap)")
    ap.add_argument("--stats-every", type=int, default=500)
    ap.add_argument("--checkpoint-jsonl", default="", help="Local JSONL file to append results to")
    ap.add_argument("--drive-checkpoint-jsonl", default="", help="Drive path to mirror JSONL every stats-every rows")
    ap.add_argument("--drive-work-db", default="", help="Drive path to save finalized <make>.work.db when finished")
    a = ap.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    w = sqlite3.connect(a.work_db, timeout=60)
    w.executescript(WORK_SCHEMA)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA synchronous=NORMAL")
    w.execute("PRAGMA cache_size=-65536")

    if a.drive_checkpoint_jsonl and os.path.isfile(a.drive_checkpoint_jsonl):
        if a.checkpoint_jsonl and not os.path.isfile(a.checkpoint_jsonl):
            os.makedirs(os.path.dirname(a.checkpoint_jsonl), exist_ok=True)
            shutil.copy2(a.drive_checkpoint_jsonl, a.checkpoint_jsonl)
        replayed = load_jsonl_into_db(w, a.drive_checkpoint_jsonl)
        print(f"[{a.make}] Replayed {replayed:,} checkpoint rows from Drive", flush=True)
    elif a.checkpoint_jsonl and os.path.isfile(a.checkpoint_jsonl):
        replayed = load_jsonl_into_db(w, a.checkpoint_jsonl)
        print(f"[{a.make}] Replayed {replayed:,} local checkpoint rows", flush=True)

    where, args = thread_filter(a.min_replies, a.any_of)
    p90 = {(s, sub): p for s, sub, p in w.execute("SELECT src, sub, p90 FROM sub_score")}
    todo = w.execute(
        f"SELECT t.src, t.tid, t.sub, t.segs, t.rules, t.created FROM thread t LEFT JOIN extract e "
        f"ON e.src=t.src AND e.tid=t.tid WHERE {where} AND (e.status IS NULL OR (e.status!='ok' AND e.tries<3))",
        args,
    ).fetchall()

    if a.priority_only:
        todo = [r for r in todo if priority(json.loads(r[3]), json.loads(r[4] or "{}"))]
        todo.sort(key=lambda r: -(r[5] or 0))
    elif a.order == "priority":
        todo.sort(key=lambda r: (-priority(json.loads(r[3]), json.loads(r[4] or "{}")), -(r[5] or 0)))
    if a.limit > 0:
        todo = todo[:a.limit]

    already_ok = w.execute(
        f"SELECT count(*) FROM thread t JOIN extract e ON e.src=t.src AND e.tid=t.tid WHERE {where} AND e.status='ok'",
        args,
    ).fetchone()[0]
    print(f"[{a.make}] Already ok: {already_ok:,} | Remaining in run: {len(todo):,} | parallel={a.parallel} | model={a.model}", flush=True)

    if not todo:
        print(f"[{a.make}] Nothing left to extract!", flush=True)
        if a.drive_work_db:
            w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            w.close()
            sync_file_atomic(a.work_db, a.drive_work_db)
            print(f"[{a.make}] Saved finished DB -> {a.drive_work_db}", flush=True)
        return

    client = OpenAIClient(a.model, a.url)
    try:
        with urllib.request.urlopen(a.url.rstrip("/") + "/health", timeout=5) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
    except Exception as e:
        sys.exit(f"[{a.make}] ERROR: vLLM server at {a.url} is not reachable ({e}). Re-run Cell 2 first!")

    system = SYSTEM.format(make=a.make.replace("_", " ").title())
    t0 = time.time()
    deadline = (t0 + a.max_minutes * 60.0) if a.max_minutes > 0 else 0
    started = time.strftime("%F %T")

    jf_out = None
    if a.checkpoint_jsonl:
        os.makedirs(os.path.dirname(a.checkpoint_jsonl), exist_ok=True)
        jf_out = open(a.checkpoint_jsonl, "a", encoding="utf-8")

    sess_ok = sess_jf = sess_err = consec_err = 0
    sess_items = sess_qf = sess_uns = 0
    sess_pt = sess_et = 0
    n = 0
    pending_commit = 0

    def work(row):
        src, tid, sub, segs_json, _, _ = row
        segs = json.loads(segs_json)
        t1 = time.time()
        try:
            content, pt, et = client.chat(system, thread_text(segs))
        except Exception as e:
            time.sleep(3)
            return src, tid, ("error", None, 0, 0, None, {"error": str(e)[:200]}), int((time.time() - t1) * 1000)
        try:
            raw = json.loads(content)
            rec, ck = validate(raw, segs, p90.get((src, sub)))
            return src, tid, ("ok", content, pt, et, rec, ck), int((time.time() - t1) * 1000)
        except Exception as e:
            return src, tid, ("json_fail", content, pt, et, None, {"error": str(e)[:200]}), int((time.time() - t1) * 1000)

    try:
        with cf.ThreadPoolExecutor(max_workers=max(1, a.parallel)) as ex:
            it = iter(todo)
            in_flight = {ex.submit(work, r) for r in itertools.islice(it, a.parallel * 2)}
            while in_flight:
                done, in_flight = cf.wait(in_flight, return_when=cf.FIRST_COMPLETED)
                if deadline and time.time() >= deadline:
                    HALT.set()
                if HALT.is_set():
                    in_flight = {f for f in in_flight if not f.cancel()}
                for f in done:
                    src, tid, res, ms = f.result()
                    status, content, pt, et, rec, ck = res
                    at = time.strftime("%F %T")
                    w.execute(
                        "INSERT INTO extract(src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(src, tid) DO UPDATE SET "
                        "status=excluded.status, raw=excluded.raw, rec=excluded.rec, checks=excluded.checks, "
                        "llm_model=excluded.llm_model, ms=excluded.ms, prompt_tokens=excluded.prompt_tokens, "
                        "eval_tokens=excluded.eval_tokens, at=excluded.at, tries=extract.tries+1",
                        (src, tid, status, content, json.dumps(rec) if rec else None, json.dumps(ck),
                         a.model, ms, pt, et, at),
                    )
                    if jf_out is not None:
                        jf_out.write(json.dumps({
                            "src": src, "tid": tid, "status": status, "raw": content, "rec": rec,
                            "checks": ck, "llm_model": a.model, "ms": ms,
                            "prompt_tokens": pt, "eval_tokens": et, "at": at,
                        }, ensure_ascii=False) + "\n")

                    n += 1
                    pending_commit += 1
                    sess_pt += pt
                    sess_et += et
                    if status == "ok":
                        sess_ok += 1
                        consec_err = 0
                        sess_items += ck.get("items", 0)
                        sess_qf += ck.get("quote_fail", 0)
                        sess_uns += ck.get("unsupported", 0)
                    elif status == "json_fail":
                        sess_jf += 1
                        consec_err = 0
                    else:
                        sess_err += 1
                        consec_err += 1
                        if consec_err >= 20:
                            print(f"[{a.make}] ABORT: 20 consecutive server errors (vLLM died?); stopping cleanly.", flush=True)
                            HALT.set()

                    if pending_commit >= 50:
                        w.commit()
                        if jf_out:
                            jf_out.flush()
                        pending_commit = 0

                    nxt = None if HALT.is_set() else next(it, None)
                    if nxt is not None:
                        in_flight.add(ex.submit(work, nxt))

                    if n % a.stats_every == 0 or n == len(todo):
                        w.commit()
                        if jf_out:
                            jf_out.flush()
                        pending_commit = 0
                        elapsed = max(time.time() - t0, 1.0)
                        rate = 3600.0 * n / elapsed
                        left = len(todo) - n
                        eta_h = left / rate if rate > 0 else 0
                        print(
                            f"[{a.make}] {n:,}/{len(todo):,} ({100*n/len(todo):.1f}%) | "
                            f"ok={sess_ok:,} jf={100*sess_jf/n:.1f}% err={sess_err} | "
                            f"qfail={100*sess_qf/max(sess_items,1):.1f}% unsup={100*sess_uns/max(sess_items,1):.1f}% | "
                            f"tok={sess_pt//n}/{sess_et//n} | {rate:,.0f} thr/h | ETA {eta_h:.2f}h",
                            flush=True,
                        )
                        if a.checkpoint_jsonl and a.drive_checkpoint_jsonl:
                            sync_file_atomic(a.checkpoint_jsonl, a.drive_checkpoint_jsonl)
    finally:
        secs = time.time() - t0
        w.execute("INSERT INTO run VALUES (?,?,?,?,?,?,?,?)",
                  (a.make, a.model, "vllm", a.parallel, 0, n, secs, started))
        w.commit()
        if jf_out:
            jf_out.flush()
            jf_out.close()
        if a.checkpoint_jsonl and a.drive_checkpoint_jsonl and os.path.isfile(a.checkpoint_jsonl):
            sync_file_atomic(a.checkpoint_jsonl, a.drive_checkpoint_jsonl)
            print(f"[{a.make}] Synced checkpoint -> {a.drive_checkpoint_jsonl}", flush=True)

        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.close()

        if a.drive_work_db:
            sync_file_atomic(a.work_db, a.drive_work_db)
            print(f"[{a.make}] Saved work DB -> {a.drive_work_db}", flush=True)

        print(f"[{a.make}] Done: {n:,} threads in {secs/3600:.2f} h ({3600*n/max(secs,1):,.0f} thr/h wall)"
              + (" [STOPPED EARLY]" if HALT.is_set() else ""), flush=True)

if __name__ == "__main__":
    main()
