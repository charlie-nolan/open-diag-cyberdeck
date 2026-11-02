#!/usr/bin/env python3
"""
Build the live-telemetry reference tables for one make (or --all): kb/telemetry/<make>.telemetry.db, then
merge a copy into kb/<make>.db as telem_* tables. The runtime side is app/telemetry_ref.py (both apps).

Steps (each takes --make <db name> or --all; run them in this order):
  params  canonical parameters, how each cable/protocol's raw value becomes one (telem_source_map), the
          capability catalog (telem_capability) and which cable reads which protocol (telem_coverage).
          Sources: SAE J1979 formulas in telemetry_ref (checked against python-obd), RomRaider logger
          definitions (raw/romraider, SSM2), FreeSSM tables, PCMHammer (GM mode $22). CPU, seconds.
  zones   healthy zones from the make's own manual tables (FSM reference tables, CHARM data-list pages read
          from the mounted CHARM image), then generic fallbacks from app/live_data.py marked 'generic'.
          Every zone quotes its source row; Python checks each number is in the quote. CPU, minutes.
  paths   out-of-zone pathways: DTC procedures whose title or step checks name the parameter, and component
          inspection sections that name its sensor. Lexical only, every link quotes the manual text. CPU.
  embed   embeddings for the pathways (Qwen3-Embedding-0.6B, int8, sqlite-vec). GPU when tools/gpu_guard.sh
          says it is free, otherwise CPU (small table).
  merge   copy the telem_* tables into kb/<make>.db (previous telem_* copy saved under kb/telemetry/backup/).
          Refuses while the CHARM build is still writing that make DB.
  report  coverage table (manual / CHARM / generic / none per parameter, pathways, cable reads), definition
          comparison for parameters both cables read, and a 20 + 20 spot-check sample with its cited page.

  ~/subaru_cyberdeck/venv/bin/python kb/build_telemetry.py --make subaru params zones paths embed merge report
Log: logs/telemetry_<make>.log. Never talks to a car.
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
KB = os.path.join(ROOT, "kb")
OUT = os.path.join(KB, "telemetry")
sys.path.insert(0, os.path.join(ROOT, "app"))
import telemetry_ref as T  # noqa: E402

ROMRAIDER_XML = os.path.join(ROOT, "raw/romraider/v370/logger_STD_EN_v370.xml")
FREESSM_JSON = os.path.join(ROOT, "app/ssm/freessm_ssm2_tables.json")
PCMH_DIR = os.path.join(ROOT, "raw/pcmhammer/Apps/UI/WindowsForms/PcmLogger")
GM_PID_JSON = os.path.join(KB, "gm_mode22_pids.json")
CHARM_MOUNT = os.path.expanduser("~/mnt/charm")
GROUPS = os.path.join(KB, "charm_make_groups.txt")
# GM brands: their PCMs speak GM mode $22 (whether a given PCM is a PCMHammer one shows in its replies)
GM_MAKES = {"chevrolet", "gmc", "buick", "cadillac", "pontiac", "oldsmobile", "saturn", "hummer"}
SSM_MAKES = {"subaru"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS telem_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS telem_param (        -- one row per canonical parameter
  key TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT, description TEXT, sources TEXT, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telem_source_map (   -- how one protocol's raw value becomes the canonical one
  id INTEGER PRIMARY KEY, source TEXT NOT NULL, raw_id TEXT NOT NULL, name TEXT, address TEXT, nbytes INTEGER,
  conversion TEXT, raw_unit TEXT, canonical INTEGER NOT NULL DEFAULT 0, param TEXT NOT NULL,
  gate TEXT, origin TEXT NOT NULL, notes TEXT);
CREATE INDEX IF NOT EXISTS telem_source_map_i ON telem_source_map(source, raw_id);
CREATE TABLE IF NOT EXISTS telem_condition (    -- named operating conditions, Python-checkable
  name TEXT PRIMARY KEY, label TEXT NOT NULL, definition TEXT NOT NULL, source_kind TEXT NOT NULL, quote TEXT);
CREATE TABLE IF NOT EXISTS telem_zone (         -- healthy zones, every row quoting its source
  id INTEGER PRIMARY KEY, param TEXT NOT NULL, model TEXT, year_min INTEGER, year_max INTEGER, engine TEXT,
  condition TEXT NOT NULL, low REAL, high REAL, typical REAL, unit TEXT NOT NULL, source_kind TEXT NOT NULL,
  section_id INTEGER, page INTEGER, quote TEXT NOT NULL, source_ref TEXT, row_name TEXT, method TEXT NOT NULL,
  variant TEXT);  -- 'MT' / 'AT' when the manual gives the value for one transmission only
CREATE INDEX IF NOT EXISTS telem_zone_i ON telem_zone(param, condition);
CREATE TABLE IF NOT EXISTS telem_zone_unmapped (  -- reference rows not stored as zones, and why (report only)
  section_id INTEGER, row_name TEXT, quote TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS telem_path (         -- where the manual sends you when a parameter is out of zone
  id INTEGER PRIMARY KEY, param TEXT NOT NULL, direction TEXT NOT NULL, model TEXT, year_min INTEGER,
  year_max INTEGER, engine TEXT, section_id INTEGER NOT NULL, step INTEGER, kind TEXT NOT NULL,
  method TEXT NOT NULL, evidence TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 5);
CREATE INDEX IF NOT EXISTS telem_path_i ON telem_path(param, direction);
CREATE TABLE IF NOT EXISTS telem_coverage (     -- which cable reads which layer for a detected protocol
  id INTEGER PRIMARY KEY, protocol TEXT NOT NULL, protocol_match TEXT NOT NULL, cable TEXT NOT NULL,
  layer TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, notes TEXT);
CREATE TABLE IF NOT EXISTS telem_capability (   -- what each cable can read, per car scope
  id INTEGER PRIMARY KEY, make TEXT NOT NULL, model TEXT, year_min INTEGER, year_max INTEGER, engine TEXT,
  ecu_id TEXT, cable TEXT NOT NULL, protocol TEXT NOT NULL, param TEXT NOT NULL, source TEXT NOT NULL,
  raw_id TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS telem_capability_i ON telem_capability(cable, param);
"""
TELEM_TABLES = ["telem_meta", "telem_param", "telem_source_map", "telem_condition", "telem_zone",
                "telem_zone_unmapped", "telem_path", "telem_coverage", "telem_capability"]

LOG = None


def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    if LOG:
        with open(LOG, "a") as f:
            f.write(line + "\n")


def makes_in_order():
    """Make DB names in the RUNBOOK's per-make build order (charm_make_groups.txt), Subaru first."""
    out = ["subaru"]
    for ln in open(GROUPS):
        db = ln.split("|", 1)[0].strip()
        if db and db not in out:
            out.append(db)
    return out


def tdb_path(make):
    return os.path.join(OUT, f"{make}.telemetry.db")


def master_path(make):
    return os.path.join(KB, f"{make}.db")


def open_tdb(make):
    os.makedirs(OUT, exist_ok=True)
    db = sqlite3.connect(tdb_path(make))
    db.executescript(SCHEMA)
    return db


def open_master(make):
    p = master_path(make)
    if not os.path.isfile(p):
        sys.exit(f"{p} not found")
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=60)
    db.row_factory = sqlite3.Row
    return db


def being_written(make):
    """True when the CHARM loop is writing this make's DB right now (its last '=== <time> <db>' log line has no
    OK/FAIL after it). Reading another make's DB meanwhile is safe."""
    if subprocess.run(["pgrep", "-f", "build_charm_all.sh"], capture_output=True).returncode != 0:
        return False
    try:
        lines = open(os.path.join(ROOT, "logs/charm_text_all.log"), errors="replace").read().splitlines()[-400:]
    except OSError:
        return False
    cur = None
    for ln in lines:
        m = re.match(r"=== (\S+) (\S+) \[", ln)
        if m:
            cur = m.group(2)
        elif re.match(r"=== (OK|FAIL) ", ln):
            cur = None
    return cur == make


def build_in_progress(make):
    """True while the CHARM per-make build hasn't finished this make (its DB is still being written)."""
    if make == "subaru":
        return False
    if os.path.exists(os.path.join(KB, f".{make}.done")):
        return False
    return subprocess.run(["pgrep", "-f", "build_charm_all.sh"], capture_output=True).returncode == 0


# ============================================================================ step: params
def romraider():
    """RomRaider logger v370 (GPL-2): SSM parameters (with ECU capability gating), switches, DTC blocks,
    per-ECU parameters. Gating: ecubyteindex counts the 0xBF reply after SYS ID + ROM ID (8 bytes), so the
    capability flag byte is ecubyteindex-8 and ecubit is a 0-based bit (same as FreeSSM's 1-based tables)."""
    root = ET.parse(ROMRAIDER_XML).getroot()
    proto = next(p for p in root.iter("protocol") if p.get("id") == "SSM")
    params, switches, dtcs, ecuparams = [], [], [], []
    for e in proto.find("parameters").findall("parameter"):
        a = e.find("address")
        convs = [(c.get("units"), c.get("expr")) for c in e.iter("conversion")]
        deps = [r.get("parameter") for r in e.iter("ref")]
        params.append(dict(id=e.get("id"), name=e.get("name"), desc=e.get("desc") or "",
                           address=a.text.strip() if a is not None else None,
                           length=int(a.get("length", 1)) if a is not None else None,
                           byte=e.get("ecubyteindex"), bit=e.get("ecubit"), convs=convs, deps=deps))
    for e in proto.find("switches").findall("switch"):
        switches.append(dict(id=e.get("id"), name=e.get("name"), address=e.get("byte"), bit=e.get("bit"),
                             byte=e.get("ecubyteindex")))
    for e in proto.find("dtcodes").findall("dtcode"):
        dtcs.append(dict(id=e.get("id"), name=e.get("name"), tmp=e.get("tmpaddr"), mem=e.get("memaddr"),
                         bit=e.get("bit")))
    for e in proto.find("ecuparams").findall("ecuparam"):
        convs = [(c.get("units"), c.get("expr")) for c in e.iter("conversion")]
        for ecu in e.findall("ecu"):
            a = ecu.find("address")
            for ecu_id in (ecu.get("id") or "").split(","):
                ecuparams.append(dict(id=e.get("id"), name=e.get("name"), ecu=ecu_id.strip(),
                                      address=a.text.strip() if a is not None else None,
                                      length=int(a.get("length", 1)) if a is not None else 1, convs=convs))
    return params, switches, dtcs, ecuparams


def pick_conv(convs, want_unit):
    """The conversion whose unit is the canonical one (or converts to it), else the first metric one."""
    for u, x in convs:
        if T.unit_norm(u) == want_unit:
            return u, x
    for u, x in convs:
        if T.convert(1.0, u, want_unit) is not None:
            return u, x
    for u, x in convs:
        if T.unit_norm(u) in T._UNIT:
            return u, x
    return convs[0] if convs else (None, None)


def gm_pids():
    """GM mode $22 definitions: kb/gm_mode22_pids.json when tools/chevy_extras.sh has made it, otherwise read
    PCMHammer (GPL-3) with the same parser (chevrolet/make_pipeline.pcmhammer_pids). DidNotWork entries are
    returned separately (kept as not_supported)."""
    bad = []
    bad_xml = os.path.join(PCMH_DIR, "Parameters.DidNotWork.xml")
    if os.path.exists(bad_xml):
        bad = [e.get("pid") or e.get("id") for e in ET.parse(bad_xml).getroot().iter("Parameter")]
    if os.path.exists(GM_PID_JSON):
        return json.load(open(GM_PID_JSON))["pids"], bad, os.path.basename(GM_PID_JSON)
    sys.path.insert(0, os.path.join(ROOT, "chevrolet"))
    from make_pipeline import pcmhammer_pids
    return (pcmhammer_pids(os.path.join(PCMH_DIR, "Parameters.Standard.xml"), bad_xml, "6C10F1"), bad,
            "PCMHammer Parameters.Standard.xml (kb/gm_mode22_pids.json not built yet)")


def obd_commands():
    """Every mode 01 / 02 / 03 / 06 / 07 / 09 command python-obd (GPL-2) defines: (mode, pid, name, desc)."""
    try:
        import obd
    except ImportError:
        return []
    out = []
    for mode in obd.commands.modes:
        for c in mode:
            if c is None:
                continue
            cmd = c.command.decode()
            out.append((cmd[:2], cmd[2:], c.name, c.desc))
    return out


def _gm_expr(eq):
    """PCMHammer equation (A, B, SIGNED(A) ...) -> evaluate()-able, or None when it uses anything else."""
    eq = eq.replace(" ", "")
    if re.fullmatch(r"[AB0-9.+\-*/%()]+", eq):
        return eq
    return None


def step_params(make):
    db = open_tdb(make)
    for t in ("telem_param", "telem_source_map", "telem_coverage", "telem_capability"):
        db.execute(f"DELETE FROM {t}")
    title = make.replace("_", " ").title()
    sources = {k: set() for k in T.CANON}
    rows = []                                              # telem_source_map rows

    # --- generic OBD-II (every make): SAE J1979 mode 01 scalar formulas + every python-obd command
    for pid, (n, expr, unit) in T.OBD01.items():
        key = T.canon_key("obd01", pid)
        rows.append(("obd01", pid, None, f"01{pid}", n, expr, unit, 1, key, None,
                     "SAE J1979 mode 01 (formula in app/telemetry_ref.OBD01, tested against python-obd)", None))
    cmds = obd_commands()
    named = {}
    for mode, pid, name, desc in cmds:
        if mode == "01":
            named[pid] = desc
            if pid not in T.OBD01:
                rows.append(("obd01", pid, desc, f"01{pid}", None, None, None, 0, T.canon_key("obd01", pid), None,
                             "python-obd command table (decoded by python-obd; no formula stored here)", name))
    rows.append(("obd01", "ATRV", "Adapter voltage at the OBD connector", "ATRV", None, None, "V", 1,
                 "adapter_volts", None, "ELM327/STN command set (adapter measures pin 16 itself)",
                 "not an ECU value: python-obd ELM_VOLTAGE"))

    # --- Subaru SSM2 (KKL; same addresses through the EX where SSM over the EX works)
    rr = None
    if make in SSM_MAKES and os.path.exists(ROMRAIDER_XML):
        rr = romraider()
        params, switches, dtcs, ecuparams = rr
        for p in params:
            key = T.canon_key("ssm", p["id"])
            want = T.CANON[key][1] if key in T.CANON else None
            u, x = pick_conv(p["convs"], want) if want else pick_conv(p["convs"], "")
            gate = f"flag byte {int(p['byte']) - 8}, bit {p['bit']}" if p["byte"] is not None else None
            note = ("depends on " + ",".join(p["deps"])) if p["deps"] else None
            rows.append(("ssm", p["id"], p["name"], p["address"], p["length"], x, T.unit_norm(u) if u else None, 1,
                         key, gate, "RomRaider logger_STD_EN_v370 (GPL-2)", note))
        for s in switches:
            rows.append(("ssm_switch", s["id"], s["name"], s["address"], 1, f"(x>>{s['bit']})&1", "on/off", 1,
                         f"ssm.{s['id']}", f"flag byte {int(s['byte']) - 8}" if s["byte"] else None,
                         "RomRaider logger_STD_EN_v370 (GPL-2)", None))
        for d in dtcs:
            rows.append(("ssm_dtc", d["id"], d["name"], f"{d['tmp']}/{d['mem']}", 1, f"(x>>{d['bit']})&1", "set", 1,
                         f"ssm.{d['id']}", None, "RomRaider logger_STD_EN_v370 (GPL-2)", "current/memorized address"))
        seen_e = set()
        for e in ecuparams:
            if e["id"] in seen_e:
                continue
            seen_e.add(e["id"])
            u, x = pick_conv(e["convs"], "")
            rows.append(("ssm_ecu", e["id"], e["name"], None, e["length"], x, T.unit_norm(u) if u else None, 1,
                         f"ssm.{e['id']}", "per ECU id (telem_capability.ecu_id)",
                         "RomRaider logger_STD_EN_v370 (GPL-2)", "address differs per ECU id"))

    # --- GM mode $22 (EX)
    gm_bad, gm_src = [], None
    if make in GM_MAKES:
        pids, gm_bad, gm_src = gm_pids()
        for e in pids:
            ident = e.get("key") or e["pid"]
            key = T.canon_key("gm22", ident)
            expr = _gm_expr(e["equation"]) if "labels" not in e else e["equation"]
            rows.append(("gm22", ident, e.get("name"), e["pid"], 2 if "B" in e["equation"] else 1, expr,
                         T.unit_norm(e.get("units")), 1, key, None, e.get("source") or gm_src,
                         "labels: " + "/".join(e["labels"]) if "labels" in e else
                         (None if expr else f"equation not plain arithmetic: {e['equation']}")))

    db.executemany("INSERT INTO telem_source_map(source,raw_id,name,address,nbytes,conversion,raw_unit,canonical,"
                   "param,gate,origin,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    # --- telem_param: canonical keys, then every protocol-only key with its definition's own name
    for r in rows:
        if r[8] in sources:
            sources[r[8]].add(r[0])
    for key, (name, unit, _, _, _) in T.CANON.items():
        if sources[key]:
            db.execute("INSERT INTO telem_param VALUES (?,?,?,?,?,?)",
                       (key, name, unit, None, ",".join(sorted(sources[key])), "value"))
    for src, raw_id, name, *_rest in rows:
        key = _rest[5]
        if key in T.CANON:
            continue
        unit = _rest[3]
        kind = {"ssm_switch": "switch", "ssm_dtc": "dtc_block"}.get(src, "value")
        nm = name or (named.get(raw_id) if src == "obd01" else None) or key
        db.execute("INSERT OR IGNORE INTO telem_param VALUES (?,?,?,?,?,?)",
                   (key, nm, T.canonical(1.0, unit)[1] if unit else None, None, src, kind))

    # --- telem_coverage: the protocol x cable facts (TELEMETRY_DB.md, checked 2026-10-25)
    BRIEF = "TELEMETRY_DB.md cable facts (checked 2026-10-25)"
    cov = [
        ("Subaru SSM2 over K-line", "SSM", "KKL", "maker", "built", BRIEF + "; app/ssm/telemetry_logger.py",
         "the only proven SSM2 path"),
        ("Subaru SSM2 over K-line", "SSM", "EX", "maker", "possible", BRIEF + "; app/ssm/ex_ssm2_probe.py",
         "unproven: NO DATA in the car test"),
        ("ISO 9141-2", "9141", "EX", "generic", "built", BRIEF + "; OBDLink EX (STN2230) protocol list", None),
        ("ISO 9141-2", "9141", "KKL", "generic", "possible", BRIEF, "K-line cable, generic OBD code not built"),
        ("ISO 14230-4 (KWP2000)", "14230", "EX", "generic", "built", BRIEF, None),
        ("ISO 14230-4 (KWP2000)", "14230", "KKL", "generic", "possible", BRIEF, "generic K-line code not built"),
        ("ISO 14230-4 (KWP2000)", "KWP", "EX", "generic", "built", BRIEF, None),
        ("SAE J1850 VPW", "J1850 VPW", "EX", "generic", "built", BRIEF, None),
        ("SAE J1850 VPW", "J1850 VPW", "EX", "maker", "built" if make in GM_MAKES else "not supported",
         BRIEF + "; GM mode $22 from PCMHammer" if make in GM_MAKES else BRIEF,
         "GM mode $22 (PCMHammer list)" if make in GM_MAKES else "no open maker definitions for this make"),
        ("SAE J1850 VPW", "J1850 VPW", "KKL", "generic", "not supported", BRIEF, "K-line only"),
        ("SAE J1850 PWM", "J1850 PWM", "EX", "generic", "built", BRIEF, None),
        ("SAE J1850 PWM", "J1850 PWM", "KKL", "generic", "not supported", BRIEF, "K-line only"),
        ("ISO 15765-4 CAN", "CAN", "EX", "generic", "built", BRIEF, None),
        ("ISO 15765-4 CAN", "CAN", "KKL", "generic", "not supported", BRIEF, "K-line only"),
        ("VW/Audi KWP1281 (K-line)", "KW1281", "KKL", "maker", "possible", BRIEF + "; kw1281test (MIT)",
         "not built, later work only"),
    ]
    db.executemany("INSERT INTO telem_coverage(protocol,protocol_match,cable,layer,status,source,notes) "
                   "VALUES (?,?,?,?,?,?,?)", cov)

    # --- telem_capability: defined rows per make
    cap = []
    for mode, pid, name, desc in cmds:
        key = T.canon_key("obd01", pid) if mode == "01" else f"obd{mode}.{pid or mode}"
        cap.append((title, None, 1996, None, None, None, "EX", "OBD-II", key, f"obd{mode}", pid or mode, "defined",
                    "SAE J1979 (python-obd command table); the car's own supported-PID bitmap decides"))
    if rr:
        params, switches, dtcs, ecuparams = rr
        for p in params:
            if p["address"]:
                cap.append((title, None, None, None, None, None, "KKL", "SSM2", T.canon_key("ssm", p["id"]), "ssm",
                            p["id"], "defined", "RomRaider SSM; the ECU's 0xBF capability bits decide"))
        for s in switches:
            cap.append((title, None, None, None, None, None, "KKL", "SSM2", f"ssm.{s['id']}", "ssm_switch", s["id"],
                        "defined", "RomRaider SSM switch"))
        for d in dtcs:
            cap.append((title, None, None, None, None, None, "KKL", "SSM2", f"ssm.{d['id']}", "ssm_dtc", d["id"],
                        "defined", "RomRaider SSM DTC block"))
        for e in ecuparams:
            cap.append((title, None, None, None, None, e["ecu"], "KKL", "SSM2", f"ssm.{e['id']}", "ssm_ecu", e["id"],
                        "defined", "RomRaider SSM per-ECU parameter"))
    if make in GM_MAKES:
        for r in rows:
            if r[0] == "gm22":
                cap.append((title, None, None, None, None, None, "EX", "J1850 VPW", r[8], "gm22", r[1], "defined",
                            r[10] + " (P01/P59-era PCMs; the PCM's reply decides)"))
        for b in gm_bad:
            cap.append((title, None, None, None, None, None, "EX", "J1850 VPW", T.canon_key("gm22", b), "gm22", b,
                        "not_supported", "PCMHammer Parameters.DidNotWork.xml"))
    db.executemany("INSERT INTO telem_capability(make,model,year_min,year_max,engine,ecu_id,cable,protocol,param,"
                   "source,raw_id,status,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", cap)
    db.execute("INSERT OR REPLACE INTO telem_meta VALUES ('params_built', ?)", (time.strftime("%F %T"),))
    db.commit()
    n = {s: c for s, c in db.execute("SELECT source, count(*) FROM telem_source_map GROUP BY source")}
    log(f"params {make}: {db.execute('SELECT count(*) FROM telem_param').fetchone()[0]} params, source map {n}, "
        f"{len(cap)} capability rows" + (f", GM from {gm_src}" if gm_src else ""))


# ============================================================================ step: zones
REF_TITLE = re.compile(r"engine condition data|specified data|i/o signal|scan tool data|scan data|data list|"
                       r"typical (data|scan|values)|normal scan|reference value|select monitor data|"
                       r"intake manifold vacuum|engine idle speed", re.I)
NUM = r"[-–−]?\d+(?:[.,]\d+)?"
DASH = r"\s*(?:—|–|-|to|~)\s*"
UNIT_RX = (r"(?:kpa|hpa|mmhg|inhg|psi|bar|°\s*[cf]|deg\s*[cf]|v|volts?|mv|millivolts?|%|rpm|g/s(?:ec)?|"
           r"lb/min|km/h|mph|ms|milliseconds?|deg(?:rees)?|°|a|steps?|counts?|hz)")
RANGE_RX = re.compile(rf"(?P<lo>{NUM})\s*(?:°\s*[CF]\b|°|{UNIT_RX}(?![a-z]))?{DASH}(?P<hi>{NUM})\s*\(?\s*(?P<u>{UNIT_RX}(?![a-z]))?\)?", re.I)
PM_RX = re.compile(rf"(?P<mid>{NUM})\s*±\s*(?P<tol>{NUM})\s*\(?\s*(?P<u>{UNIT_RX}(?![a-z]))?\)?", re.I)
LIMIT_RX = re.compile(rf"(?:(?P<pre>less than|more than|below|above|max(?:imum)?\.?|min(?:imum)?\.?)\s*)"
                      rf"(?P<v>{NUM})\s*\(?\s*(?P<u>{UNIT_RX}(?![a-z]))?\)?|"
                      rf"(?P<v2>{NUM})\s*\(?\s*(?P<u2>{UNIT_RX}(?![a-z]))?\)?\s*(?P<post>or less|or more|or lower|or higher)", re.I)


def _f(s):
    return float(s.replace("–", "-").replace("−", "-").replace(",", "."))


def _clean(s):
    s = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    s = s.replace(" ", " ").replace("&deg;", "°")
    return re.sub(r"\s+", " ", s).strip()


def parse_values(text, unit_hint=None):
    """Every range in one table cell: [(low, high, typical, unit, the matched text, condition text after it)].
    'a — b (unit): cond; c — d (unit): cond2', 'mid ± tol', 'less than X'. Discrete lists ('0 or 5') are left out."""
    out = []
    text = re.sub(r"°\s+([CF])\b", r"°\1", text).replace("°C", " °C").replace("°F", " °F")
    # '(–450 mmHg, –17.72 inHg)' after a kPa value is the same value in other units: one number is enough
    text = re.sub(r"\((?=[^()]*\d)[^()]*\b(?:mmhg|inhg|psi|°\s*f|kgf/cm2|kpa)\b[^()]*\)", " ", text, flags=re.I)
    text = re.sub(r"\(\s*varies[^)]*\)|\(\s*(typical|approx\w*|nominal)\s*\)", " ", text, flags=re.I)   # notes
    for part in re.split(r";|\s/\s(?=\d)|(?<=[A-Za-z°%)])\s*/\s*(?=[-–]?\d)", text):
        part = part.strip()
        if not part or re.search(r"\b\d+(?:\.\d+)?\s+or\s+\d", part):
            continue
        m = RANGE_RX.search(part)
        if m and not re.match(r"\s*\d", part[m.end():m.end() + 2] or " "):
            lo, hi = _f(m["lo"]), _f(m["hi"])
            if lo > hi:                               # a typo on the page ('650-75'): no zone, never a guess
                continue
            out.append((lo, hi, None, m["u"] or unit_hint, m.group(0), _cond_after(part, m.end()), part[:m.start()].strip(" :")))
            continue
        m = PM_RX.search(part)
        if m:
            mid, tol = _f(m["mid"]), abs(_f(m["tol"]))
            out.append((mid - tol, mid + tol, mid, m["u"] or unit_hint, m.group(0), _cond_after(part, m.end()),
                        part[:m.start()].strip(" :")))
            continue
        m = LIMIT_RX.search(part)
        if m:
            v = _f(m["v"] or m["v2"])
            word = (m["pre"] or m["post"] or "").lower()
            u = m["u"] or m["u2"] or unit_hint
            high_side = any(w in word for w in ("less", "below", "max", "lower"))
            b = part[:m.start()].strip(" :")
            out.append((None, v, None, u, m.group(0), _cond_after(part, m.end()), b) if high_side else
                       (v, None, None, u, m.group(0), _cond_after(part, m.end()), b))
    return out


def _cond_after(part, end):
    rest = part[end:].strip(" :)(")
    return rest[:120]


# ---- conditions: only phrases a Python check can follow. Thresholds the manual doesn't give are generic
# and marked so (warm = coolant at least 80 °C as in app/live_data.py; stated rpm = ±10 %).
WARM_C = 80.0
NOT_CHECKABLE = re.compile(r"(a/c|air condition\w*)\W{0,3}\W*(switch\W*)?(to\W*)?\W*on\b|when loaded|electrical load|"
                           r"head\s?lights? on|blower|steering wheel (fully )?turned", re.I)
VARIANT_RX = re.compile(r"\b(MT|AT|M/T|A/T)\b(?:\s*(?:model|vehicle)s?)?")      # upper case only ('at idle')
COND_RULES = [
    (re.compile(r"cranking", re.I), "cranking", "Cranking", {"engine": "cranking"}),
    (re.compile(r"(ignition (sw(itch)?|key) on|key on|koeo)[^.;]*engine (off|stopped)|engine off", re.I), "koeo",
     "Ignition on, engine off", {"engine": "off"}),
    (re.compile(r"(\d[\d,]{2,5})\s*rpm", re.I), "rpm{N}", "{N} rpm, no load", None),
    (re.compile(r"\bidl(e|ing)\b|engine on", re.I), "idle", "Idling", {"engine": "running", "speed": [None, 3]}),
]


def conditions_from(text, page_text=""):
    """[(name, label, definition, source_kind)] for one zone's condition text and its page's measuring
    condition ('After warm-up the engine' -> warm). Unrecognised text gives [] (the zone is not stored)."""
    t = text or ""
    if NOT_CHECKABLE.search(t):
        return []
    warm = bool(re.search(r"warm(ed)?[- ]?up|operating temperature|hot\b|fully warm", f"{t} {page_text}", re.I))
    for rx, name, label, defn in COND_RULES:
        m = rx.search(t)
        if not m:
            continue
        kind = "manual"
        if name == "rpm{N}":
            n = int(m.group(1).replace(",", ""))
            if n < 500:
                continue
            name, label = f"rpm{n}", f"{n} rpm, no load"
            defn = {"engine": "running", "rpm": [round(n * 0.9), round(n * 1.1)], "speed": [None, 3]}
            kind = "generic-tolerance"
        defn = dict(defn)
        if warm and name != "koeo" and name != "cranking":
            defn["ect"] = [WARM_C, None]
            name, label = name + "_warm", label + ", engine warm"
            kind = "generic-threshold" if kind == "manual" else kind
        return [(name, label, defn, kind)]
    return []


# ---- name -> canonical key: definition names first (the make's own protocol), then wording synonyms.
# Synonyms only translate wording into the definitions' terms (CLAUDE.md rule 1).
SYNONYMS = {
    "ect": ["engine coolant temperature", "coolant temperature", "coolant temp", "engine cool temp", "ect"],
    "iat": ["intake air temperature", "intake air temp", "iat"],
    "map": ["manifold absolute pressure", "intake manifold absolute pressure", "mani absolute pressure", "map",
            "intake manifold pressure"],
    "baro": ["barometric pressure", "atmospheric pressure", "atmosphere pressure", "baro"],
    "map_rel": ["manifold relative pressure", "intake manifold relative pressure", "mani relative pressure",
                "intake manifold vacuum", "manifold vacuum", "vacuum pressure"],
    "rpm": ["engine speed", "engine rpm", "rpm", "idle speed", "engine idle speed"],
    "speed": ["vehicle speed", "vss"],
    "tps": ["throttle position", "throt position", "throttle opening angle", "throttle angle", "tp angle", "tps"],
    "tps_v": ["throttle position sensor voltage", "throttle sensor voltage", "throttle position sensor signal"],
    "maf": ["mass air flow", "mass airflow", "maf", "air flow"],
    "maf_v": ["mass air flow sensor voltage", "air flow sensor voltage", "maf sensor voltage"],
    "map_v": ["manifold pressure sensor voltage", "map sensor voltage"],
    "stft1": ["short term fuel trim", "short term ft", "a/f correction #1", "short term fuel trim bank 1", "stft"],
    "ltft1": ["long term fuel trim", "long term ft", "a/f learning #1", "long term fuel trim bank 1", "ltft"],
    "stft2": ["short term fuel trim bank 2"],
    "ltft2": ["long term fuel trim bank 2"],
    "timing": ["ignition timing", "spark advance", "timing advance", "ignition total timing"],
    "ecu_volts": ["battery voltage", "ignition 1 signal", "ignition voltage", "control unit power supply",
                  "system voltage"],
    "o2_b1s1_v": ["front oxygen sensor", "front o2 sensor", "o2 sensor bank 1 sensor 1", "ho2s1"],
    "o2_b1s2_v": ["rear oxygen sensor", "rear o2 sensor", "o2 sensor bank 1 sensor 2", "ho2s2"],
    "desired_idle": ["desired idle", "desired idle speed"],
    "inj_pw1": ["fuel injection #1 pulse", "injector pulse width", "inj pulse width", "fuel injector pulse width"],
}
VOLT_SIBLING = {"tps": "tps_v", "maf": "maf_v", "map": "map_v"}


def _norm_name(s):
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s or "")       # 'tempera-ture' line breaks in PDF tables
    s = re.sub(r"[^a-z0-9#/% ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


class NameMap:
    def __init__(self, tdb, make):
        self.exact = {}
        src = "ssm" if make in SSM_MAKES else "gm22" if make in GM_MAKES else None
        if src:
            for raw_id, name, key in tdb.execute("SELECT raw_id, name, param FROM telem_source_map WHERE source=?",
                                                 (src,)):
                if name:
                    self.exact.setdefault(_norm_name(name), key)
            if src == "ssm" and os.path.exists(FREESSM_JSON):   # FreeSSM titles = the Select Monitor's names
                by_addr = {}
                for raw_id, addr, key in tdb.execute("SELECT raw_id, address, param FROM telem_source_map "
                                                     "WHERE source='ssm' AND address IS NOT NULL"):
                    by_addr.setdefault(int(addr, 16), key)
                for m in json.load(open(FREESSM_JSON))["measuring_blocks"]:
                    a = m["addr_high"] if m["addr_high"] is not None else m["addr_low"]
                    if a in by_addr:
                        self.exact.setdefault(_norm_name(m["title"]), by_addr[a])
        for key, (name, *_r) in T.CANON.items():
            self.exact.setdefault(_norm_name(name), key)
        self.syn = sorted(((_norm_name(w), k) for k, ws in SYNONYMS.items() for w in ws), key=lambda x: -len(x[0]))

    QUALIFIER = re.compile(r"\b(heater|power|supply|gnd|ground|shield|relay|solenoid|duty|switch|circuit|"
                           r"connector|terminal|resistance|current|frequency|waveform)\b|\(-\)")

    def key(self, name):
        n = _norm_name(name)
        if not n:
            return None
        if n in self.exact:
            return self.exact[n]
        for w, k in self.syn:
            if n == w or (len(w) > 4 and re.search(rf"\b{re.escape(w)}\b", n)):
                rest = n.replace(w, " ")
                if self.QUALIFIER.search(rest):
                    return None                   # 'oxygen sensor heater', 'sensor power supply': another quantity
                return k
        return None


def unit_ok(key, unit, tdb):
    """(key, unit) the zone can be stored under: the key's own unit family, or its voltage sibling."""
    u = T.unit_norm(unit) if unit else None
    if not u:
        return None
    r = tdb.execute("SELECT unit FROM telem_param WHERE key=?", (key,)).fetchone()
    want = r[0] if r else (T.CANON[key][1] if key in T.CANON else None)
    if want and T.convert(1.0, u, want) is not None:
        return key, u
    if u in ("V", "mV") and key in VOLT_SIBLING:
        return VOLT_SIBLING[key], u
    return None


def section_scopes(mdb, sid):
    """[(model, year_min, year_max, engine)] for a section: its vehicles, years run together per model/engine."""
    rows = mdb.execute("SELECT v.model, v.year, v.engine FROM section_vehicle sv JOIN vehicle v ON v.id=sv.vehicle_id "
                       "WHERE sv.section_id=? ORDER BY v.model, v.engine, v.year", (sid,)).fetchall()
    out, cur = [], None
    for m, y, e in rows:
        if cur and cur[0] == m and cur[3] == e and y <= cur[2] + 1:
            cur[2] = max(cur[2], y)
        else:
            if cur:
                out.append(tuple(cur))
            cur = [m, y, y, e]
    if cur:
        out.append(tuple(cur))
    return out or [(None, None, None, None)]


def md_rows(body):
    """Markdown table rows of an FSM section: lists of cells, with row-spanned blank cells filled from above."""
    rows, prev = [], None
    for line in body.splitlines():
        s = line.strip()
        if not s.startswith("|") or re.fullmatch(r"\|?[\s\-:|]+\|?", s):
            continue
        cells = [_clean(c) for c in s.strip("|").split("|")]
        if prev and len(prev) == len(cells) and cells and not cells[0]:
            cells[0] = prev[0]
        elif prev and cells and re.fullmatch(r"[-–]?\d+(\.\d+)?", cells[0]):
            cells = [prev[0]] + cells             # a second value line of the row above ('7.1 | — 14.2 | ...')
        rows.append(cells)
        prev = cells
    return rows


def charm_html(mdb, sid, env):
    r = mdb.execute("SELECT source_ptr FROM charm_source WHERE section_id=?", (sid,)).fetchone()
    if not r or env is None:
        return None
    with env.begin() as txn:
        p = txn.get(r[0].encode())
    return p.decode("utf-8", "replace") if p else None


def html_rows(page):
    """Rows of a CHARM page: real <table> rows, and the indent-column layout ('name<indent-24>units<indent-48>
    value', rows split by blank lines, continuation lines under their column)."""
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", page, re.S | re.I):
        cells = [_clean(c) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]
        if any(cells):
            rows.append(cells)
    body = re.sub(r"<span class='indent-(\d+)'>(?:&#09;|\t)</span>", lambda m: f"\x01{m.group(1)}\x02", page)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    for block in re.split(r"\n\s*\n", body):
        if "\x01" not in block:
            continue
        lines = [ln for ln in block.split("\n") if ln.strip()]
        cols = {}
        for ln in lines:
            segs = re.split(r"\x01(\d+)\x02", ln)
            pos = 0
            for i, seg in enumerate(segs):
                if i % 2:
                    pos = int(seg)
                    continue
                txt = _clean(seg)
                if txt:
                    cols.setdefault(pos if pos >= 10 else 0, []).append(txt)
        if len(cols) >= 2:
            rows.append([" ".join(cols[k]) for k in sorted(cols)])
    return rows


def header_conditions(rows):
    """(column index -> condition text, header width), from a header row naming conditions (ECM I/O tables)."""
    for r in rows[:4]:
        if any(re.search(r"\d", c) for c in r[1:]):
            break                                 # the data has started: no header after this
        hits = {i: c for i, c in enumerate(r) if conditions_from(c)}
        if (len(hits) >= 2 or (hits and not r[0])) and not any(re.search(r"\d", c) for c in r):
            return hits, len(r)
    return {}, 0


def zone_rows(rows, page_text):
    """(row name, value cell text, condition text, unit hint, quote) for each candidate cell of a table."""
    hdr_all, width = header_conditions(rows)
    units_col, head_unit = None, None
    for r in rows[:3]:
        m = re.search(r"\((V|mV|kPa|%|rpm|°C)\)|signal \((v)\)", " ".join(r), re.I)
        if m and not head_unit:
            head_unit = m.group(1) or m.group(2)
        for i, c in enumerate(r):
            if re.fullmatch(r"(units?( displayed)?|unit of measure)", c.strip(), re.I):
                units_col = i
    out = []
    for r in rows:
        if not r or r is rows[0] and not any(parse_values(c) for c in r[1:]):
            continue
        name = r[0]
        if len(r) > 2 and r[1] and not parse_values(r[1]) and len(r[1]) < 25 and units_col != 1 and \
                re.match(r"(signal|output|voltage)", r[1], re.I):
            name = f"{r[0]} {r[1]}"
        unit_hint = r[units_col] if units_col is not None and units_col < len(r) else None
        if unit_hint:
            m = re.match(rf"\s*({UNIT_RX})(?![a-z])", unit_hint, re.I)
            unit_hint = m.group(1) if m else None
        unit_hint = unit_hint or head_unit
        quote = " | ".join(c for c in r if c)
        if re.search(r"waveform", quote, re.I):
            continue                                      # pulsed signals: a meter value isn't a zone
        cells = [(i, c) for i, c in enumerate(r[1:], 1) if c and i != units_col]
        # the row's own condition text: cells with no value that aren't connector/terminal numbers or dashes
        own = " ".join(c for i, c in cells if not parse_values(c, unit_hint) and not
                       re.fullmatch(r"[A-Z]{0,2}\d{1,3}|[—–-]+|signal( \([+-]\))?|output|voltage", c.strip(), re.I)
                       and f"{r[0]} {c}" != name)
        vals = [(i, c) for i, c in cells if parse_values(c, unit_hint)]
        if vals:
            # value cells take the header's condition columns in order, only when the counts agree
            # (merged cells shift column numbers, so the index alone can't be trusted)
            conds = [hdr_all[k] for k in sorted(hdr_all)]
            if len(conds) == len(vals):
                out += [(name, c, own, conds[n], unit_hint, quote, None) for n, (i, c) in enumerate(vals)]
            elif not hdr_all:
                out += [(name, c, own, "", unit_hint, quote, None) for i, c in vals]
        elif cells and not hdr_all:                       # PDF tables that split one value over several cells
            out.append((name, " ".join(c for _, c in cells), "", "", unit_hint, quote, None))
    return out


CHECKABLE_PHRASES = re.compile(
    r"after (the )?(engine (is )?)?warm(ed)?[- ]?up( the engine)?|warm[- ]?up the engine|engine (is )?warm(ed up)?|"
    r"idl(e|ing)( speed)?|engine (on|off|running|stopped)|ignition (sw(itch)?|key)? ?(on|off)|key (on|off)|"
    r"\d[\d,]*\s*rpm|racing|no load|cranking|in [\"']?n[\"']? or [\"']?p[\"']?|(gear|select lever|shift lever)"
    r"( position)?( is)? in|neutral|position|range|a/c (is )?(turned )?\W*off\W*|all accessory switches "
    r"(are )?(turned )?off|accessor(y|ies) off|engine|with|the|and|is|are|or|of|at|in|to|then|(\(|\))", re.I)


def checkable(text):
    """True when the text holds nothing beyond conditions a Python check can follow (engine state, warm,
    a stated rpm, idle) and the standard test set-up (N/P, A/C and accessories off)."""
    rest = CHECKABLE_PHRASES.sub(" ", text or "")
    return not re.search(r"[A-Za-z]{2,}", rest)


def page_condition_text(text, charm=False):
    """The page's stated measuring condition. FSM pages: only a 'Measuring condition:' block counts. CHARM
    data-list pages state it in a sentence ('engine idling, upper radiator hose hot, closed throttle ...')."""
    m = re.search(r"measuring condition[s]?:?(.{0,400})", text, re.I | re.S)
    if m:
        return m.group(1)
    if not charm:
        return ""
    m = re.search(r"((engine (at |is )?idl\w+|closed throttle|upper radiator hose hot|operating temperature)"
                  r"[^\n]{0,300})", text, re.I)
    return m.group(1) if m else ""


def step_zones(make, limit=0):
    tdb = open_tdb(make)
    if not tdb.execute("SELECT 1 FROM telem_param LIMIT 1").fetchone():
        sys.exit("run the params step first")
    mdb = open_master(make)
    tdb.execute("DELETE FROM telem_zone")
    tdb.execute("DELETE FROM telem_zone_unmapped")
    tdb.execute("DELETE FROM telem_condition")
    names = NameMap(tdb, make)
    env = None
    try:
        import lmdb
        if os.path.exists(os.path.join(CHARM_MOUNT, "data.mdb")):
            env = lmdb.open(CHARM_MOUNT, readonly=True, lock=False, readahead=False, subdir=True, create=False)
    except ImportError:
        pass
    if env is None:
        log("zones: CHARM image not mounted (~/mnt/charm): CHARM data-list pages are skipped this run")
    cands = mdb.execute("SELECT s.id, s.title, s.path, s.page, s.body, d.kind, d.title FROM section s "
                        "JOIN doc d ON d.id=s.doc_id").fetchall()
    st = dict(pages=0, rows=0, zones=0, unmapped=0, no_cond=0, unit=0, html_missing=0)
    conds = {}
    for sid, title, path, page, body, kind, dtitle in cands:
        if not REF_TITLE.search(f"{title} {path}"):
            continue
        if kind == "charm":
            pg = charm_html(mdb, sid, env)
            if pg is None:
                st["html_missing"] += 1
                continue
            rows, text = html_rows(pg), _clean(pg)
        else:
            rows, text = md_rows(body), body
        measure = kind != "charm" and (not rows or re.search(r"measurement|idle speed|vacuum", f"{title} {path}", re.I))
        free = [(_clean(ln), ln) for ln in body.splitlines()
                if not ln.strip().startswith("|") and parse_values(_clean(ln))] if measure else []
        if not rows and not free:
            continue
        st["pages"] += 1
        pcond = page_condition_text(text, kind == "charm")
        if measure and re.search(r"warm[- ]?up the engine|after warm", body, re.I):
            pcond = (pcond + " After warm-up the engine.").strip()
        items = zone_rows(rows, pcond) if rows else []
        # free-text specs on measurement pages ('Vacuum pressure (at idling, A/C "OFF"): Less than -60.0 kPa')
        topic = re.sub(r"^\d+\.\s*", "", (path.split(" > ")[-2] if " > " in path else title))
        blines = body.splitlines()
        for i, (cl, raw) in enumerate(free):
            j = blines.index(raw) if raw in blines else 0
            # the spec's label is the nearest line above ending in ':'; a lone '(MT model)' line under the value
            # says which transmission it is for
            prev = next((_clean(x) for x in reversed(blines[max(0, j - 8):j]) if _clean(x).rstrip("*").endswith(":")), "")
            nxt = next((_clean(x) for x in blines[j + 1:j + 4] if _clean(x)), "")
            vm = VARIANT_RX.fullmatch(nxt.strip("*() ").replace(" model", "").replace(" vehicle", "")) if nxt else None
            items.append((topic, cl, "", prev, None, f"{prev} {cl}" + (f" {nxt}" if vm else ""),
                          vm.group(1).replace("/", "") if vm else None))
        scopes = section_scopes(mdb, sid)
        for name, cell, own, ctext, uhint, quote, vhint in items:
            st["rows"] += 1
            if re.search(r"\((except|only|for)\b[^)]*\bmodels?\)", name, re.I):
                tdb.execute("INSERT INTO telem_zone_unmapped VALUES (?,?,?,?)", (sid, name, quote[:300], "model qualifier"))
                continue                                  # '(Except U5 model)': which cars isn't known here
            key = names.key(name) or names.key(topic if not rows else "")
            if not key:
                st["unmapped"] += 1
                tdb.execute("INSERT INTO telem_zone_unmapped VALUES (?,?,?,?)", (sid, name, quote[:300], "name"))
                continue
            for lo, hi, typ, unit, mtext, after, before in parse_values(cell, uhint):
                mine = f"{own} {before} {after}".strip()
                if mine and not checkable(mine):
                    cl = []                               # the row names a state Python can't check
                else:
                    cl = conditions_from(f"{ctext} {mine}", f"{pcond} {mine}") if (ctext or mine) else \
                        conditions_from(pcond, pcond)
                if not cl:
                    st["no_cond"] += 1
                    tdb.execute("INSERT INTO telem_zone_unmapped VALUES (?,?,?,?)",
                                (sid, name, quote[:300], "no checkable condition"))
                    continue
                ku = unit_ok(key, unit, tdb)
                if not ku:
                    st["unit"] += 1
                    tdb.execute("INSERT INTO telem_zone_unmapped VALUES (?,?,?,?)",
                                (sid, name, quote[:300], f"unit '{unit}' doesn't fit {key}"))
                    continue
                k2, u2 = ku
                vs = {v.upper().replace("/", "") for v in VARIANT_RX.findall(f"{ctext} {mine}")}
                variant = vhint or (vs.pop() if len(vs) == 1 else None)
                # Python check: every number stored must appear in the quoted source text
                qnums = {round(_f(x), 4) for x in re.findall(NUM, quote.replace("°", " "))}
                for v in (lo, hi):
                    if v is not None and round(v, 4) not in qnums and round(-v, 4) not in qnums and typ is None:
                        break
                else:
                    cname, clabel, cdef, ckind = cl[0]
                    lo, hi, typ = (round(x, 4) if x is not None else None for x in (lo, hi, typ))
                    conds[cname] = (clabel, cdef, ckind, (pcond or ctext)[:300])
                    for model, y0, y1, eng in scopes:
                        tdb.execute("INSERT INTO telem_zone(param,model,year_min,year_max,engine,condition,low,high,"
                                    "typical,unit,source_kind,section_id,page,quote,source_ref,row_name,method,variant) "
                                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (k2, model, y0, y1, eng, cname, lo, hi, typ, u2,
                                     "charm" if kind == "charm" else "manual", sid, page, quote[:500], dtitle,
                                     name, "table" if (rows and not free) else "spec line", variant))
                        st["zones"] += 1
                    continue
                st["unmapped"] += 1
                tdb.execute("INSERT INTO telem_zone_unmapped VALUES (?,?,?,?)", (sid, name, quote[:300], "number check"))
        if limit and st["pages"] >= limit:
            break
    generic_zones(tdb, conds)
    for n, (label, d, k, q) in conds.items():
        tdb.execute("INSERT OR REPLACE INTO telem_condition VALUES (?,?,?,?,?)", (n, label, json.dumps(d), k, q))
    tdb.execute("INSERT OR REPLACE INTO telem_meta VALUES ('zones_built', ?)", (time.strftime("%F %T"),))
    tdb.commit()
    log(f"zones {make}: {st}")


def generic_zones(tdb, conds):
    """Fallback ranges already in app/live_data.py (generic OBD-II / common practice, gasoline engines), marked
    'generic'. The apps prefer any manual or CHARM zone for the same parameter and condition."""
    LD = "app/live_data.py generic range (not from this car's manual)"
    rows = [
        ("ect", "running_any", 80, 105, "°C", "Coolant 80-105 C: normal operating temperature"),
        ("stft1", "idle_warm", -10, 10, "%", "fuel trim within ±10 %: normal"),
        ("ltft1", "idle_warm", -10, 10, "%", "fuel trim within ±10 %: normal"),
        ("stft2", "idle_warm", -10, 10, "%", "fuel trim within ±10 %: normal"),
        ("ltft2", "idle_warm", -10, 10, "%", "fuel trim within ±10 %: normal"),
        ("rpm", "idle_warm", 550, 1000, "rpm", "550-1000 rpm: normal warm idle"),
        ("map", "idle_warm", None, 40, "kPa", "manifold pressure at idle 40 kPa or less: strong vacuum, normal"),
        ("ecu_volts", "running_any", 13.3, 14.9, "V", "13.3-14.9 V with the engine running: charging normally"),
        ("ecu_volts", "koeo", 12.4, None, "V", "12.4 V or more, engine off: battery charged"),
        ("adapter_volts", "running_any", 13.3, 14.9, "V", "13.3-14.9 V with the engine running: charging normally"),
        ("adapter_volts", "koeo", 12.4, None, "V", "12.4 V or more, engine off: battery charged"),
    ]
    conds.setdefault("idle_warm", ("Idling, engine warm", {"engine": "running", "speed": [None, 3],
                                                             "ect": [WARM_C, None]}, "generic-threshold", None))
    conds.setdefault("running_any", ("Engine running", {"engine": "running"}, "generic", None))
    conds.setdefault("koeo", ("Ignition on, engine off", {"engine": "off"}, "generic", None))
    for key, cond, lo, hi, unit, q in rows:
        tdb.execute("INSERT INTO telem_zone(param,model,year_min,year_max,engine,condition,low,high,typical,unit,"
                    "source_kind,section_id,page,quote,source_ref,row_name,method) "
                    "VALUES (?,NULL,NULL,NULL,NULL,?,?,?,NULL,?,'generic',NULL,NULL,?,?,?,'generic')",
                    (key, cond, lo, hi, unit, q, LD, key))


# ============================================================================ step: paths
DIR_RULES = [("low", re.compile(r"\b(low input|low voltage|circuit low|too low|less than|lower than|below|"
                                  r"or less|under|low)\b", re.I)),
             ("high", re.compile(r"\b(high input|high voltage|circuit high|too high|more than|higher than|greater "
                                   r"than|above|or more|over|high)\b", re.I)),
             ("no_reading", re.compile(r"\b(no signal|no activity|open circuit|circuit open|circuit malfunction|"
                                         r"no response|signal lost|loss of signal)\b", re.I)),
             ("unstable", re.compile(r"\b(range/performance|range/perf|range performance|erratic|intermittent|unstable|"
                                       r"fluctuat\w*|stuck)\b", re.I))]
INSPECT_RX = re.compile(r"\b(inspection|testing and inspection|component tests?|check(ing)?|test)\b", re.I)


def directions(text):
    return [d for d, rx in DIR_RULES if rx.search(text)] or ["any"]


def path_terms(tdb, make):
    """Parameter -> the words the manual uses for it: its definition names plus the wording synonyms."""
    terms = {}
    for key, name in tdb.execute("SELECT key, name FROM telem_param WHERE kind='value'"):
        ws = {_norm_name(name)} | {_norm_name(w) for w in SYNONYMS.get(key, [])}
        ws = {w for w in ws if len(w) >= 6 and not re.fullmatch(r"[a-z]?\d+", w)}   # skip 'map', 'rpm', ids
        if ws:
            terms[key] = sorted(ws, key=len, reverse=True)
    return terms


def _sentences(text):
    for s in re.split(r"(?<=[.?!])\s+|\n+", text):
        s = _clean(s)
        if 12 <= len(s) <= 600:
            yield s


def step_paths(make):
    tdb = open_tdb(make)
    mdb = open_master(make)
    tdb.execute("DELETE FROM telem_path")
    if tdb.execute("SELECT 1 FROM sqlite_master WHERE name='telem_path_vec'").fetchone():
        import sqlite_vec                                 # vectors key on telem_path ids: rebuilt by 'embed'
        tdb.enable_load_extension(True)
        sqlite_vec.load(tdb)
        tdb.execute("DROP TABLE telem_path_vec")
    terms = path_terms(tdb, make)
    owner = {}                                            # manual term -> parameter
    for key, ws in terms.items():
        for w in ws:
            owner.setdefault(w, key)
    big = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(owner, key=len, reverse=True)) + r")\b")

    after_q = re.compile(r"^\W*(sensor\W+)?(heater|relay|solenoid|power|supply|switch|ground|shield)\b")

    def keys_in(norm_text):
        # 'oxygen sensor heater', 'pressure sensor power supply': a different quantity from the sensor's reading
        return {owner[m.group(1)] for m in big.finditer(norm_text) if not after_q.search(norm_text[m.end():])}

    st = dict(dtc_title=0, dtc_step=0, inspection=0)
    rows = []
    # 1. DTC procedures: the DTC's own name names the parameter (or its sensor), or a step's check reads it
    dtcname = {c: d or "" for c, d in mdb.execute("SELECT code, description FROM dtc")}
    for code, sid, title, body, kind in mdb.execute(
            "SELECT DISTINCT p.code, s.id, s.title, s.body, d.kind FROM dtc_procedure p JOIN section s ON s.id=p.section_id "
            "JOIN doc d ON d.id=s.doc_id"):
        ev = _clean(f"{code} {dtcname.get(code) or title}")   # the code's own name, or its page title
        for key in keys_in(_norm_name(ev)):
            for d in directions(ev):
                rows.append((key, d, sid, None, "dtc", "dtc_title", ev[:400], 0))
            st["dtc_title"] += 1
        if kind == "charm":
            continue
        for line in body.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [_clean(c) for c in line.strip().strip("|").split("|")]
            if len(cells) >= 5 and cells[0].isdigit() and re.search(r"\d", cells[2]):
                for key in keys_in(_norm_name(cells[2])):
                    for d in directions(cells[2]):
                        rows.append((key, d, sid, int(cells[0]), "dtc", "dtc_step", f"Step {cells[0]}: {cells[2]}"[:400], 1))
                    st["dtc_step"] += 1

    # 2. component inspections: a section about the parameter's sensor whose title says it inspects/tests it
    for sid, title, path, body in mdb.execute("SELECT id, title, path, body FROM section"):
        if not INSPECT_RX.search(f"{title} {path}"):
            continue
        np_ = _norm_name(path)
        hits = [k for k in keys_in(np_) if any(f"{w} sensor" in np_ for w in terms[k])]
        if not hits:
            continue
        ev = next((x for x in _sentences(body) if re.search(r"(Ω|ohm|\bV\b|volt|kPa|resistance|voltage)", x)), "")
        if ev:
            for key in hits:
                rows.append((key, "any", sid, None, "inspection", "component_title", f"{path}: {ev}"[:400], 2))
                st["inspection"] += 1

    seen, n, scopes = set(), 0, {}
    for key, d, sid, step, kind, method, ev, rank in rows:
        if sid not in scopes:
            scopes[sid] = section_scopes(mdb, sid)
        for model, y0, y1, eng in scopes[sid]:
            k = (key, d, sid, step, model, y0, eng)
            if k in seen:
                continue
            seen.add(k)
            tdb.execute("INSERT INTO telem_path(param,direction,model,year_min,year_max,engine,section_id,step,kind,"
                        "method,evidence,rank) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, d, model, y0, y1, eng, sid, step, kind, method, ev, rank))
            n += 1
    tdb.execute("INSERT OR REPLACE INTO telem_meta VALUES ('paths_built', ?)", (time.strftime("%F %T"),))
    tdb.commit()
    log(f"paths {make}: {n} scoped rows from {st}")


# ============================================================================ step: embed
def gpu_free():
    r = subprocess.run(["bash", "-c", f"source {ROOT}/tools/gpu_guard.sh && gpu_free"], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout or r.stderr).strip()


def step_embed(make, device="auto"):
    tdb = open_tdb(make)
    rows = tdb.execute("SELECT p.id, p.param, p.direction, p.evidence, coalesce(m.name, p.param) FROM telem_path p "
                       "LEFT JOIN telem_param m ON m.key=p.param").fetchall()
    if not rows:
        log(f"embed {make}: no pathways")
        return
    if device == "auto":
        ok, why = gpu_free()
        device = "cuda" if ok else "cpu"
        log(f"embed {make}: {'GPU free' if ok else why + '; using the CPU'}")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")          # the embedding model is already cached
    import numpy as np
    import sqlite_vec
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device=device)
    tdb.enable_load_extension(True)
    sqlite_vec.load(tdb)
    tdb.execute("DROP TABLE IF EXISTS telem_path_vec")
    tdb.execute("CREATE VIRTUAL TABLE telem_path_vec USING vec0(id INTEGER PRIMARY KEY, embedding int8[1024])")
    texts = {}
    for i, key, d, ev, name in rows:
        texts.setdefault(f"{name} reading {d.replace('_', ' ')}. {ev}", []).append(i)
    uniq = list(texts)
    t0 = time.time()
    for b in range(0, len(uniq), 64):
        chunk = uniq[b:b + 64]
        v = model.encode(chunk, convert_to_numpy=True, normalize_embeddings=True)[:, :1024]
        q = np.clip(np.round(v * 127), -127, 127).astype(np.int8)
        for txt, vec in zip(chunk, q):
            for i in texts[txt]:
                tdb.execute("INSERT INTO telem_path_vec(id, embedding) VALUES (?, vec_int8(?))", (i, vec.tobytes()))
    tdb.commit()
    log(f"embed {make}: {len(rows)} pathways ({len(uniq)} distinct texts) on {device} in {time.time() - t0:.0f} s")


# ============================================================================ step: merge
def step_merge(make):
    if build_in_progress(make):
        log(f"merge {make}: SKIPPED, the CHARM build hasn't finished kb/{make}.db yet (no .{make}.done)")
        return
    src = tdb_path(make)
    dst = master_path(make)
    if not os.path.isfile(src) or not os.path.isfile(dst):
        sys.exit(f"merge needs {src} and {dst}")
    m = sqlite3.connect(dst, timeout=120)
    have = [r[0] for r in m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'telem_%' "
                                    "AND name NOT LIKE 'telem_path_vec_%'")]
    if have:                                  # back up the previous copy (the rest of the DB is untouched)
        os.makedirs(os.path.join(OUT, "backup"), exist_ok=True)
        bk = os.path.join(OUT, "backup", f"{make}.telem.{time.strftime('%Y%m%d_%H%M')}.db")
        b = sqlite3.connect(bk)
        b.close()
        m.execute("ATTACH ? AS bk", (bk,))
        for t in have:
            if t != "telem_path_vec":
                m.execute(f"CREATE TABLE bk.{t} AS SELECT * FROM main.{t}")
        m.commit()
        m.execute("DETACH bk")
        log(f"merge {make}: previous telem_* copy saved to {bk}")
    try:
        import sqlite_vec
        m.enable_load_extension(True)
        sqlite_vec.load(m)
        vec = True
    except Exception:
        vec = False
    m.execute("ATTACH ? AS t", (src,))
    with m:
        for t in have:
            m.execute(f"DROP TABLE IF EXISTS main.{t}")
        for (sql,) in m.execute("SELECT sql FROM t.sqlite_master WHERE type IN ('table','index') AND name LIKE 'telem_%' "
                                "AND name NOT LIKE 'telem_path_vec%' AND sql IS NOT NULL").fetchall():
            m.execute(sql)
        for t in TELEM_TABLES:
            m.execute(f"INSERT INTO main.{t} SELECT * FROM t.{t}")
        if vec and m.execute("SELECT 1 FROM t.sqlite_master WHERE name='telem_path_vec'").fetchone():
            m.execute("CREATE VIRTUAL TABLE main.telem_path_vec USING vec0(id INTEGER PRIMARY KEY, embedding int8[1024])")
            m.execute("INSERT INTO main.telem_path_vec(id, embedding) SELECT id, embedding FROM t.telem_path_vec")
    m.execute("DETACH t")
    n = {t: m.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("telem_zone", "telem_path")}
    m.close()
    log(f"merge {make}: telem_* tables replaced in {dst} {n}")


# ============================================================================ step: report
def step_report(make, sample=20, seed=None):
    tdb = open_tdb(make)
    tdb.row_factory = sqlite3.Row
    mdb = open_master(make)
    out = [f"# Telemetry report: {make} ({time.strftime('%F %T')})", ""]
    params = [r["key"] for r in tdb.execute("SELECT key FROM telem_param WHERE kind='value'")]
    kinds = {}
    for r in tdb.execute("SELECT param, source_kind, count(*) n FROM telem_zone GROUP BY param, source_kind"):
        kinds.setdefault(r["param"], {})[r["source_kind"]] = r["n"]
    paths = {r[0]: r[1] for r in tdb.execute("SELECT param, count(DISTINCT section_id) FROM telem_path GROUP BY param")}
    cnt = dict(manual=0, charm=0, generic=0, none=0)
    for p in params:
        k = kinds.get(p, {})
        cnt["manual" if k.get("manual") else "charm" if k.get("charm") else "generic" if k.get("generic") else "none"] += 1
    out += [f"Parameters: {len(params)}; with a manual zone {cnt['manual']}, CHARM zone {cnt['charm']}, "
            f"generic only {cnt['generic']}, none {cnt['none']}.",
            f"Pathways: {sum(paths.values())} manual sections across {len(paths)} parameters.", "",
            "| parameter | manual | CHARM | generic | path sections |", "|---|---|---|---|---|"]
    for p in sorted(set(kinds) | set(paths)):
        k = kinds.get(p, {})
        out.append(f"| {p} | {k.get('manual', 0)} | {k.get('charm', 0)} | {k.get('generic', 0)} | {paths.get(p, 0)} |")
    out += ["", "## What each cable can read (defined in open definitions)", "",
            "| cable | protocol | source | defined | not_supported |", "|---|---|---|---|---|"]
    for r in tdb.execute("SELECT cable, protocol, source, sum(status='defined') d, sum(status='not_supported') ns "
                         "FROM telem_capability GROUP BY cable, protocol, source"):
        out.append(f"| {r['cable']} | {r['protocol']} | {r['source']} | {r['d']} | {r['ns']} |")
    out += ["", "## Same quantity, both cables: raw definitions side by side", "",
            "| key | SSM2 (RomRaider) | OBD-II mode 01 (J1979) | GM mode $22 (PCMHammer) |", "|---|---|---|---|"]
    for key in T.CANON:
        defs = {}
        for r in tdb.execute("SELECT source, raw_id, address, conversion, raw_unit FROM telem_source_map WHERE param=? "
                             "AND source IN ('ssm','obd01','gm22')", (key,)):
            defs.setdefault(r["source"], []).append(f"{r['raw_id']} @{r['address']}: {r['conversion']} {r['raw_unit'] or ''}")
        if len(defs) >= 2:
            out.append(f"| {key} | {'; '.join(defs.get('ssm', ['-']))} | {'; '.join(defs.get('obd01', ['-']))} | "
                       f"{'; '.join(defs.get('gm22', ['-']))} |")
    rnd = random.Random(seed)
    out += ["", f"## Spot-check sample ({sample} zones, {sample} pathways)",
            "Mechanical check: quote found on the cited page, and the stored numbers in the quote. "
            "Read each against its page for the real verdict.", ""]
    zs = tdb.execute("SELECT * FROM telem_zone WHERE section_id IS NOT NULL").fetchall()
    ok = 0
    for z in rnd.sample(zs, min(sample, len(zs))):
        body = mdb.execute("SELECT body, path, page FROM section WHERE id=?", (z["section_id"],)).fetchone()
        nums = [x for x in (z["low"], z["high"]) if x is not None]
        page_text = _clean(body["body"]) if body else ""
        qn = {round(_f(x), 4) for x in re.findall(NUM, z["quote"])}
        pn = {round(_f(x), 4) for x in re.findall(NUM, page_text)}
        typ = z["typical"] is not None                    # 'mid ± tol' rows store mid-tol / mid+tol
        hit = bool(body) and (typ or all(round(x, 4) in qn for x in nums))
        on_page = bool(body) and (z["source_kind"] == "charm" or typ or all(round(x, 4) in pn for x in nums))
        ok += hit and on_page
        out.append(f"- [{'ok' if hit and on_page else 'CHECK'}] {z['param']} {z['low']}..{z['high']} {z['unit']} "
                   f"@{z['condition']} ({z['model']} {z['year_min']}-{z['year_max']} {z['engine'] or ''}) "
                   f"sec {z['section_id']} p.{z['page']} {body['path'] if body else '?'} :: \"{z['quote'][:160]}\"")
    ps = tdb.execute("SELECT * FROM telem_path").fetchall()
    okp = 0
    for p in rnd.sample(ps, min(sample, len(ps))):
        body = mdb.execute("SELECT body, path FROM section WHERE id=?", (p["section_id"],)).fetchone()
        ev = p["evidence"]
        if p["method"] == "component_title" and body and ev.startswith(body["path"] + ": "):
            ev = ev[len(body["path"]) + 2:]               # component links: 'page path: quoted sentence'
        frag = re.sub(r"^Step \d+: ", "", ev)[:40]
        hit = bool(body) and (_clean(frag) in _clean(body["body"]) or p["method"] == "dtc_title")
        okp += hit
        out.append(f"- [{'ok' if hit else 'CHECK'}] {p['param']} {p['direction']} -> sec {p['section_id']} "
                   f"{body['path'] if body else '?'} ({p['method']}{' step ' + str(p['step']) if p['step'] else ''}) "
                   f":: \"{p['evidence'][:160]}\"")
    out.insert(2, f"Spot-check (mechanical): zones {ok}/{min(sample, len(zs))}, pathways {okp}/{min(sample, len(ps))}")
    unm = tdb.execute("SELECT reason, count(*) FROM telem_zone_unmapped GROUP BY reason ORDER BY 2 DESC").fetchall()
    out += ["", "## Reference rows not stored (and why)", ""] + [f"- {r[0]}: {r[1]}" for r in unm]
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    path = os.path.join(ROOT, "logs", f"telemetry_report_{make}.md")
    open(path, "w").write("\n".join(out) + "\n")
    log(f"report {make}: {cnt}; pathways {sum(paths.values())}; spot-check zones {ok}/{min(sample, len(zs))}, "
        f"paths {okp}/{min(sample, len(ps))} -> {path}")


def _numstr(x):
    return f"{x:g}" if x == int(x) or len(f"{x:g}") <= 6 else f"{x}"


# ============================================================================ replay
def step_replay(make, session_db, car_text):
    """Recorded READ CAR / TEST DRIVE samples (the app's session DB, table drive_sample) through normalize + judge
    for one car ('Model,Year[,Engine]'). The reader isn't stored with old samples, so both label sets are tried
    and the one that reads more values is used. Prints a status count per parameter."""
    if not os.path.isfile(session_db):
        sys.exit(f"{session_db} not found (copy the deck's ~/subaru_cyberdeck/session_active.db here first)")
    parts = [x.strip() for x in (car_text or "").split(",")]
    car = {"model": parts[0] if parts else None, "year": parts[1] if len(parts) > 1 else None,
           "engine": parts[2] if len(parts) > 2 else None, "db": master_path(make)}
    if not T.has_tables(car["db"]):
        car["db"] = tdb_path(make)
    s = sqlite3.connect(f"file:{session_db}?mode=ro", uri=True)
    if not s.execute("SELECT 1 FROM sqlite_master WHERE name='drive_sample'").fetchone():
        sys.exit("no drive_sample table in that session DB (no test drive recorded yet)")
    counts, n = {}, 0
    for drive_id, t, codes, live in s.execute("SELECT drive_id, t, codes, live FROM drive_sample ORDER BY t"):
        best = max(("ssm", "obd01"), key=lambda src: len(T.from_live_text(live, src)))
        rs = T.normalize(T.from_live_text(live, best), best, t=t)
        n += 1
        for j in T.judge(rs, car):
            c = counts.setdefault(j["key"], {})
            c[j["status"]] = c.get(j["status"], 0) + 1
    log(f"replay {make} {car_text}: {n} samples")
    for k, c in sorted(counts.items()):
        log(f"  {k}: " + ", ".join(f"{a} {b}" for a, b in sorted(c.items())))


# ============================================================================ main
def main():
    global LOG
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+", choices=["params", "zones", "paths", "embed", "merge", "report", "replay"])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--make", help="make DB name (kb/<make>.db), e.g. subaru, chevrolet, land_rover")
    g.add_argument("--all", action="store_true", help="every make, in the RUNBOOK's build order")
    ap.add_argument("--device", default="auto", help="embed: auto (GPU if gpu_guard says free) / cpu / cuda")
    ap.add_argument("--limit", type=int, default=0, help="zones: stop after N reference pages (testing)")
    ap.add_argument("--seed", type=int, default=None, help="report: spot-check sample seed")
    ap.add_argument("--session", default=os.path.join(ROOT, "session_active.db"), help="replay: the app's session DB")
    ap.add_argument("--car", default="", help="replay: 'Model,Year[,Engine]' the samples came from")
    a = ap.parse_args()
    makes = makes_in_order() if a.all else [a.make]
    for make in makes:
        LOG = os.path.join(ROOT, "logs", f"telemetry_{make}.log")
        if not os.path.isfile(master_path(make)):
            log(f"{make}: kb/{make}.db not found, skipped")
            continue
        if being_written(make) and any(s in a.steps for s in ("zones", "paths", "merge", "report")):
            log(f"{make}: the CHARM build is writing kb/{make}.db now; only params run (re-run when it is done)")
            steps = [s for s in a.steps if s in ("params", "embed")]
        else:
            steps = a.steps
        for s in steps:
            t0 = time.time()
            {"params": lambda: step_params(make), "zones": lambda: step_zones(make, a.limit),
             "paths": lambda: step_paths(make), "embed": lambda: step_embed(make, a.device),
             "merge": lambda: step_merge(make), "report": lambda: step_report(make, seed=a.seed),
             "replay": lambda: step_replay(make, a.session, a.car)}[s]()
            log(f"{make}: {s} done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
