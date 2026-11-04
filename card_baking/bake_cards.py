#!/usr/bin/env python3
"""
Job 2 of card baking: write one diagnostic card per trouble code for one car, from cited sources only.

For each code it gathers, from subaru.db and the extraction work DB:
  - the FSM definition and diagnostic procedure (following the FSM's own "refer to" links one level)
  - confirmed forum fixes, counted per cause bucket for this model and year range (plain SQL)
  - matching TSBs / recalls / NHTSA complaints for this model and year
then the card model (Qwen 3.6-35B-A3B on llama-server) ranks causes and writes the card with a
source id on every claim. A second pass asks the model whether each cited source really supports
its claim and drops the ones that don't. Torque specs and labor times are copied from the DB as
printed, never written by the model.

  python3 bake_cards.py --model Forester --years 1998-2002 --limit 3      # try 3 cards
  python3 bake_cards.py --model Forester --years 1998-2002                # every code for the car
  python3 bake_cards.py --model Forester --years 1998-2002 --codes P0303,P0304 --force
  python3 bake_cards.py show P0303 --model Forester

Output: cards.db (table dtc_card), loaded into subaru.db by the kb builder. Standard library only.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

from llm import LLM

KB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kb")
DEFAULT_KB = os.path.join(KB_DIR, "subaru.db")
DEFAULT_WORK = os.path.join(KB_DIR, "bake_work.db")
DEFAULT_OUT = os.path.join(KB_DIR, "cards.db")

CARD_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dtc_card (
  code TEXT NOT NULL,                 -- P0303
  model TEXT NOT NULL,                -- Forester
  year_min INTEGER NOT NULL, year_max INTEGER NOT NULL,
  engine TEXT,                        -- EJ251 or NULL for every engine
  title TEXT NOT NULL,                -- 'P0303: Cylinder 3 misfire detected'
  text TEXT NOT NULL,                 -- the card as the deck shows it (plain text, sources inline)
  card_json TEXT NOT NULL,            -- structured card (causes, checks, live data, sources)
  sources_json TEXT NOT NULL,         -- [{id, kind, label, url}] every source the card may cite
  forum_counts_json TEXT,             -- confirmed forum fixes per cause bucket, as counted
  dropped_json TEXT,                  -- claims removed by the source check, kept for review
  llm_model TEXT, built_at TEXT,
  PRIMARY KEY (code, model, year_min, year_max)
) WITHOUT ROWID;
"""

CARD_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "causes": {"type": "array", "maxItems": 7, "items": {
            "type": "object",
            "properties": {
                "cause": {"type": "string"},
                "why": {"type": "string"},
                "how_to_check": {"type": "string"},
                "live_data": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
                "sources": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
            },
            "required": ["cause", "why", "how_to_check", "live_data", "sources"]}},
        "fsm_steps": {"type": "array", "maxItems": 10, "items": {
            "type": "object",
            # 'check' with a minimum length: a field called 'step' got filled with the manual's step codes ("10AA1")
            "properties": {"check": {"type": "string", "minLength": 20},
                           "sources": {"type": "array", "items": {"type": "string"}}},
            "required": ["check", "sources"]}},
        "caution": {"type": ["string", "null"]},
    },
    "required": ["summary", "causes", "fsm_steps", "caution"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"supported": {"type": "string", "enum": ["yes", "partly", "no"]},
                   "reason": {"type": "string", "maxLength": 300}},
    "required": ["supported", "reason"],
}

SYSTEM = """You write a diagnostic card for a Subaru trouble code, for a home mechanic with a scan tool.
Use ONLY the numbered sources given. Every cause and step lists the source ids it comes from, like ["S1","F3"].
- Order causes as in FORUM COUNTS 'ranked' (already ordered: this car's count where 3+ owners of it confirmed
  the fix, else every Subaru's count). Put an FSM-required check first only when the FSM says to check it first.
- Any count you state must match FORUM COUNTS exactly.
- A cause with zero sources must not appear. Do not add causes the sources don't mention.
- Each cause cites the forum threads (F ids) whose cause bucket matches it, plus any manual section (S ids) that covers it.
- Never write torque values, capacities or part numbers; the deck adds those from the manual itself.
- live_data: SSM/OBD values worth watching for that cause (named as a scan tool shows them, e.g. "short term fuel trim").
- fsm_steps: the manual's diagnostic checks in order, each 'check' written out as a short sentence saying what to check
  and how (e.g. "Check the harness between the sensor and the ECM for continuity"). Never just a step number or
  step code like "3" or "10AH2".
- Plain, short sentences."""

VERIFY_SYSTEM = """You check one cause listed on a car diagnostic card for a trouble code against the sources it cites.
supported=yes if a source reports it as the confirmed fix for this code, or the manual names it as something to check;
partly if a source only links it loosely; no if no cited source or the forum counts mention it at all.
A forum thread where an owner confirmed this fix counts as support. Don't judge ranking words or the mechanism.
reason: one short sentence."""


# ------------------------------------------------------------------ helpers
def ro(path):
    return sqlite3.connect(f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro", uri=True)


def has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def load_retriever(kb_path, kb_dir):
    sys.path.insert(0, os.path.expanduser(kb_dir or os.path.dirname(kb_path)))
    try:
        from kb_retriever import Retriever
    except ImportError:
        sys.exit("kb_retriever.py not found; pass --kb-dir (the folder holding build_subaru_kb.py)")
    return Retriever


def vehicle_ids(kb, model, y0, y1, engine):
    sql, args = "SELECT id FROM vehicle WHERE upper(model)=upper(?) AND year BETWEEN ? AND ?", [model, y0, y1]
    if engine:
        sql += " AND (engine=? OR engine IS NULL OR engine LIKE 'F4-%' OR engine LIKE 'H%')"
        args.append(engine)
    return [r[0] for r in kb.execute(sql, args)]


def fsm_sources(kb, code, vids, budget):
    """FSM/CHARM procedure sections for the code, plus the sections they say to 'refer to' (one level)."""
    if vids:
        q = ",".join("?" * len(vids))
        rows = kb.execute(f"""SELECT s.id, s.path, s.page, s.body, d.kind FROM dtc_procedure p
            JOIN section s ON s.id=p.section_id JOIN doc d ON d.id=s.doc_id
            WHERE p.code=? AND p.vehicle_id IN ({q}) GROUP BY s.id ORDER BY d.kind='charm', s.id""",
                          [code] + vids).fetchall()
    else:
        rows = []
    out, seen, used = [], set(), 0
    for sid, path, page, body, kind in rows:
        for i, s2 in enumerate([(sid, path, page, body, kind)] + [
                kb.execute("SELECT s.id, s.path, s.page, s.body, d.kind FROM section s JOIN doc d ON d.id=s.doc_id "
                           "WHERE s.id=?", (to,)).fetchone()
                for (to,) in kb.execute("SELECT to_id FROM section_ref WHERE from_id=? AND to_id IS NOT NULL", (sid,))]):
            if not s2 or s2[0] in seen or used >= budget:
                continue
            seen.add(s2[0])
            text = s2[3][:max(400, budget - used)]
            used += len(text)
            out.append({"kind": "CHARM" if s2[4] == "charm" else "FSM",
                        "label": s2[1] + (f", p.{s2[2]}" if s2[2] else "")
                                 + (f" (the {code} procedure refers here)" if i else ""), "text": text})
    return out


CODE_RE = re.compile(r"\b[PBCU][0-9][0-9A-F]{3}\b")


def forum_counts(work, code, model, y0, y1, slack, min_car=3):
    """Confirmed fixes per cause bucket for this model+years and for every Subaru, plus one ranking.
    A bucket is ranked by this car's count only when at least min_car owners of this car confirmed it;
    otherwise every Subaru's count decides, so one-off threads don't outrank the common fixes."""
    if not work:
        return {"this_car": [], "all_subaru": [], "ranked": []}, []
    rows = work.execute("""SELECT t.url, t.title, e.model, e.year, e.fix, e.evidence, e.category, e.part,
            (upper(e.model)=upper(?) AND (e.year IS NULL OR e.year BETWEEN ? AND ?)) AS same_car,
            e.confirmed='op_confirmed' AS op, t.score
        FROM extract_code c JOIN extract e USING(src, tid) JOIN thread t USING(src, tid)
        WHERE c.code=? AND e.relevant AND e.evidence_ok AND e.confirmed IN ('op_confirmed','other_confirmed')
          AND e.category NOT IN ('other', 'unknown')""", (model, y0 - slack, y1 + slack, code)).fetchall()
    # a thread can list several codes; a fix that names other codes but not this one belongs to those
    rows = [r for r in rows if code in (r[4] or "") or not set(CODE_RE.findall(r[4] or "")) - {code}]
    car, allc, parts = {}, {}, {}
    for r in rows:
        allc[r[6]] = allc.get(r[6], 0) + 1
        if r[8]:
            car[r[6]] = car.get(r[6], 0) + 1
            if r[7]:
                parts.setdefault(r[6], set()).add(r[7])

    def rank_key(b):
        n = car.get(b, 0)
        return (0, -n, b) if n >= min_car else (1, -allc[b], b)
    ranked = sorted(allc, key=rank_key)
    picked = []
    for b in ranked[:6]:  # up to 2 examples for each top bucket, this car first, at most 10
        ex = sorted((r for r in rows if r[6] == b), key=lambda r: (-r[8], -r[9], -(r[10] or 0)))[:2]
        picked += [r[:7] + (r[8],) for r in ex]
    counts = {"this_car": [{"cause": b, "confirmed_fixes": n, "parts": sorted(parts.get(b, []))[:5]}
                           for b, n in sorted(car.items(), key=lambda x: -x[1])[:10]],
              "all_subaru": [{"cause": b, "confirmed_fixes": n}
                             for b, n in sorted(allc.items(), key=lambda x: -x[1])[:10]],
              "ranked": ranked[:8]}
    return counts, picked[:10]


def build_sources(r, kb, work, code, a, y0, y1, vids):
    desc = (kb.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone() or [None])[0] or ""
    sources = []

    def add(kind, prefix, label, text, url=None):
        sid = f"{prefix}{sum(1 for s in sources if s['id'].startswith(prefix)) + 1}"
        sources.append({"id": sid, "kind": kind, "label": label, "text": text, "url": url})

    for s in fsm_sources(kb, code, vids, a.fsm_chars):
        add(s["kind"], "S", s["label"], s["text"])
    counts, examples = forum_counts(work, code, a.model, y0, y1, a.forum_year_slack, a.min_forum)
    for url, title, model, year, fix, ev, cat, same in examples:
        add("forum", "F", f"{model or '?'} {year or ''} | {title}"[:120],
            f"Fix: {fix}\nCause bucket: {cat}\nQuote: \"{ev}\"", url)
    if has_table(kb, "knowledge"):
        rows = kb.execute("""SELECT k.kind, k.model, k.year_min, k.title, k.body, k.url FROM knowledge_fts f
            JOIN knowledge k ON k.id=f.rowid WHERE knowledge_fts MATCH ? AND upper(k.model)=upper(?)
            AND coalesce(k.year_max, 9999) >= ? AND coalesce(k.year_min, 0) <= ? AND k.kind != 'forum_fix'
            ORDER BY k.kind='nhtsa_complaint', bm25(knowledge_fts) LIMIT ?""",
                          (fts_terms(code, desc), a.model, y0, y1, a.knowledge_k)).fetchall()
        for kind, model, yr, title, body, url in rows:
            add(kind, "K", f"{kind} {model} {yr}: {title}"[:120], body[:700], url)
    return desc, counts, sources


FTS_STOP = {"the", "and", "detected", "malfunction", "circuit", "system", "range", "performance", "problem",
            "low", "high", "input", "signal", "bank", "too"}


def fts_terms(code, desc):
    """The code itself, OR every content word of its description together
    ('cylinder AND misfire', so brake 'master cylinder' TSBs don't match P0303)."""
    words = [w for w in dict.fromkeys(re.findall(r"[a-z]{3,}", desc.lower())) if w not in FTS_STOP][:4]
    q = f'"{code.lower()}"'
    if words:
        q += " OR (" + " AND ".join(f'"{w}"' for w in words) + ")"
    return q


def prompt_for(code, desc, a, y0, y1, counts, sources):
    lines = [f"CODE: {code} — {desc}", f"CAR: {a.make} {a.model} {y0}-{y1}" + (f", {a.engine}" if a.engine else ""), ""]
    lines.append(f"FORUM COUNTS (confirmed fixes for this car and every {a.make}, and the order to follow):")
    lines.append(json.dumps(counts, ensure_ascii=False))
    lines.append("")
    for s in sources:
        lines.append(f"[{s['id']}] ({s['kind']}) {s['label']}\n{s['text']}\n")
    return "\n".join(lines)


def verify_card(llm, card, by_id, counts=None, max_tokens=600):   # 200 could cut the reply off mid-JSON
    """Second pass: keep a cause/step only if its cited sources support it."""
    dropped = []

    def supported(claim, ids):
        cited = [by_id[i] for i in ids if i in by_id]
        if not cited:
            return "no", "cites no real source"
        text = "\n\n".join(f"[{s['id']}] {s['text'][:1500]}" for s in cited)
        if counts:   # 'N confirmed fixes' statements come from this table, not from the one thread cited
            text += "\n\n[FORUM COUNTS] confirmed fixes per cause: " + json.dumps(
                {"this_car": {x["cause"]: x["confirmed_fixes"] for x in counts.get("this_car", [])},
                 "all_subaru": {x["cause"]: x["confirmed_fixes"] for x in counts.get("all_subaru", [])}})
        res, _ = llm.json(VERIFY_SYSTEM, f"CLAIM: {claim}\n\nSOURCES:\n{text}", VERIFY_SCHEMA, max_tokens=max_tokens)
        return res["supported"], res["reason"]

    keep = []
    for c in card["causes"]:
        c["sources"] = [i for i in c["sources"] if i in by_id]
        verdict, why = supported(c["cause"], c["sources"])   # the cause itself, not the model's wording around it
        if verdict == "no":
            dropped.append({"cause": c["cause"], "reason": why})
        else:
            c["support"] = verdict
            keep.append(c)
    card["causes"] = keep
    steps = []
    for s in card["fsm_steps"]:
        s["sources"] = [i for i in s["sources"] if i in by_id]
        if len(re.findall(r"[A-Za-z]{2,}", s.get("step") or "")) < 3:   # bare step numbers/codes ("3", "10AH2/10AG2")
            dropped.append({"step": s.get("step"), "reason": "empty step"})
        elif s["sources"]:
            steps.append(s)
        else:
            dropped.append({"step": s["step"], "reason": "cites no real source"})
    card["fsm_steps"] = steps
    return card, dropped


# A number with a spec unit in model-written text: torque, size, pressure, volume, electrical, temperature.
SPEC_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?(?:\s*(?:-|–|to|±|\+/-)\s*\d+(?:[.,]\d+)?)?\s*"
    r"(?:N\s*[·.*]?\s*m|Nm|ft[\s.\-]*lbs?|lb[\s.\-]*ft|in[\s.\-]*lbs?|kgf?[\s.\-]*m|kgf|mm|cm|inch(?:es)?|in\.|"
    r"psi|kPa|MPa|bar|qts?|quarts?|liters?|litres?|ml|cc|ohms?|Ω|kΩ|volts?|mV|amps?|°\s*[CF]|"
    r"gal(?:lons?)?|oz|foot[\s\-]*pounds?|degrees?|deg|rpm|%)(?![A-Za-z])", re.I)
# OEM part-number shapes, every make the cards are baked for (formats only, no part is named):
PART_NO_RE = re.compile("|".join([
    r"\b\d{5}-?[A-Z]{2}[0-9A-Z]{3}\b",                          # Subaru 22401AA720
    r"\b\d{5}-[0-9A-Z]{5}(?:-[0-9A-Z]{2})?\b",                  # Toyota/Lexus, Nissan, Hyundai/Kia 5-5
    r"\b\d{5}-[A-Z0-9]{3}-[A-Z0-9]{3,4}\b",                     # Honda/Acura 5-3-3
    r"\b(?=[0-9]{0,3}[A-Z])[A-Z0-9]{4}-[A-Z0-9]{4,6}-[A-Z]{1,3}\b",   # Ford/Lincoln/Mercury prefix-base-suffix
    r"\b(?=[0-9]{0,3}[A-Z])[A-Z0-9]{4}-\d{2}-[A-Z0-9]{3,4}\b",      # Mazda
    r"\b\d{7,8}[A-Z]{2}\b",                                     # Mopar (Chrysler/Dodge/Jeep/Ram)
    r"\b\d{8}\b|\b\d{11}\b",                                   # GM 8 digits, BMW 11 digits
    r"\b\d{2} \d{2} \d \d{3} \d{3}\b",                          # BMW spaced
    r"\b(?=\d{0,2}[A-Z])[0-9][0-9A-Z]{2} ?\d{3} ?\d{3} ?[A-Z]{0,2}\b",   # VW/Audi 06A 905 115 D
    r"\bA ?\d{3} ?\d{3} ?\d{2} ?\d{2}\b",                         # Mercedes A 000 140 32 87
    r"\bMD\d{6}\b|\b\d{4}A\d{3}\b",                             # Mitsubishi
]))


def scrub_specs(card):
    """The model must not state specs; the deck copies them from the manual. Any it wrote anyway are
    replaced with a pointer to the manual and listed in dropped_json."""
    found = []

    def clean(text, where):
        if not isinstance(text, str):
            return text
        for rx, label in ((SPEC_RE, "[spec: see manual]"), (PART_NO_RE, "[part no.: see parts list]")):
            for m in rx.finditer(text):
                found.append({"spec_removed": m.group(0), "in": where})
            text = rx.sub(label, text)
        return text

    card["summary"] = clean(card.get("summary"), "summary")
    card["caution"] = clean(card.get("caution"), "caution")
    for c in card["causes"]:
        for k in ("cause", "why", "how_to_check"):
            c[k] = clean(c.get(k), c.get("cause", "cause"))
        c["live_data"] = [clean(x, "live_data") for x in c.get("live_data", [])]
    for s in card["fsm_steps"]:
        s["step"] = (clean(s.get("step"), "fsm_steps") or "").lstrip(" :;-–—.")
    return found


def render(code, desc, card, sources, specs, labor):
    by_id = {s["id"]: s for s in sources}
    for item in card["causes"] + card["fsm_steps"]:   # only real source ids are ever printed
        item["sources"] = [i for i in item["sources"] if i in by_id]
    out = [f"{code}: {desc}", card["summary"], ""]
    if card["causes"]:
        out.append("Likely causes, most common first:")
        for n, c in enumerate(card["causes"], 1):
            mark = "" if c.get("support", "yes") == "yes" else " (partly supported)"
            out.append(f"{n}. {c['cause']}{mark} [{', '.join(c['sources'])}]")
            out.append(f"   Why: {c['why']}")
            out.append(f"   Check: {c['how_to_check']}")
            if c["live_data"]:
                out.append(f"   Watch: {'; '.join(c['live_data'])}")
    if card["fsm_steps"]:
        out += ["", "Manual procedure outline:"]
        out += [f"- {s['step']} [{', '.join(s['sources'])}]" for s in card["fsm_steps"]]
    if specs:
        out += ["", "Specs (from the manual, as printed):"] + [f"- {x}" for x in specs]
    if labor:
        out += ["", "Book labor / parts (CHARM):"] + [f"- {x}" for x in labor]
    if card.get("caution"):
        out += ["", f"Caution: {card['caution']}"]
    cited = {i for c in card["causes"] for i in c["sources"]} | {i for s in card["fsm_steps"] for i in s["sources"]}
    out += ["", "Sources:"]
    for i in sorted(cited, key=lambda x: (x[0], int(x[1:]))):
        s = by_id[i]
        out.append(f"[{i}] {s['label']}" + (f" {s['url']}" if s.get("url") else ""))
    return "\n".join(out)


def codes_for(kb, work, a, y0, y1, vids):
    if a.codes:
        return [c.strip().upper() for c in a.codes.split(",") if c.strip()]
    codes = []
    if work:  # codes owners of this car post about most come first (so --limit tries the useful ones)
        codes += [r[0] for r in work.execute(
            """SELECT c.code FROM extract_code c JOIN extract e USING(src, tid)
               WHERE upper(e.model)=upper(?) AND (e.year IS NULL OR e.year BETWEEN ? AND ?)
                 AND e.confirmed IN ('op_confirmed','other_confirmed') AND e.evidence_ok
               GROUP BY c.code HAVING count(*) >= ? ORDER BY count(*) DESC""",
            (a.model, y0 - a.forum_year_slack, y1 + a.forum_year_slack, a.min_forum))]
    if vids:  # then every code the manual has a procedure for
        q = ",".join("?" * len(vids))
        codes += [r[0] for r in kb.execute(f"SELECT DISTINCT code FROM dtc_procedure WHERE vehicle_id IN ({q}) "
                                           "ORDER BY code", vids)]
    known = {r[0] for r in kb.execute("SELECT code FROM dtc")}
    return [c for c in dict.fromkeys(codes) if c in known]


# ------------------------------------------------------------------ main steps
def step_bake(a):
    y0, y1 = (int(x) for x in a.years.split("-")) if "-" in a.years else (int(a.years),) * 2
    kb = ro(a.kb)
    work = ro(a.work) if os.path.exists(os.path.expanduser(a.work)) else None
    if not work:
        print(f"note: no work DB at {a.work}; cards will use the manual and TSBs only", file=sys.stderr)
    Retriever = load_retriever(a.kb, a.kb_dir)
    r = Retriever(a.kb, model=a.model, year=y1, engine=None)
    vids = vehicle_ids(kb, a.model, y0, y1, a.engine)
    out = sqlite3.connect(os.path.expanduser(a.out))
    out.executescript(CARD_SCHEMA_SQL)
    llm = LLM(a.url, temperature=0.2, max_tokens=2000)
    if not llm.ping():
        sys.exit(f"llama-server not answering at {a.url} (start serve_cards.sh first)")

    codes = codes_for(kb, work, a, y0, y1, vids)
    if not a.force:
        have = {row[0] for row in out.execute("SELECT code FROM dtc_card WHERE upper(model)=upper(?) AND year_min=? "
                                              "AND year_max=?", (a.model, y0, y1))}
        codes = [c for c in codes if c not in have]
    if a.limit:
        codes = codes[:a.limit]
    print(f"bake: {len(codes)} cards for {a.model} {y0}-{y1}")
    t_start = time.time()
    failed = []
    for n, code in enumerate(codes, 1):
        t0 = time.time()
        try:   # one bad reply, server hiccup or odd DB row skips this card (retried next run), never the bake
            desc, counts, sources = build_sources(r, kb, work, code, a, y0, y1, vids)
            if not sources:
                print(f"  {code}: no sources, skipped")
                continue
            by_id = {s["id"]: s for s in sources}
            card, _ = llm.json(SYSTEM, prompt_for(code, desc, a, y0, y1, counts, sources), CARD_JSON_SCHEMA)
            for st in card["fsm_steps"]:
                st["step"] = st.pop("check", st.get("step", ""))
            dropped = []
            if not a.no_verify:
                card, dropped = verify_card(llm, card, by_id, counts)
            for item in card["causes"] + card["fsm_steps"]:   # invented ids like "S9" never reach the card
                item["sources"] = [i for i in item["sources"] if i in by_id]
        except Exception as e:
            failed.append(code)
            print(f"  {n}/{len(codes)} {code}: FAILED, not saved ({e})", flush=True)
            continue
        dropped += scrub_specs(card)
        # verbatim facts from the DB, for the top causes (never model-written)
        specs, labor = [], []
        for c in card["causes"][:3]:
            specs += r.specs(c["cause"], limit=2)
            if has_table(kb, "labor"):
                labor += r.labor(c["cause"], limit=2)
        specs, labor = list(dict.fromkeys(specs))[:6], list(dict.fromkeys(labor))[:6]
        text = render(code, desc, card, sources, specs, labor)
        out.execute("INSERT OR REPLACE INTO dtc_card VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            code, a.model, y0, y1, a.engine, f"{code}: {desc}", text, json.dumps(card, ensure_ascii=False),
            json.dumps([{k: s[k] for k in ("id", "kind", "label", "url")} for s in sources], ensure_ascii=False),
            json.dumps(counts, ensure_ascii=False), json.dumps(dropped, ensure_ascii=False), a.model_name,
            time.strftime("%Y-%m-%dT%H:%M:%S")))
        out.commit()
        el = time.time() - t_start
        print(f"  {n}/{len(codes)} {code}: {len(card['causes'])} causes, {len(dropped)} dropped, "
              f"{time.time() - t0:.0f}s  (ETA {(len(codes) - n) * el / n / 3600:.1f} h)", flush=True)
    if failed:
        print(f"bake: {len(failed)} cards failed and will be retried on the next run: {', '.join(failed)}")


def step_show(a):
    out = ro(a.out)
    for (text,) in out.execute("SELECT text FROM dtc_card WHERE code=? AND upper(model)=upper(?)",
                               (a.code.upper(), a.model)):
        print(text, "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", default=DEFAULT_KB)
    ap.add_argument("--kb-dir", help="folder with kb_retriever.py (default: next to --kb)")
    ap.add_argument("--work", default=DEFAULT_WORK, help="extract_fixes.py work DB")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--model", required=True, help="e.g. Outback; the car is always picked at run time")
    ap.add_argument("--make", default="Subaru", help="the make the card prompt names (default Subaru)")
    if len(sys.argv) > 1 and sys.argv[1] == "show":
        ap.add_argument("cmd")
        ap.add_argument("code")
        step_show(ap.parse_args())
        return
    ap.add_argument("--years", required=True, help="card covers these model years, e.g. 2005-2009")
    ap.add_argument("--engine", help="e.g. EJ251; default every engine")
    ap.add_argument("--codes", help="comma list; default every code with a manual procedure or forum fixes")
    ap.add_argument("--min-forum", type=int, default=3, help="forum-only codes need this many confirmed fixes")
    ap.add_argument("--forum-year-slack", type=int, default=1)
    ap.add_argument("--fsm-chars", type=int, default=7000)
    ap.add_argument("--knowledge-k", type=int, default=5)
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--model-name", default="Qwen3.6-35B-A3B-Q4_K_M", help="recorded with each card")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="rebuild cards that already exist")
    ap.add_argument("--no-verify", action="store_true", help="skip the source check (faster, less safe)")
    a = ap.parse_args()
    if a.make.lower() != "subaru":   # the prompt names the make; Subaru keeps the original wording
        global SYSTEM
        SYSTEM = SYSTEM.replace("Subaru", a.make)
    step_bake(a)


if __name__ == "__main__":
    main()
