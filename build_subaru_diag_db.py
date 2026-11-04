#!/usr/bin/env python3
"""
Build subaru_diag.db: a small, sourced diagnostic database for the cyberdeck.

Standard library only (runs on the Orange Pi as-is). Every row records where it
came from in the `source` table.

Steps (run any subset, in any order; each one is re-runnable):
  fsm        DTC list + per-DTC diagnostic procedures parsed from a manual_chunks
             knowledge DB (e.g. test_subaru_knowledge.db built by build_test_db.py)
  download   fetch NHTSA flat files (TSBs, complaints, recalls) into raw/nhtsa/
  nhtsa      load the Subaru rows from those flat files
  romraider  load SSM parameters / switches / DTC addresses from a RomRaider
             logger definition XML
  index      rebuild DTC cross-links and the full-text search index

Examples:
  python3 build_subaru_diag_db.py fsm --knowledge-db ~/subaru_cyberdeck/test_subaru_knowledge.db \
      --model Forester --chassis SF --years 2002 --engine EJ251
  python3 build_subaru_diag_db.py download nhtsa index
  python3 build_subaru_diag_db.py romraider --xml ~/Downloads/logger_STD_EN_v370.xml index
  python3 build_subaru_diag_db.py lookup P0420 --model Forester --year 2002
"""
import argparse
import csv
import io
import os
import re
import sqlite3
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

HOME = os.path.dirname(os.path.abspath(__file__))  # repo root
DEFAULT_DB = os.path.join(HOME, "subaru_diag.db")
DEFAULT_RAW = os.path.join(HOME, "raw")

DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b")

# NHTSA flat files on static.nhtsa.gov (checked 2026-10). TSBs and recalls are
# now split into several zips; every file listed for a kind is fetched. If a URL
# 404s, get the current links from https://www.nhtsa.gov/nhtsa-datasets-and-apis
# and drop the zips into raw/nhtsa/<kind>/ (every .zip there is read).
_FFDD = "https://static.nhtsa.gov/odi/ffdd"
NHTSA_URLS = {
    "tsbs": [f"{_FFDD}/tsbs/TSBS_RECEIVED_{r}.zip" for r in
             ("1995-1999", "2000-2004", "2005-2009", "2010-2014", "2015-2019",
              "2020-2024", "2025-2026")],
    "cmpl": [f"{_FFDD}/cmpl/FLAT_CMPL.zip"],
    "rcl":  [f"{_FFDD}/rcl/FLAT_RCL_PRE_2010.zip", f"{_FFDD}/rcl/FLAT_RCL_POST_2010.zip"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS source (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, url TEXT, license TEXT,
    version TEXT, retrieved_at TEXT);

CREATE TABLE IF NOT EXISTS vehicle (
    id INTEGER PRIMARY KEY, model TEXT, chassis TEXT, year_from INT, year_to INT,
    engine TEXT, transmission TEXT, notes TEXT,
    UNIQUE (model, chassis, year_from, year_to, engine));

-- Manufacturer-specific DTC definitions (FSM, RomRaider, FreeSSM ...)
CREATE TABLE IF NOT EXISTS dtc_subaru (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL, module TEXT, description TEXT,
    fsm_ref TEXT, ssm_tmp_addr TEXT, ssm_mem_addr TEXT, ssm_bit INT,
    vehicle_id INT REFERENCES vehicle(id), source_id INT REFERENCES source(id),
    UNIQUE (code, vehicle_id, source_id));
CREATE INDEX IF NOT EXISTS ix_dtc_subaru_code ON dtc_subaru(code);

-- FSM diagnostic procedure text, one row per manual chunk, in manual order
CREATE TABLE IF NOT EXISTS dtc_procedure (
    id INTEGER PRIMARY KEY, code TEXT NOT NULL, seq INT, section_title TEXT,
    content TEXT, kb_chunk_id INT, vehicle_id INT REFERENCES vehicle(id),
    source_id INT REFERENCES source(id),
    UNIQUE (code, vehicle_id, kb_chunk_id));
CREATE INDEX IF NOT EXISTS ix_dtc_procedure_code ON dtc_procedure(code);

-- SSM (Subaru Select Monitor) live-data parameters from RomRaider
CREATE TABLE IF NOT EXISTS ssm_param (
    id TEXT NOT NULL, kind TEXT, name TEXT, description TEXT, address TEXT,
    length INT, bit INT, units TEXT, expr TEXT, target TEXT,
    cap_byte INT, cap_bit INT, ecu_id TEXT NOT NULL DEFAULT '',
    source_id INT REFERENCES source(id),
    PRIMARY KEY (id, ecu_id, units));

CREATE TABLE IF NOT EXISTS tsb (
    nhtsa_id INT, bulletin_no TEXT, comm_date TEXT, comm_type TEXT,
    model TEXT, model_year INT, components TEXT, mfr_system TEXT,
    mfr_subsystem TEXT, summary TEXT, source_id INT REFERENCES source(id),
    PRIMARY KEY (nhtsa_id, model, model_year));
CREATE INDEX IF NOT EXISTS ix_tsb_vehicle ON tsb(model, model_year);

CREATE TABLE IF NOT EXISTS complaint (
    cmpl_id INT PRIMARY KEY, odi_no INT, model TEXT, model_year INT,
    component TEXT, fail_date TEXT, miles INT, summary TEXT,
    source_id INT REFERENCES source(id));
CREATE INDEX IF NOT EXISTS ix_complaint_vehicle ON complaint(model, model_year);

CREATE TABLE IF NOT EXISTS recall (
    campaign TEXT, model TEXT, model_year INT, component TEXT,
    defect TEXT, consequence TEXT, remedy TEXT,
    source_id INT REFERENCES source(id),
    PRIMARY KEY (campaign, model, model_year, component));
CREATE INDEX IF NOT EXISTS ix_recall_vehicle ON recall(model, model_year);

-- Forum fixes: link + short summary only (filled by a later, targeted step)
CREATE TABLE IF NOT EXISTS forum_fix (
    id INTEGER PRIMARY KEY, site TEXT, url TEXT UNIQUE, title TEXT, model TEXT,
    year_from INT, year_to INT, summary TEXT, confirmed INT, retrieved_at TEXT);

-- DTCs mentioned in free text (rebuilt by `index`)
CREATE TABLE IF NOT EXISTS dtc_mention (code TEXT, kind TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS ix_dtc_mention_code ON dtc_mention(code);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_diag USING fts5(
    kind UNINDEXED, ref UNINDEXED, model UNINDEXED, model_year UNINDEXED, text,
    tokenize='porter unicode61');
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def upsert_source(conn, name, url, license_, version=None):
    conn.execute(
        "INSERT INTO source(name,url,license,version,retrieved_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET url=excluded.url, license=excluded.license, "
        "version=excluded.version, retrieved_at=excluded.retrieved_at",
        (name, url, license_, version, now()))
    return conn.execute("SELECT id FROM source WHERE name=?", (name,)).fetchone()[0]


def parse_years(spec):
    a, _, b = spec.partition("-")
    return int(a), int(b or a)


# ---------------------------------------------------------------------------
# FSM: DTC list and procedures from a manual_chunks knowledge DB
# ---------------------------------------------------------------------------
LIST_ROW_RE = re.compile(
    r"^\|\s*\*?\s*([PCBU][0-3][0-9A-F]{3})\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|", re.M)
TOP_SECTION_RE = re.compile(r"^(?:<span[^>]*></span>)?\**\s*[A-Z]{1,2}:\s")
DTC_HEADING_RE = re.compile(r"DTC\s+([PCBU][0-3][0-9A-F]{3})")


def module_for(code):
    if code[0] != "P":
        return {"C": "chassis", "B": "body", "U": "network"}[code[0]]
    n = int(code[1:]) if code[1:].isdigit() else -1
    if 700 <= n % 1000 <= 799:  # P07xx and P17xx are transmission on these cars
        return "transmission"
    return "engine"


def step_fsm(conn, a):
    kb = os.path.expanduser(a.knowledge_db)
    y0, y1 = parse_years(a.years)
    # Read-only and without sqlite-vec: we only touch the plain manual_chunks table.
    src_conn = sqlite3.connect(f"file:{kb}?mode=ro", uri=True)
    files = [r[0] for r in src_conn.execute("SELECT DISTINCT file_name FROM manual_chunks")]
    src_id = upsert_source(conn, f"FSM:{os.path.basename(kb)}", kb,
                           "Subaru FSM (personal use)", "; ".join(files))
    conn.execute("INSERT OR IGNORE INTO vehicle(model,chassis,year_from,year_to,engine) "
                 "VALUES(?,?,?,?,?)", (a.model, a.chassis, y0, y1, a.engine))
    veh_id = conn.execute("SELECT id FROM vehicle WHERE model=? AND chassis=? AND year_from=? "
                          "AND year_to=? AND engine IS ?",
                          (a.model, a.chassis, y0, y1, a.engine)).fetchone()[0]
    conn.execute("DELETE FROM dtc_subaru WHERE vehicle_id=? AND source_id=?", (veh_id, src_id))
    conn.execute("DELETE FROM dtc_procedure WHERE vehicle_id=? AND source_id=?", (veh_id, src_id))

    rows = src_conn.execute(
        "SELECT id, section_title, content FROM manual_chunks ORDER BY id").fetchall()

    # 1) DTC list tables ("A: LIST" sections): | code | description | <Ref. to ...> |
    defs = {}
    for _id, title, text in rows:
        if "LIST" not in (title or "").upper():
            continue
        for m in LIST_ROW_RE.finditer(text or ""):
            code, desc, ref = m.group(1), m.group(2).strip(), m.group(3).strip()
            if desc.lower() in ("item", "description", "dtc"):
                continue
            ref = re.sub(r"^<Ref\. to\s*|>$", "", ref)
            defs.setdefault(code, (desc, ref))
    conn.executemany(
        "INSERT OR REPLACE INTO dtc_subaru(code,module,description,fsm_ref,vehicle_id,source_id) "
        "VALUES(?,?,?,?,?,?)",
        [(c, module_for(c), d, r, veh_id, src_id) for c, (d, r) in sorted(defs.items())])

    # 2) Procedures: from a lettered "X: DTC Pxxxx —" heading until the next lettered heading.
    cur, seq, procs = None, 0, []
    for _id, title, text in rows:
        title = title or ""
        if TOP_SECTION_RE.match(title):
            m = DTC_HEADING_RE.search(title)
            cur, seq = (m.group(1) if m else None), 0
        if cur:
            seq += 1
            clean_title = re.sub(r"<[^>]+>|\*", "", title).strip()
            procs.append((cur, seq, clean_title, text, _id, veh_id, src_id))
    conn.executemany(
        "INSERT OR REPLACE INTO dtc_procedure(code,seq,section_title,content,kb_chunk_id,"
        "vehicle_id,source_id) VALUES(?,?,?,?,?,?,?)", procs)
    conn.commit()
    print(f"fsm: {len(defs)} DTC definitions, {len(set(p[0] for p in procs))} DTCs with "
          f"procedures ({len(procs)} chunks) for {a.model} {a.chassis} {y0}-{y1}")


# ---------------------------------------------------------------------------
# NHTSA flat files
# ---------------------------------------------------------------------------
def step_download(conn, a):
    for kind, urls in NHTSA_URLS.items():
        dest_dir = os.path.join(a.raw, "nhtsa", kind)
        os.makedirs(dest_dir, exist_ok=True)
        if any(f.lower().endswith(".zip") for f in os.listdir(dest_dir)) and not a.force:
            print(f"download: {kind} already present in {dest_dir} (use --force to refetch)")
            continue
        failed = []
        for url in urls:
            dest = os.path.join(dest_dir, os.path.basename(url))
            try:
                print(f"download: {url}")
                req = urllib.request.Request(url, headers={"User-Agent": "subaru-cyberdeck/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r, open(dest + ".part", "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                os.replace(dest + ".part", dest)
                print(f"download: saved {dest} ({os.path.getsize(dest) >> 20} MB)")
            except Exception as e:
                print(f"download: failed ({e})")
                failed.append(url)
                if os.path.exists(dest + ".part"):
                    os.remove(dest + ".part")
        if failed:
            print(f"download: {len(failed)} {kind} file(s) missing. Get current links from "
                  f"https://www.nhtsa.gov/nhtsa-datasets-and-apis and put the zips in {dest_dir}")


def _year_ok(v):
    v = v.strip()
    return v.isdigit() and (1900 <= int(v) <= 2100 or v == "9999")      # 9999: year not known


def _date_ok(v):
    v = v.strip()
    return not v or (len(v) == 8 and v.isdigit() and 1900 <= int(v[:4]) <= 2100)


# Fields are read by position. Before a file is used, its first rows must show a model year, a make and a
# date where the code reads them; a reordered layout stops that file instead of filling the wrong columns.
LAYOUT = {  # folder -> (minimum fields, {index: (name, check)})
    "tsbs": (14, {9: ("model year", _year_ok), 7: ("make", lambda v: bool(re.search(r"[A-Za-z]", v))),
                  4: ("communication date", _date_ok)}),
    "cmpl": (20, {5: ("YEARTXT", _year_ok), 3: ("MAKETXT", lambda v: bool(re.search(r"[A-Za-z]", v))),
                  7: ("FAILDATE", _date_ok)}),
    "rcl": (22, {4: ("YEARTXT", _year_ok), 2: ("MAKETXT", lambda v: bool(re.search(r"[A-Za-z]", v)))}),
}
LAYOUT_SAMPLE = 200


def layout_error(kind, rows):
    """None when the sampled rows fit LAYOUT[kind], else a message naming the field and what it held."""
    if kind not in LAYOUT:
        return None
    width, fields = LAYOUT[kind]
    full = [r for r in rows if len(r) >= width]
    if not full:
        return f"no row has the {width} fields this layout needs (first row has {len(rows[0]) if rows else 0})"
    for i, (name, ok) in fields.items():
        bad = [r[i] for r in full if not ok(r[i])]
        if len(bad) > 0.05 * len(full):
            return f"field {i} ({name}) holds {bad[0][:40]!r} in {len(bad)} of {len(full)} sampled rows"
    return None


def _checked(kind, label, rows):
    """Pass rows through after the first LAYOUT_SAMPLE pass layout_error(); otherwise skip the file, loudly."""
    head = []
    for r in rows:
        head.append(r)
        if len(head) >= LAYOUT_SAMPLE:
            break
    err = layout_error(kind, head)
    if err:
        print(f"nhtsa: SKIPPED {label}: layout changed? {err}", file=sys.stderr)
        return
    yield from head
    yield from rows


def iter_flat_rows(dir_):
    """Yield tab-split rows from every .zip/.txt in dir_, streaming (no extraction). Each file's layout is
    checked first (LAYOUT, by folder name)."""
    if not os.path.isdir(dir_):
        return
    kind = os.path.basename(os.path.normpath(dir_))
    csv.field_size_limit(1 << 24)
    for name in sorted(os.listdir(dir_)):
        path = os.path.join(dir_, name)
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                for member in z.namelist():
                    if member.lower().endswith((".txt", ".tsv")) and not member.upper().startswith(
                            ("TSBS.TXT", "CMPL.TXT", "RCL.TXT", "MFRCOMMS.TXT")):
                        with z.open(member) as fh:
                            yield from _checked(kind, f"{name}:{member}",
                                                _read_tab(io.TextIOWrapper(fh, "latin-1", newline="")))
        elif name.lower().endswith((".txt", ".tsv")) and "FLAT" in name.upper():
            with open(path, encoding="latin-1", newline="") as fh:
                yield from _checked(kind, name, _read_tab(fh))


def _read_tab(fh):
    for line in fh:
        yield line.rstrip("\r\n").split("\t")


def _int(s):
    try:
        return int(str(s).strip())
    except ValueError:
        return None


def _date(s):
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s or None


def step_nhtsa(conn, a):
    y0, y1 = parse_years(a.years)
    make = "SUBARU"

    def keep(mk, yr):
        return mk.strip().upper() == make and yr is not None and y0 <= yr <= y1

    # TSBs. Field order per TSBS.txt (2024+ "MfrComms" layout, 14 fields):
    # 0 NHTSA ID, 1 replacement bulletin, 2 date added, 3 TSB/doc id, 4 comm date,
    # 5 campaign id, 6 comm type, 7 make, 8 model, 9 model year, 10 NHTSA components,
    # 11 mfr system, 12 mfr subsystem, 13 summary
    src = upsert_source(conn, "NHTSA TSB flat file", NHTSA_URLS["tsbs"][0], "US public domain")
    n = bad = 0
    batch = []
    for r in iter_flat_rows(os.path.join(a.raw, "nhtsa", "tsbs")):
        if len(r) < 14:
            bad += 1
            continue
        yr = _int(r[9])
        if not keep(r[7], yr):
            continue
        batch.append((_int(r[0]), r[3].strip(), _date(r[4]), r[6].strip(), r[8].strip().upper(),
                      yr, r[10].strip(), r[11].strip(), r[12].strip(), r[13].strip(), src))
        n += 1
    conn.executemany("INSERT OR REPLACE INTO tsb VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
    print(f"nhtsa: {n} Subaru TSB rows {y0}-{y1}" + (f" ({bad} short rows skipped)" if bad else ""))

    # Complaints, per CMPL.txt: 0 CMPLID, 1 ODINO, 3 MAKETXT, 4 MODELTXT, 5 YEARTXT,
    # 7 FAILDATE, 11 COMPDESC, 17 MILES, 19 CDESCR
    src = upsert_source(conn, "NHTSA complaints flat file", NHTSA_URLS["cmpl"][0], "US public domain")
    n, batch = 0, []
    for r in iter_flat_rows(os.path.join(a.raw, "nhtsa", "cmpl")):
        if len(r) < 20:
            continue
        yr = _int(r[5])
        if not keep(r[3], yr):
            continue
        batch.append((_int(r[0]), _int(r[1]), r[4].strip().upper(), yr, r[11].strip(),
                      _date(r[7]), _int(r[17]), r[19].strip(), src))
        n += 1
    conn.executemany("INSERT OR REPLACE INTO complaint VALUES(?,?,?,?,?,?,?,?,?)", batch)
    print(f"nhtsa: {n} Subaru complaint rows {y0}-{y1}")

    # Recalls, per RCL.txt: 1 CAMPNO, 2 MAKETXT, 3 MODELTXT, 4 YEARTXT, 6 COMPNAME,
    # 19 DESC_DEFECT, 20 CONEQUENCE_DEFECT, 21 CORRECTIVE_ACTION
    src = upsert_source(conn, "NHTSA recalls flat file", NHTSA_URLS["rcl"][0], "US public domain")
    n, batch = 0, []
    for r in iter_flat_rows(os.path.join(a.raw, "nhtsa", "rcl")):
        if len(r) < 22:
            continue
        yr = _int(r[4])
        if not keep(r[2], yr):
            continue
        # pre-2010 records often leave DESC_DEFECT empty and put the text in NOTES (22)
        defect = r[19].strip() or (r[22].strip() if len(r) > 22 else "")
        batch.append((r[1].strip(), r[3].strip().upper(), yr, r[6].strip(),
                      defect, r[20].strip(), r[21].strip(), src))
        n += 1
    conn.executemany("INSERT OR REPLACE INTO recall VALUES(?,?,?,?,?,?,?,?)", batch)
    print(f"nhtsa: {n} Subaru recall rows {y0}-{y1}")
    conn.commit()


# ---------------------------------------------------------------------------
# RomRaider logger definitions
# ---------------------------------------------------------------------------
def step_romraider(conn, a):
    path = os.path.expanduser(a.xml)
    root = ET.parse(path).getroot()
    version = root.get("version") or os.path.basename(path)
    src = upsert_source(conn, "RomRaider logger definitions", path,
                        "RomRaider community definitions (GPL tool; personal use)", version)
    conn.execute("DELETE FROM ssm_param WHERE source_id=?", (src,))
    conn.execute("DELETE FROM dtc_subaru WHERE source_id=?", (src,))

    proto = next((p for p in root.iter("protocol") if p.get("id") == "SSM"), None)
    if proto is None:
        sys.exit("romraider: no <protocol id=\"SSM\"> in that XML")

    params = []

    def convs(el):
        cs = el.findall("./conversions/conversion")
        return cs or [None]

    for p in proto.iter("parameter"):
        addr = p.find("address")
        for c in convs(p):
            params.append((p.get("id"), "parameter", p.get("name"), p.get("desc"),
                           addr.text.strip() if addr is not None and addr.text else None,
                           _int(addr.get("length", 1)) if addr is not None else None, None,
                           c.get("units") if c is not None else None,
                           c.get("expr") if c is not None else None, p.get("target"),
                           _int(p.get("ecubyteindex", "")), _int(p.get("ecubit", "")), "", src))
    for s in proto.iter("switch"):
        params.append((s.get("id"), "switch", s.get("name"), s.get("desc"), s.get("byte"), 1,
                       _int(s.get("bit", "")), "on/off", None, s.get("target"),
                       _int(s.get("ecubyteindex", "")), _int(s.get("ecubit", "")), "", src))
    for e in proto.iter("ecuparam"):
        for ecu in e.findall("ecu"):
            addr = ecu.find("address")
            for c in convs(e):
                for ecu_id in (ecu.get("id") or "").split(","):
                    params.append((e.get("id"), "ecuparam", e.get("name"), e.get("desc"),
                                   addr.text.strip() if addr is not None and addr.text else None,
                                   _int(addr.get("length", 1)) if addr is not None else None,
                                   _int(addr.get("bit", "")) if addr is not None else None,
                                   c.get("units") if c is not None else None,
                                   c.get("expr") if c is not None else None, e.get("target"),
                                   None, None, ecu_id.strip(), src))
    conn.executemany("INSERT OR REPLACE INTO ssm_param VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", params)

    dtcs = []
    for d in proto.iter("dtcode"):
        name = d.get("name") or ""
        m = DTC_RE.search(name)
        code = m.group(1) if m else d.get("id")
        desc = re.sub(r"^\s*[PCBU][0-3][0-9A-F]{3}\s*[-–—:]?\s*", "", name)
        dtcs.append((code, module_for(code) if m else None, desc, d.get("tmpaddr"),
                     d.get("memaddr"), _int(d.get("bit", "")), None, src))
    conn.executemany(
        "INSERT OR REPLACE INTO dtc_subaru(code,module,description,ssm_tmp_addr,ssm_mem_addr,"
        "ssm_bit,vehicle_id,source_id) VALUES(?,?,?,?,?,?,?,?)", dtcs)
    conn.commit()
    kinds = {}
    for p in params:
        kinds[p[1]] = kinds.get(p[1], 0) + 1
    print(f"romraider: {kinds} rows, {len(dtcs)} DTC definitions (version {version})")


# ---------------------------------------------------------------------------
# Cross-links + full-text index
# ---------------------------------------------------------------------------
def step_index(conn, a):
    conn.execute("DELETE FROM dtc_mention")
    conn.execute("DELETE FROM fts_diag")
    ment, fts = [], []

    def add(kind, ref, model, year, text):
        if not text:
            return
        fts.append((kind, ref, model, year, text))
        for code in set(DTC_RE.findall(text.upper())):
            ment.append((code, kind, ref))

    for code, desc, model, y in conn.execute(
            "SELECT d.code, d.description, v.model, v.year_from FROM dtc_subaru d "
            "LEFT JOIN vehicle v ON v.id=d.vehicle_id"):
        fts.append(("dtc", code, model, y, f"{code} {desc}"))
    for code, rid, title, content, model, y in conn.execute(
            "SELECT p.code, p.id, p.section_title, p.content, v.model, v.year_from "
            "FROM dtc_procedure p LEFT JOIN vehicle v ON v.id=p.vehicle_id"):
        fts.append(("procedure", str(rid), model, y, f"{code} {title}\n{content}"))
    for rid, model, y, comp, summ in conn.execute(
            "SELECT nhtsa_id, model, model_year, components, summary FROM tsb"):
        add("tsb", str(rid), model, y, f"{comp}\n{summ}")
    for rid, model, y, comp, summ in conn.execute(
            "SELECT cmpl_id, model, model_year, component, summary FROM complaint"):
        add("complaint", str(rid), model, y, f"{comp}\n{summ}")
    for camp, model, y, comp, d, c, r in conn.execute(
            "SELECT campaign, model, model_year, component, defect, consequence, remedy FROM recall"):
        add("recall", camp, model, y, f"{comp}\n{d}\n{c}\n{r}")
    for rid, model, y, title, summ in conn.execute(
            "SELECT id, model, year_from, title, summary FROM forum_fix"):
        add("forum", str(rid), model, y, f"{title}\n{summ}")

    conn.executemany("INSERT INTO dtc_mention VALUES(?,?,?)", ment)
    conn.executemany("INSERT INTO fts_diag VALUES(?,?,?,?,?)", fts)
    conn.execute("INSERT INTO fts_diag(fts_diag) VALUES('optimize')")
    conn.commit()
    conn.execute("VACUUM")
    print(f"index: {len(fts)} documents indexed, {len(ment)} DTC mentions")


# ---------------------------------------------------------------------------
# Lookup: what the agent should pull before calling the LLM
# ---------------------------------------------------------------------------
def step_lookup(conn, a):
    code = a.code.upper()
    model = (a.model or "").upper()
    print(f"== {code}")
    for desc, mod, ref, src, tmp in conn.execute(
            "SELECT d.description, d.module, d.fsm_ref, s.name, d.ssm_tmp_addr FROM dtc_subaru d "
            "JOIN source s ON s.id=d.source_id WHERE d.code=?", (code,)):
        print(f"  [{src}] ({mod}) {desc}" + (f"  FSM: {ref}" if ref else "")
              + (f"  SSM tmp {tmp}" if tmp else ""))
    n = conn.execute("SELECT count(*) FROM dtc_procedure WHERE code=?", (code,)).fetchone()[0]
    print(f"  FSM procedure chunks: {n}")
    where, args = "m.code=?", [code]
    if model:
        where += " AND upper(t.model) LIKE ?"
        args.append(f"%{model}%")
    if a.year:
        where += " AND t.model_year=?"
        args.append(a.year)
    for bno, y, summ in conn.execute(
            f"SELECT DISTINCT t.bulletin_no, t.model_year, substr(t.summary,1,160) FROM dtc_mention m "
            f"JOIN tsb t ON m.kind='tsb' AND t.nhtsa_id=m.ref WHERE {where} LIMIT 10", args):
        print(f"  TSB {bno} ({y}): {summ}")
    cwhere = where.replace("t.", "c.")
    cnt = conn.execute(
        f"SELECT count(*), avg(c.miles) FROM dtc_mention m JOIN complaint c "
        f"ON m.kind='complaint' AND c.cmpl_id=m.ref WHERE {cwhere}", args).fetchone()
    print(f"  NHTSA complaints mentioning it: {cnt[0]}"
          + (f" (avg {int(cnt[1]):,} mi)" if cnt[1] else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+",
                    help="fsm | download | nhtsa | romraider | index | lookup <CODE>")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--raw", default=DEFAULT_RAW, help="where downloaded files live")
    ap.add_argument("--knowledge-db", default=os.path.join(HOME, "test_subaru_knowledge.db"))
    ap.add_argument("--model", default=None)
    ap.add_argument("--chassis", default=None)
    ap.add_argument("--engine", default=None)
    ap.add_argument("--year", type=int, default=None, help="lookup filter")
    ap.add_argument("--years", default="1990-2012", help="model-year range, e.g. 2002 or 1990-2012")
    ap.add_argument("--xml", help="RomRaider logger definition XML")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    steps = list(a.steps)
    if "lookup" in steps:
        i = steps.index("lookup")
        if i + 1 >= len(steps):
            ap.error("lookup needs a DTC, e.g. lookup P0420")
        a.code = steps.pop(i + 1)

    conn = connect(a.db)
    t0 = time.time()
    for step in steps:
        if step == "fsm":
            if not (a.model and a.chassis):
                ap.error("fsm needs --model and --chassis (e.g. --model Forester --chassis SF --years 2002)")
            step_fsm(conn, a)
        elif step == "download":
            step_download(conn, a)
        elif step == "nhtsa":
            step_nhtsa(conn, a)
        elif step == "romraider":
            if not a.xml:
                ap.error("romraider needs --xml path/to/logger.xml")
            step_romraider(conn, a)
        elif step == "index":
            step_index(conn, a)
        elif step == "lookup":
            step_lookup(conn, a)
        else:
            ap.error(f"unknown step {step}")
    conn.close()
    print(f"done in {time.time() - t0:.1f}s -> {a.db}")


if __name__ == "__main__":
    main()
