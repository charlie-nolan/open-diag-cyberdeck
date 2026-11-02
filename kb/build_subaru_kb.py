#!/usr/bin/env python3
"""
Build subaru.db: one read-only knowledge DB for the cyberdeck (Subaru 1990-2012).

Steps (run in order; each is safe to re-run):
  fsm    Parse a marker FSM markdown file into procedure-level sections, specs, DTC links, cross refs.
         python build_subaru_kb.py fsm "<FSM>.md" --model Forester --chassis SF --years 2002 \
             --engines EJ251 --title "USDM Forester FSM 2002 G8050BE"
  diag   Pull DTC descriptions, SSM params, TSBs, NHTSA complaints/recalls, forum fixes
         from subaru_diag.db (built by build_subaru_diag_db.py). Re-run after that DB grows.
         python build_subaru_kb.py diag ~/subaru_cyberdeck/subaru_diag.db
  charm  Load Operation CHARM pages (the mounted lmdb-pages image) for Subaru 1990-2012 into the same
         section/spec tables, deduped by page body, plus labor times. Runs in the venv that has lmdb + bs4:
         ~/subaru_cyberdeck/venv/bin/python build_subaru_kb.py charm --charm ~/mnt/charm
  cards  Load pre-baked per-DTC cards from card_baking's cards.db (after diag, before embed/finish).
         python build_subaru_kb.py cards ~/subaru_cyberdeck/kb/cards.db
  labor  Rebuild the labor table from the stored CHARM pages (no mount needed).
  embed  (desktop, GPU) Qwen3-Embedding-0.6B, 1024-d, int8, into section_vec / knowledge_vec.
         Documents get no prefix; queries on the Pi must use the model's "query" prompt.
         Needs: pip install sentence-transformers sqlite-vec
  finish Optimize FTS, ANALYZE, VACUUM, switch to read-only-friendly journal mode.

Standard library only for fsm / diag / finish (charm needs lmdb + bs4) (runs on the Pi too).
Default output: subaru.db next to this script; override with --db.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import urllib.parse
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "subaru.db")
EMBED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBED_DIM = 1024
MAX_SECTION = 6000      # chars; bigger procedures are split at their numbered sub-items
TARGET_PART = 3500

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS vehicle (
  id INTEGER PRIMARY KEY,
  model TEXT NOT NULL, chassis TEXT, year INTEGER NOT NULL, engine TEXT,
  transmission TEXT, market TEXT NOT NULL DEFAULT 'USDM',
  UNIQUE (model, chassis, year, engine, market)
);

CREATE TABLE IF NOT EXISTS doc (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                 -- fsm, tsb, nhtsa, forum, romraider
  title TEXT NOT NULL UNIQUE,
  file_name TEXT, license TEXT
);

CREATE TABLE IF NOT EXISTS section (
  id INTEGER PRIMARY KEY,
  doc_id INTEGER NOT NULL REFERENCES doc(id),
  chapter TEXT,                       -- 'FUEL INJECTION (FUEL SYSTEMS)'
  topic TEXT,                         -- '3. Intake Manifold'
  fsm_code TEXT,                      -- 'S175034A18'
  title TEXT NOT NULL,                -- 'A: REMOVAL'
  path TEXT NOT NULL,                 -- chapter > topic > title
  part INTEGER NOT NULL DEFAULT 1,    -- >1 when a long procedure was split
  page INTEGER,                       -- PDF page (1-based)
  body TEXT NOT NULL,
  images TEXT,                        -- JSON list of image file names
  body_hash BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS section_doc ON section(doc_id, chapter);
CREATE INDEX IF NOT EXISTS section_code ON section(fsm_code);
CREATE UNIQUE INDEX IF NOT EXISTS section_hash ON section(doc_id, body_hash);

CREATE TABLE IF NOT EXISTS section_vehicle (
  vehicle_id INTEGER NOT NULL REFERENCES vehicle(id),
  section_id INTEGER NOT NULL REFERENCES section(id),
  PRIMARY KEY (vehicle_id, section_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS section_ref (
  from_id INTEGER NOT NULL REFERENCES section(id),
  ref_text TEXT NOT NULL,             -- 'FU-52, RELEASING OF FUEL PRESSURE, OPERATION, Fuel.'
  to_id INTEGER REFERENCES section(id),
  PRIMARY KEY (from_id, ref_text)
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS section_fts USING fts5(
  title, path, body, content='section', content_rowid='id',
  tokenize="porter unicode61 remove_diacritics 2", prefix='2 3');

CREATE TABLE IF NOT EXISTS dtc (
  code TEXT PRIMARY KEY,              -- P0304
  module TEXT,                        -- engine, at, abs ...
  description TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dtc_ssm (       -- where the ECU flags this DTC (RomRaider), for SSM reads
  code TEXT NOT NULL, tmp_addr TEXT NOT NULL, mem_addr TEXT, bit INTEGER NOT NULL,
  PRIMARY KEY (code, tmp_addr, bit)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS dtc_procedure (
  code TEXT NOT NULL,
  vehicle_id INTEGER NOT NULL REFERENCES vehicle(id),
  section_id INTEGER NOT NULL REFERENCES section(id),
  PRIMARY KEY (code, vehicle_id, section_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS spec (
  id INTEGER PRIMARY KEY,
  section_id INTEGER NOT NULL REFERENCES section(id),
  kind TEXT NOT NULL,                 -- torque (more kinds later: capacity, clearance ...)
  component TEXT NOT NULL,            -- best-effort label from the line / sub-heading
  value_si REAL, tol_si REAL, unit_si TEXT,
  value_us REAL, tol_us REAL, unit_us TEXT,
  raw TEXT NOT NULL                   -- the FSM line it came from
);
CREATE INDEX IF NOT EXISTS spec_section ON spec(section_id);
CREATE VIRTUAL TABLE IF NOT EXISTS spec_fts USING fts5(
  component, context, tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS ssm_param (     -- read-only SSM parameters (RomRaider defs)
  id TEXT NOT NULL, ecu_id TEXT NOT NULL DEFAULT '', kind TEXT, name TEXT, description TEXT,
  address TEXT, length INTEGER, bit INTEGER, units TEXT, expr TEXT,
  PRIMARY KEY (id, ecu_id, units)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS knowledge (     -- TSBs, NHTSA complaints/recalls, forum fixes
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL, ref TEXT, model TEXT, year_min INTEGER, year_max INTEGER,
  title TEXT NOT NULL, body TEXT NOT NULL, url TEXT, confirmed INTEGER,
  UNIQUE (kind, ref, model, year_min)
);
CREATE INDEX IF NOT EXISTS knowledge_scope ON knowledge(model, year_min, year_max);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title, body, content='knowledge', content_rowid='id', tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS dtc_card (      -- pre-baked per-DTC diagnostic cards (card_baking/bake_cards.py)
  code TEXT NOT NULL, model TEXT NOT NULL, year_min INTEGER NOT NULL, year_max INTEGER NOT NULL,
  engine TEXT, title TEXT NOT NULL, text TEXT NOT NULL, card_json TEXT NOT NULL, sources_json TEXT NOT NULL,
  forum_counts_json TEXT, dropped_json TEXT, llm_model TEXT, built_at TEXT,
  UNIQUE (code, model, year_min, year_max)   -- rowid table (not WITHOUT ROWID) so FTS/vec can key on it
);
CREATE VIRTUAL TABLE IF NOT EXISTS dtc_card_fts USING fts5(
  title, text, content='dtc_card', tokenize='porter unicode61');

CREATE TABLE IF NOT EXISTS charm_source (  -- provenance: the first CHARM key each stored page came from
  section_id INTEGER PRIMARY KEY REFERENCES section(id),
  source_key TEXT NOT NULL, source_ptr TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS labor (         -- CHARM labor-time / parts tables, one row per table row
  id INTEGER PRIMARY KEY,
  section_id INTEGER NOT NULL REFERENCES section(id),
  operation TEXT NOT NULL, hours REAL, part_number TEXT, price TEXT, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS labor_section ON labor(section_id);
"""

# ------------------------------------------------------------------ text cleanup
CODE_RE = re.compile(r"\s*\b(S[A-Z]?\d{5,}[A-Z0-9]*)\s*$")
PAGE_RE = re.compile(r'<span id="page-(\d+)-\d+"></span>')
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
IMG_PAGE_RE = re.compile(r"\]\(_page_(\d+)_")
PROC_RE = re.compile(r"^([A-Z]{1,2}):\s+\S")
TOPIC_RE = re.compile(r"^(\d{1,2})\.(?:\s+|(?=[A-Z][a-z]))(.+)$")   # "24. Wheel Bearing" and marker's "24.Wheel Bearing"
SUBITEM_RE = re.compile(r"^(\d{1,2})[.)]\s+[A-Z0-9(]")
DTC_TITLE_RE = re.compile(r"\bDTC\s+([PBCU][0-9A-F]{4})\b")
REF_RE = re.compile(r"<Ref\. to ([^>]{3,200}?)\.?\s*>")
TORQUE_RE = re.compile(          # 44 N·m (4.5 kgf-m, 33 ft-lb) / 30±5 N·m (3.1±0.5 kg·m, 22.4±3.6 ft·lb) / 118 N m (...)
    r"(?:T\d*\s*:\s*)?(\d+(?:\.\d+)?)\s*(?:±\s*(\d+(?:\.\d+)?))?\s*N\s?[·.]?\s?m\s*\(\s*[\d.]+\s*(?:±\s*[\d.]+)?\s*kgf?\s?[-·.]?\s?m,\s*"
    r"(\d+(?:\.\d+)?)\s*(?:±\s*(\d+(?:\.\d+)?))?\s*ft\s?[-·.]?\s?lb\)")


def clean_inline(s):
    s = PAGE_RE.sub("", s)
    s = re.sub(r"\[([^\]]*)\]\(#page-[^)]*\)", r"\1", s)          # [47,](#page-229-0) -> 47,
    s = re.sub(r"</?math[^>]*>", "", s)
    s = re.sub(r"\$\s*([^$]{1,40}?)\s*\$", r"\1", s)                # $32 \pm 10$ -> 32 \pm 10
    s = s.replace("\\pm", "±").replace("\\times", "×").replace("\\circ", "°")
    s = re.sub(r"<br\s*/?>", "; ", s)
    s = re.sub(r"</?(b|i|u|sup|sub|strong|em)>", "", s)
    s = s.replace("**", "")
    s = re.sub(r"-{4,}", "---", s)                                   # marker table rules can be 200k dashes
    s = re.sub(r"(\d)\s*±\s*(\d)", r"\1±\2", s)
    return html.unescape(s)


def clean_heading(line):
    h = clean_inline(line.lstrip("#").strip()).strip()
    m = CODE_RE.search(h)
    code = m.group(1) if m else None
    if m:
        h = h[:m.start()].strip()
    return h.strip(" *"), code


def classify(h):
    if not h:
        return "label"
    if PROC_RE.match(h):
        return "proc"
    m = TOPIC_RE.match(h)
    if m and re.search(r"[a-z]", m.group(2)):
        return "topic"
    if SUBITEM_RE.match(h):
        return "subitem"
    if h.endswith(":") or not re.search(r"[A-Z]", h):
        return "label"
    letters = re.sub(r"[^A-Za-z]", "", h)
    if len(letters) >= 4 and letters.isupper():
        return "caps"
    return "label"


def clean_body(lines):
    text = "\n".join(lines)
    images = IMG_RE.findall(text)
    text = IMG_RE.sub("[fig]", text)
    out = []
    for ln in text.split("\n"):
        if ln.startswith("#"):
            h, code = clean_heading(ln)
            ln = f"### {h}" + (f" ({code})" if code else "") if h else ""
        else:
            ln = clean_inline(ln).rstrip()
        out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"(\[fig\]\s*){2,}", "[fig] ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, images


# ------------------------------------------------------------------ FSM parsing
def parse_fsm(md_text):
    """Yield dicts: chapter, topic, title, code, page, lines, subitem_starts (line idx)."""
    raw = md_text.split("\n")
    heads = []                      # (line_no, kind, text, code)
    for i, ln in enumerate(raw):
        if ln.startswith("#"):
            h, code = clean_heading(ln)
            heads.append((i, classify(h), h, code))

    # A CAPS heading (no section code) is a chapter only when the next structural heading
    # is topic "1." -- topic numbering restarts in every chapter.
    chapter_lines = set()
    for j, (i, kind, h, code) in enumerate(heads):
        if kind != "caps" or code:
            continue
        for _, k2, h2, _ in heads[j + 1:j + 6]:
            if k2 in ("topic", "proc", "caps"):
                if k2 == "topic" and TOPIC_RE.match(h2).group(1) == "1":
                    chapter_lines.add(i)
                break

    # Topic numbering restarts at "1." in every chapter. When it restarts and no chapter heading was
    # found above, the nearest CAPS heading since the previous topic names the new chapter.
    last_caps, prev_num = None, 0
    for i, kind, h, code in heads:
        if kind == "caps" and not code:
            last_caps = i
        elif kind == "topic":
            num = int(TOPIC_RE.match(h).group(1))
            if num == 1 and prev_num > 1 and last_caps is not None and not any(
                    last_caps <= c < i for c in chapter_lines):
                chapter_lines.add(last_caps)
            prev_num, last_caps = num, None

    head_at = {i: (kind, h, code) for i, kind, h, code in heads}
    chapter = topic = None
    topic_code = None
    cur = None
    page = 1
    sections = []

    def close():
        if cur and any(l.strip() for l in cur["lines"][1:]):
            sections.append(cur)

    for i, ln in enumerate(raw):
        for m in PAGE_RE.finditer(ln):
            page = int(m.group(1)) + 1
        for m in IMG_PAGE_RE.finditer(ln):     # marker image names carry the absolute page index
            page = max(page, int(m.group(1)) + 1)
        if i in chapter_lines:
            close()
            chapter, topic, cur = head_at[i][1], None, None
            continue
        kind = head_at.get(i, (None,))[0]
        if kind == "topic":
            close()
            topic, topic_code = head_at[i][1], head_at[i][2]
            cur = dict(chapter=chapter, topic=topic, title=topic, code=topic_code, page=page,
                       lines=[ln], subitems=[])
            continue
        if kind == "proc":
            close()
            cur = dict(chapter=chapter, topic=topic, title=head_at[i][1],
                       code=head_at[i][2] or topic_code, page=page, lines=[ln], subitems=[])
            continue
        if cur is None:
            cur = dict(chapter=chapter, topic=topic, title=topic or chapter or "Front matter",
                       code=None, page=page, lines=[], subitems=[])
        if kind == "subitem":
            cur["subitems"].append(len(cur["lines"]))
        cur["lines"].append(ln)
    close()
    return sections


def split_long(sec):
    """Split a long procedure at its numbered sub-items (or paragraphs) into parts."""
    lines = sec["lines"]
    if len("\n".join(lines)) <= MAX_SECTION:
        return [(1, None, lines)]
    cuts = [0] + [s for s in sec["subitems"] if s > 0] + [len(lines)]
    blocks = [lines[a:b] for a, b in zip(cuts, cuts[1:]) if b > a]
    # paragraph fallback for blocks that are still huge
    fine = []
    for b in blocks:
        if len("\n".join(b)) <= MAX_SECTION:
            fine.append(b)
            continue
        acc = []
        for ln in b:
            acc.append(ln)
            size = len("\n".join(acc))
            if (not ln.strip() and size > TARGET_PART) or size > MAX_SECTION:
                fine.append(acc)
                acc = []
        if acc:
            fine.append(acc)
    parts, acc = [], []
    for b in fine:
        if acc and len("\n".join(acc + b)) > TARGET_PART:
            parts.append(acc)
            acc = []
        acc = acc + b
    if acc:
        parts.append(acc)
    out = []
    for n, p in enumerate(parts, 1):
        sub = None
        first = next((l for l in p if l.startswith("#")), None)
        if n > 1 and first:
            sub = clean_heading(first)[0]
        out.append((n, sub, p if n == 1 else lines[:1] + [f"(continued, part {n})"] + p))
    return out


def component_for(line, before, lines, idx, topic):
    """Best-effort label for a torque value."""
    txt = re.sub(r"Tightening torque\s*(\([^)]*\))?\s*:?", "", before, flags=re.I)
    txt = re.sub(r"^[\s|;:\-–—]+|[\s|;:\-–—]+$", "", txt)
    cells = [c.strip() for c in txt.split("|") if c.strip() and not re.fullmatch(r"\(?\d+\)?|T\d*", c.strip())]
    if cells:
        txt = cells[-1]
    if len(re.sub(r"[^A-Za-z]", "", txt)) >= 4 and not _generic(txt):
        return txt[:120]
    for back in range(idx - 1, max(-1, idx - 8), -1):
        prev = lines[back].strip().strip("#*- ").strip()
        prev = re.sub(r"^\d+\)\s*", "", prev)
        if (len(re.sub(r"[^A-Za-z]", "", prev)) >= 4 and "torque" not in prev.lower()
                and not re.fullmatch(r"(NOTE|CAUTION|WARNING|MEMO)S?:?", prev)
                and "N·m" not in prev and prev != "[fig]"):
            return (topic if _generic(prev) else prev)[:120]
    return topic[:120]


def _generic(label):
    return bool(PROC_RE.match(label) or re.search(r"reverse order|^\(?continued", label, re.I))


def step_fsm(conn, a):
    md_text = open(a.markdown, encoding="utf-8", errors="replace").read()
    years = [int(y) for y in a.years.split(",")]
    engines = a.engines.split(",")
    vids = []
    for model in a.model.split(","):            # Legacy/Outback manuals cover both: --model Legacy,Outback
        for y in years:
            for e in engines:
                conn.execute("INSERT OR IGNORE INTO vehicle(model,chassis,year,engine) VALUES (?,?,?,?)",
                             (model, a.chassis, y, e))
                vids.append(conn.execute("SELECT id FROM vehicle WHERE model=? AND chassis=? AND year=? AND engine=?",
                                         (model, a.chassis, y, e)).fetchone()[0])
    conn.execute("INSERT OR IGNORE INTO doc(kind,title,file_name,license) VALUES ('fsm',?,?,?)",
                 (a.title, os.path.basename(a.markdown), "Subaru FSM, personal use"))
    doc_id = conn.execute("SELECT id FROM doc WHERE title=?", (a.title,)).fetchone()[0]

    # Re-running replaces this document's rows.
    old = [r[0] for r in conn.execute("SELECT id FROM section WHERE doc_id=?", (doc_id,))]
    if old:
        q = ",".join("?" * len(old))
        for t, col in (("section_ref", "from_id"), ("dtc_procedure", "section_id"),
                       ("section_vehicle", "section_id")):
            conn.execute(f"DELETE FROM {t} WHERE {col} IN ({q})", old)
        conn.execute(f"DELETE FROM spec WHERE section_id IN ({q})", old)
        conn.execute(f"DELETE FROM section WHERE id IN ({q})", old)

    stats = dict(raw=0, kept=0, dup=0, parts=0, specs=0, dtc=0, refs=0)
    seen = set()
    for sec in parse_fsm(md_text):
        stats["raw"] += 1
        pieces = []
        for part, sub, lines in split_long(sec):
            body, images = clean_body(lines)
            chunks = charm_parts(body)               # hard cap: tables can defeat the sub-item split
            for k, chunk in enumerate(chunks):
                pieces.append((len(pieces) + 1, sub if k == 0 else (sub or "") + f" (cont. {k + 1})",
                               chunk if k == 0 else f"(continued)\n{chunk}", images if k == 0 else []))
        for part, sub, body, images in pieces:
            if len(re.sub(r"^#.*$|\[fig\]", "", body, flags=re.M).strip()) < 25:
                continue
            h = hashlib.blake2b(body.encode(), digest_size=8).digest()
            if h in seen:
                stats["dup"] += 1
                continue
            seen.add(h)
            title = sec["title"] + (f" — {sub}" if sub else "")
            path = " > ".join(x for x in (sec["chapter"], sec["topic"] if sec["topic"] != sec["title"] else None,
                                          title) if x)
            cur = conn.execute(
                "INSERT INTO section(doc_id,chapter,topic,fsm_code,title,path,part,page,body,images,body_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, sec["chapter"], sec["topic"], sec["code"], title, path, part, sec["page"], body,
                 json.dumps(images) if images else None, h))
            sid = cur.lastrowid
            stats["kept"] += 1
            stats["parts"] += part > 1
            conn.executemany("INSERT OR IGNORE INTO section_vehicle VALUES (?,?)", [(v, sid) for v in vids])

            m = DTC_TITLE_RE.search(sec["title"])
            if m:
                conn.execute("INSERT OR IGNORE INTO dtc(code) VALUES (?)", (m.group(1),))
                conn.executemany("INSERT OR IGNORE INTO dtc_procedure VALUES (?,?,?)",
                                 [(m.group(1), v, sid) for v in vids])
                stats["dtc"] += 1

            for r in set(REF_RE.findall(body)):
                conn.execute("INSERT OR IGNORE INTO section_ref(from_id, ref_text) VALUES (?,?)",
                             (sid, re.sub(r"\s+", " ", r).strip()))
                stats["refs"] += 1

            blines = body.split("\n")
            for li, ln in enumerate(blines):
                for tm in TORQUE_RE.finditer(ln):
                    comp = component_for(ln, ln[:tm.start()], blines, li,
                                         re.sub(r"^\d+\.\s*", "", sec["topic"] or title))
                    conn.execute(
                        "INSERT INTO spec(section_id,kind,component,value_si,tol_si,unit_si,value_us,tol_us,unit_us,raw) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, "torque", comp, float(tm.group(1)), float(tm.group(2)) if tm.group(2) else None,
                         "N·m", float(tm.group(3)), float(tm.group(4)) if tm.group(4) else None, "ft-lb",
                         ln.strip()[:400]))
                    stats["specs"] += 1

    resolve_refs(conn, doc_id)
    conn.execute("DELETE FROM spec_fts")
    conn.execute("INSERT INTO spec_fts(rowid, component, context) "
                 "SELECT p.id, p.component, s.topic || ' ' || s.title FROM spec p JOIN section s ON s.id=p.section_id")
    conn.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
    conn.commit()
    resolved = conn.execute("SELECT count(*), count(to_id) FROM section_ref").fetchone()
    print(f"fsm: {stats['raw']} raw sections -> {stats['kept']} stored ({stats['parts']} split parts, "
          f"{stats['dup']} duplicates dropped); {stats['specs']} torque specs; {stats['dtc']} DTC sections; "
          f"cross refs {resolved[1]}/{resolved[0]} resolved")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def resolve_refs(conn, doc_id):
    """'<Ref. to FU-52, RELEASING OF FUEL PRESSURE, OPERATION, Fuel.>' -> section id.
    Last field is the topic, second-to-last the procedure; match on those."""
    topics = {}
    for sid, topic, title, part in conn.execute(
            "SELECT id, topic, title, part FROM section WHERE doc_id=? ORDER BY id", (doc_id,)):
        t = _norm(re.sub(r"^\d+\.\s*", "", topic or ""))
        topics.setdefault(t, []).append((sid, _norm(re.sub(r"^[A-Z]{1,2}:\s*", "", title)), part))
    rows = conn.execute("SELECT from_id, ref_text FROM section_ref r JOIN section s ON s.id=r.from_id "
                        "WHERE s.doc_id=?", (doc_id,)).fetchall()
    for from_id, ref in rows:
        fields = [f.strip() for f in ref.split(",") if f.strip()]
        if len(fields) < 2:
            continue
        cands = topics.get(_norm(fields[-1]))
        if not cands:
            continue
        want = _norm(fields[-2]) if len(fields) >= 3 else ""
        pick = next((sid for sid, t, p in cands if want and p == 1 and want in t), None)
        if pick is None:
            pick = cands[0][0]
        conn.execute("UPDATE section_ref SET to_id=? WHERE from_id=? AND ref_text=?", (pick, from_id, ref))


# ------------------------------------------------------------------ subaru_diag.db
def step_diag(conn, a):
    conn.execute("ATTACH DATABASE ? AS d",
                 (f"file:{urllib.parse.quote(os.path.abspath(a.diag_db))}?mode=ro",))
    have = {r[0] for r in conn.execute("SELECT name FROM d.sqlite_master WHERE type='table'")}
    n = {}
    if "dtc_subaru" in have:
        # FSM wording wins over RomRaider's; first row per code is kept.
        conn.execute("DELETE FROM dtc WHERE code NOT IN (SELECT code FROM dtc_procedure)")
        conn.execute("UPDATE dtc SET module=NULL, description=NULL")
        conn.execute("""INSERT INTO dtc(code, module, description)
                        SELECT code, module, description FROM d.dtc_subaru
                        ORDER BY fsm_ref IS NULL, id
                        ON CONFLICT(code) DO UPDATE SET
                             module=coalesce(dtc.module, excluded.module),
                             description=coalesce(dtc.description, excluded.description)""")
        conn.execute("DELETE FROM dtc_ssm")
        conn.execute("""INSERT OR IGNORE INTO dtc_ssm(code, tmp_addr, mem_addr, bit)
                        SELECT code, ssm_tmp_addr, ssm_mem_addr, ssm_bit FROM d.dtc_subaru
                        WHERE ssm_tmp_addr IS NOT NULL AND ssm_bit IS NOT NULL""")
        n["dtc"] = conn.execute("SELECT count(*) FROM dtc").fetchone()[0]
        n["dtc_ssm"] = conn.execute("SELECT count(*) FROM dtc_ssm").fetchone()[0]
    if "ssm_param" in have:
        conn.execute("DELETE FROM ssm_param")
        conn.execute("""INSERT OR REPLACE INTO ssm_param(id,ecu_id,kind,name,description,address,length,bit,units,expr)
                        SELECT id,ecu_id,kind,name,description,address,length,bit,units,expr FROM d.ssm_param""")
        n["ssm_param"] = conn.execute("SELECT count(*) FROM d.ssm_param").fetchone()[0]
    feeds = {
        "tsb": "SELECT 'tsb', nhtsa_id, model, model_year, model_year, "
               "'TSB ' || coalesce(bulletin_no,'') || ' (' || coalesce(comm_date,'') || '): ' || coalesce(components,''), "
               "summary, NULL, NULL FROM d.tsb",
        "complaint": "SELECT 'nhtsa_complaint', cmpl_id, model, model_year, model_year, component, summary, "
                     "NULL, NULL FROM d.complaint",
        "recall": "SELECT 'recall', campaign || ' ' || coalesce(component,''), model, model_year, model_year, component, "
                  "coalesce(defect,'') || char(10) || 'Consequence: ' || coalesce(consequence,'') || char(10) || "
                  "'Remedy: ' || coalesce(remedy,''), NULL, NULL FROM d.recall",
        "forum_fix": "SELECT 'forum_fix', id, model, year_from, year_to, title, summary, url, confirmed FROM d.forum_fix",
    }
    for t, sql in feeds.items():
        if t in have:
            conn.execute("DELETE FROM knowledge WHERE kind=?", (sql.split("'")[1],))
            conn.execute("INSERT OR REPLACE INTO knowledge(kind,ref,model,year_min,year_max,title,body,url,confirmed) "
                         + sql + " WHERE true")
            n[t] = conn.execute(f"SELECT count(*) FROM d.{t}").fetchone()[0]
    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    conn.commit()
    conn.execute("DETACH DATABASE d")
    print("diag:", ", ".join(f"{k} {v}" for k, v in n.items()))


# ------------------------------------------------------------------ Operation CHARM
CHARM_MAKES = ("Subaru", "Subaru Truck")
# Engine part of a CHARM model name: Subaru "F4-2.5L SOHC", "H6-3.0L"; GM/others "V8-5.3L", "L4-2.2L VIN F".
CHARM_ENGINE_RE = re.compile(r"\b([A-Z]{1,3}\d{1,2}-\d{1,2}\.\dL.*)$")
CHARM_NM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:N\s*[·.*]?\s*m|Nm)\b", re.I)
CHARM_FT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ft[\s.\-]*lbs?\.?|lb[\s.\-]*ft)", re.I)
CHARM_IN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*in[\s.\-]*lbs?\.?", re.I)
CHARM_HOURS_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s*$")
CHARM_PRICE_RE = re.compile(r"^\$?\d{1,5}(?:,\d{3})?\.\d{2}$")
CHARM_PARTNO_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{4,19}$")
CHARM_DTC_RE = re.compile(r"^\s*(?:DTC\s+)?([PBCU][0-9][0-9A-F]{3})\b")


def _toks(s):
    return re.sub(r"[-/]", " ", s.upper()).split()


def known_models(conn, nhtsa_db=None, makes=()):
    """Model names NHTSA uses for this make (TSB/complaint/recall rows): the make DB's own knowledge rows, else
    kb/nhtsa_all.db filtered by make. Longest first, as token lists. Used to tell the model apart from series codes
    and trims in CHARM's vehicle names ('K Tahoe 4WD' -> 'Tahoe', 'Monte Carlo SS' -> 'Monte Carlo')."""
    names = {r[0] for r in conn.execute("SELECT DISTINCT model FROM knowledge WHERE kind IN "
                                        "('tsb','nhtsa_complaint','recall') AND model IS NOT NULL")}
    if not names and nhtsa_db and os.path.isfile(nhtsa_db) and makes:
        n = sqlite3.connect(f"file:{nhtsa_db}?mode=ro", uri=True)
        q = ",".join("?" * len(makes))
        for t in ("tsb", "complaint", "recall"):
            try:
                names |= {r[0] for r in n.execute(f"SELECT DISTINCT model FROM {t} WHERE upper(make) IN ({q})",
                                                 [m.upper() for m in makes])}
            except sqlite3.Error:
                pass
        n.close()
    out = {tuple(_toks(x)) for x in names if x}
    # a bare number or one letter can't name a model on its own ('1500', 'C')
    out = [t for t in out if t and not (len(t) == 1 and (t[0].isdigit() or len(t[0]) < 2))]
    return sorted(out, key=lambda t: (-len(t), t))


def charm_vehicle(model_full, known=None):
    """'Forester L F4-2.5L SOHC' -> ('Forester', 'F4-2.5L SOHC'); 'Legacy Outback ...' -> 'Outback'.
    With known (from known_models): the longest NHTSA model name found as whole words in the CHARM name, in
    CHARM's spelling; with no match, or no list, the first word (the old rule, right for Subaru)."""
    m = CHARM_ENGINE_RE.search(model_full)
    engine = m.group(1).strip() if m else None
    name = model_full[:m.start()].strip() if m else model_full.strip()
    words = model_full.split()
    if words[:2] == ["Legacy", "Outback"]:
        return "Outback", engine
    if known:
        # CHARM spelling, split the same way as _toks so positions line up
        orig = re.sub(r"[-/]", " ", name).split()
        up = [w.upper() for w in orig]
        best = None
        for k in known:
            n = len(k)
            if best and n < len(best[1]):
                break
            for i in range(len(up) - n + 1):
                if tuple(up[i:i + n]) == k:
                    if best is None or i < best[0]:
                        best = (i, k)
                    break
        if best:
            i, k = best
            return " ".join(orig[i:i + len(k)]), engine
    return words[0], engine


def charm_text(soup):
    """Page text with each table row on one line ('cell | cell | cell')."""
    for t in soup(["script", "style", "button", "head", "title"]):
        t.decompose()
    rows = []
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if cells:
            rows.append(cells)
        tr.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n"))
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip(), rows


def charm_parts(body):
    """Split a long page at line boundaries into ~TARGET_PART pieces."""
    if len(body) <= MAX_SECTION:
        return [body]
    parts, acc = [], []
    for ln in body.split("\n"):
        if acc and len("\n".join(acc)) + len(ln) > TARGET_PART:
            parts.append("\n".join(acc))
            acc = []
        acc.append(ln[:MAX_SECTION])
    if acc:
        parts.append("\n".join(acc))
    return parts


def step_charm(conn, a):
    sys.path.insert(0, os.path.expanduser(a.charm_script))
    try:
        from bs4 import BeautifulSoup
        import charm_ingest as ci        # open_charm / enc_prefix / walk_vehicle / is_index_node / section_of
    except ImportError as e:
        sys.exit(f"charm needs lmdb, bs4 and charm_ingest.py (--charm-script): {e}")
    y0, y1 = (int(x) for x in a.years.split("-"))
    env = ci.open_charm(Path(os.path.expanduser(a.charm)))

    conn.execute("INSERT OR IGNORE INTO doc(kind,title,file_name,license) VALUES ('charm',?,?,?)",
                 (CHARM_TITLE, "lmdb-pages.sqsh", "Operation CHARM, personal use"))
    doc_id = conn.execute("SELECT id FROM doc WHERE title=?", (CHARM_TITLE,)).fetchone()[0]
    # Re-running replaces all CHARM rows.
    old = "SELECT id FROM section WHERE doc_id=?"
    for t, col in (("section_ref", "from_id"), ("dtc_procedure", "section_id"), ("section_vehicle", "section_id"),
                   ("spec", "section_id"), ("labor", "section_id"), ("charm_source", "section_id")):
        conn.execute(f"DELETE FROM {t} WHERE {col} IN ({old})", (doc_id,))
    conn.execute("DELETE FROM section WHERE doc_id=?", (doc_id,))
    conn.commit()

    known = known_models(conn, os.path.join(os.path.dirname(os.path.abspath(a.db)), "nhtsa_all.db"),
                         [m.strip() for m in a.makes.split(",") if m.strip()])
    by_hash = {}                          # body_hash -> section id (dedupe across vehicles)
    st = dict(vehicles=0, nodes=0, index=0, listing=0, image_only=0, empty=0, pages=0, new=0, specs=0, labor=0, dtc=0)
    for make in [m.strip() for m in a.makes.split(",") if m.strip()]:
        for year in range(y0, y1 + 1):
            yprefix = ci.enc_prefix(f"{make}/{year}")
            with env.begin() as txn:
                vid_by_model, vehicle_prefix = {}, None
                for key, ptr, payload in ci.walk_vehicle(txn, yprefix):
                    segs = key.split(b"/")
                    if len(segs) < 5 or not segs[3]:
                        continue
                    model_full = urllib.parse.unquote(segs[3].decode("utf-8", "replace"))
                    if a.only and a.only.lower() not in f"{year} {model_full}".lower():
                        continue
                    if model_full not in vid_by_model:
                        model, engine = charm_vehicle(model_full, known)
                        # chassis is NULL for CHARM rows, so UNIQUE can't dedupe them; look up first.
                        row = conn.execute("SELECT id FROM vehicle WHERE model=? AND chassis IS NULL AND year=? "
                                           "AND engine IS ? AND market='USDM'", (model, year, engine)).fetchone()
                        vid_by_model[model_full] = row[0] if row else conn.execute(
                            "INSERT INTO vehicle(model,chassis,year,engine,market) VALUES (?,NULL,?,?,'USDM')",
                            (model, year, engine)).lastrowid
                        st["vehicles"] += 1
                        print(f"  {make} {year} {model_full}", flush=True)
                    vid = vid_by_model[model_full]
                    st["nodes"] += 1
                    if not payload or ci.is_index_node(ptr):
                        st["index"] += 1
                        continue
                    if b"id='expand-all'" in payload[:300] or b'id="expand-all"' in payload[:300]:
                        st["listing"] += 1
                        continue
                    key_text = urllib.parse.unquote(key.decode("utf-8", "replace"))
                    crumbs = [c for c in key_text.strip("/").split("/")[3:] if c]
                    soup = BeautifulSoup(payload.decode("utf-8", "replace"), "lxml")
                    imgs = [i["src"] for i in soup.find_all("img", src=True)][:20]
                    body, rows = charm_text(soup)
                    if len(body) < 25 and not any(len(r) >= 2 for r in rows):
                        st["image_only" if imgs else "empty"] += 1
                        continue
                    st["pages"] += 1
                    section = ci.section_of(key_text)
                    title = crumbs[-1] if crumbs else model_full
                    path = " > ".join(crumbs)
                    # Lead each page with its component and section ("Timing Belt > Labor Times"). Short labor/parts
                    # pages are often identical text for different parts, so the component must be in the hashed
                    # text, but not the category above it: Engine > Timing Belt and Maintenance > Timing Belt are
                    # the same page and should stay one row.
                    head = " > ".join(crumbs[-2:])
                    parts = charm_parts(body)
                    for n, part in enumerate(parts, 1):
                        text = f"{head}\n{part}" if n == 1 else f"{head} (continued, part {n})\n{part}"
                        h = hashlib.blake2b(text.encode(), digest_size=8).digest()
                        sid = by_hash.get(h)
                        if sid is None:
                            sid = conn.execute(
                                "INSERT INTO section(doc_id,chapter,topic,fsm_code,title,path,part,page,body,images,body_hash) "
                                "VALUES (?,?,?,NULL,?,?,?,NULL,?,?,?)",
                                (doc_id, crumbs[0] if crumbs else None, crumbs[-2] if len(crumbs) > 1 else None,
                                 title, path, n, text, json.dumps(imgs) if imgs and n == 1 else None, h)).lastrowid
                            by_hash[h] = sid
                            st["new"] += 1
                            conn.execute("INSERT INTO charm_source VALUES (?,?,?)",
                                         (sid, key_text, ptr.decode("utf-8", "replace")))
                            if n == 1:
                                st["specs"] += charm_specs_labor(conn, sid, section, rows, st)
                        conn.execute("INSERT OR IGNORE INTO section_vehicle VALUES (?,?)", (vid, sid))
                        m = CHARM_DTC_RE.match(title)
                        if m and n == 1:
                            conn.execute("INSERT OR IGNORE INTO dtc(code) VALUES (?)", (m.group(1),))
                            conn.execute("INSERT OR IGNORE INTO dtc_procedure VALUES (?,?,?)", (m.group(1), vid, sid))
                            st["dtc"] += 1
                conn.commit()
    st["labor"] = labor_from_sections(conn)
    conn.execute("DELETE FROM spec_fts")
    conn.execute("INSERT INTO spec_fts(rowid, component, context) SELECT p.id, p.component, "
                 "coalesce(s.topic,'') || ' ' || s.title FROM spec p JOIN section s ON s.id=p.section_id")
    conn.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('charm_stats', ?)", (json.dumps(st),))
    conn.commit()
    print("charm:", ", ".join(f"{k} {v}" for k, v in st.items()))


CHARM_TITLE = "Operation CHARM (Subaru 1990-2012)"


LABOR_SKIP = {"labor times", "parts", "parts information", "parts and labor", "repair and diagnosis"}


def charm_specs_labor(conn, sid, section, rows, st):
    """Torque rows -> spec (as printed; nothing converted). Labor rows are built by labor_from_sections()."""
    if section.lower() in ("labor times", "parts"):
        return 0
    n = 0
    for cells in rows:
        if len(cells) < 2:
            continue
        raw = " | ".join(cells)[:400]
        nm, ft, inch = CHARM_NM_RE.search(raw), CHARM_FT_RE.search(raw), CHARM_IN_RE.search(raw)
        if not (nm or ft or inch):
            continue
        us, unit_us = (ft, "ft-lb") if ft else (inch, "in-lb") if inch else (None, None)
        conn.execute("INSERT INTO spec(section_id,kind,component,value_si,tol_si,unit_si,value_us,tol_us,unit_us,raw) "
                     "VALUES (?,?,?,?,NULL,?,?,NULL,?,?)",
                     (sid, "torque", cells[0][:200], float(nm.group(1)) if nm else None, "N·m" if nm else None,
                      float(us.group(1)) if us else None, unit_us, raw))
        n += 1
    return n


def labor_from_sections(conn):
    """Rebuild `labor` from stored CHARM labor/parts pages (their table rows are 'a | b | c' lines).
    Each operation carries its page path and table group, e.g. 'Exhaust > Exhaust Manifold > Gasket > Replace'.
    Rows of 0 h with no part number are group headers ("included in ..." rows), not operations."""
    conn.execute("DELETE FROM labor")
    n = 0
    for sid, path, body in conn.execute(
            "SELECT s.id, s.path, s.body FROM section s JOIN doc d ON d.id=s.doc_id WHERE d.kind='charm' "
            "AND (lower(s.path) LIKE '%labor times%' OR lower(s.path) LIKE '%> parts%' OR lower(s.path) LIKE 'parts%')"
    ).fetchall():
        crumbs = [c for c in path.split(" > ") if c.lower() not in LABOR_SKIP]
        labor_page = "labor times" in path.lower()
        group = None
        for line in body.split("\n")[1:]:          # line 0 is the page heading added by step_charm
            cells = [c.strip() for c in line.split(" | ") if c.strip()]
            if not cells:
                continue
            hours = part = price = None
            for c in cells[1:]:
                # letters or a dash, or 6+ digits: GM's numbers are all digits (19115080) and were dropped before.
                # Same rule as parts_sourcing/interchange.is_part_number.
                if CHARM_PARTNO_RE.match(c.replace(" ", "")) and re.search(r"\d", c) and (
                        re.search(r"[A-Z\-]", c) or len(c.replace(" ", "")) >= 6):
                    part = c.replace(" ", "")[:40]
            for c in cells[1:]:
                if "$" in c:
                    price = c[:40]
                elif (part or not labor_page) and CHARM_PRICE_RE.match(c):
                    # On parts pages (or any row with a part number) a bare decimal is a list price, not hours.
                    price = price or c.strip()
                elif labor_page and not part and CHARM_HOURS_RE.match(c) and hours is None:
                    hours = float(c)
            if len(cells) == 1 or (not hours and not part and not price):
                if len(cells[0]) < 80:
                    group = cells[0]          # table sub-heading for the rows below it
                continue
            segs = []
            for x in crumbs + ([group] if group else []) + [cells[0]]:
                if not segs or segs[-1].lower() != x.lower():
                    segs.append(x)
            op = " > ".join(segs)
            conn.execute("INSERT INTO labor(section_id,operation,hours,part_number,price,raw) VALUES (?,?,?,?,?,?)",
                         (sid, op[:300], hours, part, price, line[:400]))
            n += 1
    conn.commit()
    return n


def step_labor(conn, a):
    print(f"labor: {labor_from_sections(conn)} rows rebuilt from stored CHARM pages")



# ------------------------------------------------------------------ pre-baked DTC cards
CARD_COLS = ("code, model, year_min, year_max, engine, title, text, card_json, sources_json, "
             "forum_counts_json, dropped_json, llm_model, built_at")


def step_cards(conn, a):
    conn.execute("ATTACH DATABASE ? AS c", (f"file:{urllib.parse.quote(os.path.abspath(a.cards_db))}?mode=ro",))
    conn.execute("DELETE FROM dtc_card")
    conn.execute(f"INSERT INTO dtc_card({CARD_COLS}) SELECT {CARD_COLS} FROM c.dtc_card")
    conn.execute("INSERT INTO dtc_card_fts(dtc_card_fts) VALUES ('rebuild')")
    conn.commit()
    conn.execute("DETACH DATABASE c")
    n, codes = conn.execute("SELECT count(*), count(DISTINCT code) FROM dtc_card").fetchone()
    print(f"cards: {n} cards for {codes} codes")


# ------------------------------------------------------------------ embeddings (desktop)
# What each source is called in the text in front of its rows. A new source (e.g. community posts) adds a label here
# and a row query in embed_sources(); everything else (header, batching, int8 format) is shared.
SOURCE_LABEL = {"fsm": "factory service manual", "charm": "service manual (Operation CHARM)",
                "tsb": "technical service bulletin", "recall": "safety recall",
                "nhtsa_complaint": "owner complaint (NHTSA)", "forum": "forum repair report",
                "card": "diagnostic card", "community": "community repair report"}


def _years(ys):
    ys = sorted({y for y in ys if y})
    if not ys:
        return ""
    runs, lo = [], ys[0]
    for p, y in zip(ys, ys[1:] + [None]):
        if y != p + 1:
            runs.append(str(lo) if lo == p else f"{lo}-{p}")
            lo = y
    return ",".join(runs)


def cars_label(cars, most=3):
    """[(model, year, engine)] -> 'Forester 1999-2002 (EJ251); Impreza 2002 (+4 more models)'."""
    by = {}
    for model, year, engine in cars:
        m = by.setdefault(model or "", [set(), set()])
        m[0].add(year)
        if engine:
            m[1].add(engine)
    out = []
    for model in sorted(by, key=lambda k: -len(by[k][0]))[:most]:
        ys, es = by[model]
        eng = f" ({', '.join(sorted(es)[:3])}{', ...' if len(es) > 3 else ''})" if es else ""
        out.append(f"{model} {_years(ys)}{eng}".strip())
    more = len(by) - most
    return "; ".join(out) + (f" (+{more} more models)" if more > 0 else "")


def embed_header(make, source, cars="", where=""):
    """One short line in front of every embedded row: make, cars, source type, where it sits (system/section)."""
    bits = [f"{make} {cars}".strip(), SOURCE_LABEL.get(source, source), where]
    return " | ".join(b for b in bits if b)


def _drop_repeat(path, body):
    """CHARM bodies start with the last crumbs of their path; drop that line, which the header already holds."""
    first, _, rest = body.partition("\n")
    if rest and path.endswith(re.sub(r" \(continued, part \d+\)$", "", first.strip())):
        cont = re.search(r"\(continued, part \d+\)$", first)
        return (cont.group(0) + "\n" + rest) if cont else rest
    return body


def embed_sources(conn, make, body_chars, text):
    """(vec table, source table, key column, rows query, row -> text) for each embedded source."""
    def section(r):
        sid, path, body, kind, cars = r
        if text == "plain":
            return path + "\n" + body[:body_chars]
        cl = "" if text == "source" else cars_label(
            [(m, int(y) if y else None, e or None) for m, y, e in
             (c.split("\x1f") for c in (cars or "").split("\x1e") if c)])
        return embed_header(make, kind, cl, path) + "\n" + _drop_repeat(path, body)[:body_chars]

    def knowledge(r):
        kid, kind, model, y0, y1, title, body = r
        if text == "plain":
            return title + "\n" + body[:body_chars]
        yrs = _years(range(y0, (y1 or y0) + 1)) if y0 else ""
        cl = "" if text == "source" else f"{model or ''} {yrs}".strip()
        return embed_header(make, kind, cl, title) + "\n" + body[:body_chars]

    def card(r):
        rid, model, y0, y1, engine, title, body = r
        if text == "plain":
            return title + "\n" + body[:body_chars]
        cl = "" if text == "source" else f"{model} {_years(range(y0, y1 + 1))}" + (f" ({engine})" if engine else "")
        return embed_header(make, "card", cl, title) + "\n" + body[:body_chars]

    big = body_chars + 400      # read a little more than is kept: _drop_repeat may remove the first line
    return (
        ("section_vec", "section", "id",
         f"SELECT s.id, s.path, substr(s.body, 1, {big}), d.kind, (SELECT group_concat(v.model || char(31) || "
         "v.year || char(31) || coalesce(v.engine, ''), char(30)) FROM section_vehicle sv JOIN vehicle v "
         "ON v.id=sv.vehicle_id WHERE sv.section_id=s.id) FROM section s JOIN doc d ON d.id=s.doc_id", section),
        ("knowledge_vec", "knowledge", "id",
         f"SELECT id, kind, model, year_min, year_max, title, substr(body, 1, {big}) FROM knowledge", knowledge),
        ("card_vec", "dtc_card", "rowid",
         f"SELECT rowid, model, year_min, year_max, engine, title, substr(text, 1, {big}) FROM dtc_card", card))


def step_embed(conn, a):
    try:
        import sqlite_vec
        from embedder import DocEncoder
    except ImportError as e:
        sys.exit(f"embed needs sentence-transformers, numpy and sqlite-vec: {e}")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    make = a.make or os.path.splitext(os.path.basename(a.db))[0].replace("_", " ").title()
    fmt = f"{a.text}:{a.body_chars}"
    old = conn.execute("SELECT v FROM meta WHERE k='embed_text'").fetchone()
    if a.missing and old and old[0] != fmt:
        print(f"embed: WARNING existing vectors were made from text '{old[0]}', new ones from '{fmt}'; "
              "run without --missing to redo them all the same way", flush=True)
    enc = DocEncoder(EMBED_MODEL, device=a.device, dtype=a.dtype, max_tokens=a.max_tokens,
                     token_budget=a.token_budget)
    import time
    for table, src, key, sql, to_text in embed_sources(conn, make, a.body_chars, a.text):
        if a.only and src not in a.only.split(","):
            continue
        t0 = time.time()
        have = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if a.missing and have:
            # Keep existing vectors: drop ones whose row is gone (a reloaded manual), embed only new rows.
            live = {r[0] for r in conn.execute(f"SELECT {key} FROM {src}")}
            done = {r[0] for r in conn.execute(f"SELECT id FROM {table}")}
            conn.executemany(f"DELETE FROM {table} WHERE id=?", [(i,) for i in done - live])
            rows = [(r[0], to_text(r)) for r in conn.execute(sql) if r[0] not in done]
            print(f"embed: {table}: {len(done - live)} stale vectors removed, {len(rows)} new rows", flush=True)
        else:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"CREATE VIRTUAL TABLE {table} USING vec0(id INTEGER PRIMARY KEY, embedding int8[{EMBED_DIM}])")
            rows = [(r[0], to_text(r)) for r in conn.execute(sql)]
        conn.commit()
        n = 0
        # One transaction per chunk (~16k rows): a crash or Ctrl-C loses at most one chunk, and --missing resumes.
        for ids, vecs in enc.encode_rows(rows):
            conn.executemany(f"INSERT INTO {table}(id, embedding) VALUES (?, vec_int8(?))",
                             [(i, v.tobytes()) for i, v in zip(ids, vecs)])
            conn.commit()
            n += len(ids)
            dt = time.time() - t0
            print(f"embed: {table} {n}/{len(rows)} rows, {n / dt:.0f} rows/s", flush=True)
        print(f"embed: {len(rows)} rows -> {table} in {time.time() - t0:.0f} s", flush=True)
    if enc.device.type == "cuda":
        import torch
        print(f"embed: peak VRAM {torch.cuda.max_memory_allocated() / 2**20:.0f} MiB (torch allocations), "
              f"{enc.truncated} rows cut at {a.max_tokens} tokens", flush=True)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (f"{EMBED_MODEL}:{EMBED_DIM}:int8",))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_text', ?)", (fmt,))
    conn.commit()


def step_relink(conn, a):
    """Re-attach CHARM pages to cars named by known_models(), without re-reading or re-embedding the pages.
    Older builds named every car by the first word of CHARM's name ('K Tahoe 4WD' -> 'K'), which merged different
    models into one vehicle. This walks the CHARM keys again (each key points at a stored page through charm_source,
    or by the same text hash for pages that were deduplicated), then replaces vehicle, section_vehicle and
    dtc_procedure in one transaction. Sections, specs, labor, knowledge and vectors are untouched."""
    sys.path.insert(0, os.path.expanduser(a.charm_script))
    try:
        from bs4 import BeautifulSoup
        import charm_ingest as ci
    except ImportError as e:
        sys.exit(f"relink needs lmdb, bs4 and charm_ingest.py (--charm-script): {e}")
    root = Path(os.path.expanduser(a.charm))
    if not (root / "data.mdb").is_file():
        sys.exit(f"no data.mdb in {root}: mount the CHARM pages first (same mount the charm step uses)")
    doc = conn.execute("SELECT id FROM doc WHERE kind='charm'").fetchone()
    if not doc:
        sys.exit("no CHARM pages in this DB")
    makes = [m.strip() for m in a.makes.split(",") if m.strip()]
    known = known_models(conn, os.path.join(os.path.dirname(os.path.abspath(a.db)), "nhtsa_all.db"), makes)
    if not known:
        sys.exit("no NHTSA model names for this make (run add_nhtsa.py first); nothing changed")
    by_ptr = {}
    for sid, ptr in conn.execute("SELECT section_id, source_ptr FROM charm_source"):
        by_ptr.setdefault(ptr.encode(), []).append(sid)
    by_hash = {h: sid for sid, h in conn.execute("SELECT id, body_hash FROM section WHERE doc_id=?", (doc[0],))}
    title_of = dict(conn.execute("SELECT id, title FROM section WHERE doc_id=?", (doc[0],)))
    y0, y1 = (int(x) for x in a.years.split("-"))
    env = ci.open_charm(root)
    cars = {}                              # (model, year, engine) -> set of section ids
    names = {}                             # (model, year, engine) -> CHARM names merged into it
    st = dict(keys=0, by_ptr=0, by_hash=0, unmatched=0, parsed=0)
    miss = {}                              # (page bytes hash, heading) -> section ids found by text hash ([] = no stored page);
                                           # keyed by content, so the same page under many years is parsed once
    for make in makes:
        for year in range(y0, y1 + 1):
            with env.begin() as txn:
                for key, ptr, payload in ci.walk_vehicle(txn, ci.enc_prefix(f"{make}/{year}")):
                    segs = key.split(b"/")
                    if len(segs) < 5 or not segs[3] or not payload or ci.is_index_node(ptr):
                        continue
                    model_full = urllib.parse.unquote(segs[3].decode("utf-8", "replace"))
                    model, engine = charm_vehicle(model_full, known)
                    car = (model, year, engine)
                    names.setdefault(car, set()).add(model_full)
                    st["keys"] += 1
                    sids = by_ptr.get(ptr)
                    if sids:
                        st["by_ptr"] += 1
                    else:
                        key_text = urllib.parse.unquote(key.decode("utf-8", "replace"))
                        crumbs = [c for c in key_text.strip("/").split("/")[3:] if c]
                        head = " > ".join(crumbs[-2:])
                        pk = (hashlib.blake2b(payload, digest_size=16).digest(), head)
                        if pk not in miss:
                            found = []
                            if not (b"id='expand-all'" in payload[:300] or b'id="expand-all"' in payload[:300]):
                                st["parsed"] += 1
                                body, rows = charm_text(BeautifulSoup(payload.decode("utf-8", "replace"), "lxml"))
                                for n, part in enumerate(charm_parts(body), 1):
                                    text = f"{head}\n{part}" if n == 1 else f"{head} (continued, part {n})\n{part}"
                                    sid = by_hash.get(hashlib.blake2b(text.encode(), digest_size=8).digest())
                                    if sid:
                                        found.append(sid)
                            miss[pk] = found
                        sids = miss[pk]
                        st["by_hash" if sids else "unmatched"] += 1
                    cars.setdefault(car, set()).update(sids or ())
            print(f"  {make} {year}: {len(cars)} cars so far, {st}", flush=True)
    cars = {c: s for c, s in cars.items() if s}
    if not cars:
        sys.exit("relink found no pages; nothing changed (wrong --makes?)")
    old_n = conn.execute("SELECT count(*) FROM vehicle").fetchone()[0]
    conn.commit()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM dtc_procedure WHERE section_id IN (SELECT id FROM section WHERE doc_id=?)", doc)
        conn.execute("DELETE FROM section_vehicle WHERE section_id IN (SELECT id FROM section WHERE doc_id=?)", doc)
        conn.execute("DELETE FROM vehicle WHERE id NOT IN (SELECT vehicle_id FROM section_vehicle) "
                     "AND id NOT IN (SELECT vehicle_id FROM dtc_procedure)")
        n_dtc = 0
        for (model, year, engine), sids in sorted(cars.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] or "")):
            row = conn.execute("SELECT id FROM vehicle WHERE model=? AND chassis IS NULL AND year=? AND engine IS ? "
                               "AND market='USDM'", (model, year, engine)).fetchone()
            vid = row[0] if row else conn.execute(
                "INSERT INTO vehicle(model,chassis,year,engine,market) VALUES (?,NULL,?,?,'USDM')",
                (model, year, engine)).lastrowid
            conn.executemany("INSERT OR IGNORE INTO section_vehicle VALUES (?,?)", [(vid, s) for s in sids])
            for s in sids:
                m = CHARM_DTC_RE.match(title_of.get(s) or "")
                if m:
                    conn.execute("INSERT OR IGNORE INTO dtc(code) VALUES (?)", (m.group(1),))
                    conn.execute("INSERT OR IGNORE INTO dtc_procedure VALUES (?,?,?)", (m.group(1), vid, s))
                    n_dtc += 1
        new_n = conn.execute("SELECT count(*) FROM vehicle").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('charm_relink', ?)", (json.dumps(
            dict(st, vehicles_before=old_n, vehicles_after=new_n, dtc_links=n_dtc)),))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    with open(os.path.splitext(a.db)[0] + ".relink.txt", "w") as f:
        for (model, year, engine) in sorted(cars, key=lambda c: (c[0], c[1], c[2] or "")):
            f.write(f"{year}\t{model}\t{engine or ''}\t<- {' | '.join(sorted(names[(model, year, engine)]))}\n")
    models = sorted({c[0] for c in cars})
    print(f"relink: vehicles {old_n} -> {new_n}, {len(models)} models, {st}")
    print("relink: models:", ", ".join(models))
    print(f"relink: every CHARM name and the model it got: {os.path.splitext(a.db)[0]}.relink.txt")


def step_finish(conn, a):
    for t in ("section_fts", "knowledge_fts", "dtc_card_fts"):
        conn.execute(f"INSERT INTO {t}({t}) VALUES ('optimize')")
    conn.execute("ANALYZE")
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    rows = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("vehicle", "section", "spec", "labor", "dtc_card", "dtc", "dtc_ssm", "dtc_procedure", "section_ref",
                      "knowledge", "ssm_param")}
    print("finish:", rows, f"{os.path.getsize(a.db) / 1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    sub = ap.add_subparsers(dest="step", required=True)
    f = sub.add_parser("fsm")
    f.add_argument("markdown")
    f.add_argument("--model", required=True, help="comma list, e.g. Legacy,Outback")
    f.add_argument("--chassis", required=True)
    f.add_argument("--years", required=True, help="comma list, e.g. 2001,2002")
    f.add_argument("--engines", required=True, help="comma list, e.g. EJ251")
    f.add_argument("--title", required=True)
    d = sub.add_parser("diag")
    d.add_argument("diag_db")
    c = sub.add_parser("charm")
    c.add_argument("--charm", default="~/mnt/charm", help="mounted CHARM lmdb-pages directory")
    c.add_argument("--charm-script", default="~/subaru_cyberdeck/cyberdeck_forge",
                   help="folder holding charm_ingest.py")
    c.add_argument("--years", default="1990-2012")
    c.add_argument("--makes", default=",".join(CHARM_MAKES),
                   help="CHARM makes to load; one DB per make, e.g. --db chevy.db --makes Chevrolet,'Chevy Truck'")
    c.add_argument("--only", help="test run: only vehicles whose 'year model' contains this, e.g. '2002 Forester'")
    cd = sub.add_parser("cards", help="load pre-baked DTC cards from cards.db (after diag, before embed/finish)")
    cd.add_argument("cards_db")
    sub.add_parser("labor", help="rebuild the labor table from stored CHARM pages (no CHARM mount needed)")
    e = sub.add_parser("embed")
    e.add_argument("--device", default="cuda", help="cuda (stops if CUDA is missing) or cpu")
    e.add_argument("--dtype", default="float16", choices=("float16", "bfloat16", "float32"),
                   help="float16: as fast as bfloat16 on the 3070 and closer to float32")
    e.add_argument("--token-budget", type=int, default=16384,
                   help="tokens per GPU batch (rows x longest row); halves itself on out-of-memory")
    e.add_argument("--max-tokens", type=int, default=2048,
                   help="cut each row at this many tokens (2,000 chars of body stay under it; the old step had no cap)")
    e.add_argument("--body-chars", type=int, default=2000, help="characters of each row's body to embed")
    e.add_argument("--text", default="plain", choices=("plain", "header", "source"),
                   help="plain: path/title + body (what every stored vector uses); header: one line of make, cars, "
                        "source type and section in front (no consistent gain on acura+subaru 2026-10-27, "
                        "logs/embed_bench/); source: header without the cars")
    e.add_argument("--make", help="make name for the header (default: from the DB file name)")
    e.add_argument("--only", help="re-embed only these tables, e.g. dtc_card (default: section,knowledge,dtc_card)")
    e.add_argument("--missing", action="store_true",
                   help="keep existing vectors; embed only rows that have none and drop vectors of deleted rows")
    r = sub.add_parser("relink", help="rename CHARM cars by NHTSA model names and re-attach their pages "
                                      "(needs the CHARM mount; keeps pages and vectors)")
    r.add_argument("--charm", default="~/mnt/charm", help="mounted CHARM lmdb-pages directory")
    r.add_argument("--charm-script", default="~/subaru_cyberdeck/cyberdeck_forge", help="folder holding charm_ingest.py")
    r.add_argument("--years", default="1982-2013")
    r.add_argument("--makes", required=True, help="the CHARM makes this DB was built from, e.g. Chevrolet,'Chevy Truck'")
    sub.add_parser("finish")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db, uri=True)  # uri=True so step_diag can ATTACH file:...?mode=ro
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '1')")
    {"fsm": step_fsm, "diag": step_diag, "charm": step_charm, "labor": step_labor, "cards": step_cards, "embed": step_embed, "relink": step_relink, "finish": step_finish}[a.step](conn, a)
    conn.close()


if __name__ == "__main__":
    main()
