#!/usr/bin/env python3
"""
make_pipeline.py: build one make's knowledge DB (kb/<slug>.db) the way subaru.db was built, driven by a
profile JSON (profiles/*.json). Nothing car-specific is in this file; the Tahoe lives in its profile.

Runs on the PC (the cloud can't reach NHTSA, archive.org, Reddit or vPIC). Every step is resumable.

  python3 make_pipeline.py PROFILE check        what's installed / downloaded / mounted, pass or fail
  python3 make_pipeline.py PROFILE nhtsa        TSBs, complaints, recalls from raw/nhtsa/*.zip -> <slug>_diag.db
  python3 make_pipeline.py PROFILE vin          NHTSA vPIC: WMI -> make into kb/vin.db (+ decode profile VINs)
  python3 make_pipeline.py PROFILE charm-list   which CHARM make/model names match the profile (read-only)
  python3 make_pipeline.py PROFILE kb           build_subaru_kb.py charm + diag into kb/<slug>.db, DTC names, finish
  python3 make_pipeline.py PROFILE reddit-get   pick the profile's subreddits out of the Pushshift torrent (aria2c)
  python3 make_pipeline.py PROFILE reddit       load those dumps (+ --topup: Arctic Shift API after the dumps end)
  python3 make_pipeline.py PROFILE forums       Wayback Machine forum pull, robots.txt-gated, per forum DB
  python3 make_pipeline.py PROFILE gm-pids [XLSX] Mode 22 PIDs from PCMHammer (+ community sheet) -> kb/gm_mode22_pids.json
  python3 make_pipeline.py PROFILE embed        (GPU, after the Subaru bake) Qwen3-Embedding-0.6B vectors
  python3 make_pipeline.py PROFILE status       one short report of everything above

It reuses the Subaru scripts as libraries (build_subaru_diag_db.py, scrape_reddit_subaru.py,
load_pushshift_subaru.py, kb/build_subaru_kb.py, cyberdeck_forge/charm_ingest.py) without editing them.
Manual-derived output (kb/<slug>.db) is personal use only, like subaru.db.
"""
import argparse
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "cyberdeck-research/1.0 (personal offline repair knowledge base; ~1 req/s)"
AI_BOTS = ("gptbot", "chatgpt-user", "claudebot", "claude-web", "anthropic-ai", "ccbot", "google-extended",
           "perplexitybot", "bytespider", "omgilibot", "facebookbot", "cohere-ai", "amazonbot", "applebot-extended")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


class Ctx:
    def __init__(self, a):
        self.a = a
        p = a.profile if os.path.isabs(a.profile) or os.path.exists(a.profile) else os.path.join(HERE, "profiles", a.profile)
        with open(p) as f:
            self.p = json.load(f)
        self.root = os.path.expanduser(a.root)
        self.slug = self.p["slug"]
        self.y0, self.y1 = (int(x) for x in (self.p["years"] + "-" + self.p["years"]).split("-")[:2])
        self.kb_db = os.path.join(self.root, "kb", f"{self.slug}.db")
        self.diag_db = os.path.join(self.root, f"{self.slug}_diag.db")
        self.builder = os.path.join(self.root, "kb", "build_subaru_kb.py")
        self.venv_py = os.path.join(self.root, "venv", "bin", "python")
        sys.path[:0] = [self.root, os.path.join(self.root, "kb"), os.path.join(self.root, "cyberdeck_forge")]

    def path(self, rel):
        return os.path.join(self.root, rel)


def http_get(url, timeout=120, tries=6, delay=1.0, _last=[0.0]):
    """GET with ~1 req/s pacing and backoff. Returns (status, bytes, headers)."""
    backoff = 10
    err = ""
    for _ in range(tries):
        wait = delay - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 404, 410, 451):
                return e.code, b"", dict(e.headers or {})
            if e.code == 429:
                log("  429, sleeping 60s")
                time.sleep(60)
                continue
            err = f"HTTP {e.code}"
        except Exception as e:
            err = str(e)[:150]
        log(f"  retry in {backoff}s: {err}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 600)
    return None, b"", {}


# ------------------------------------------------------------------ text tagging (profile-driven)
class Tagger:
    """Same output shape as scrape_reddit_subaru.tag(): models, years, chassis, engines, dtcs, implied."""
    DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
    YEAR4_RE = re.compile(r"\b(19[89]\d|20[0-2]\d)\b")
    YEAR2_RE = re.compile(r"(?:\b|['’])(\d{2})(?!\d)\s*(?:[a-z]+\s+)?$", re.I)   # "'03 Tahoe", "03 chevy tahoe"

    def __init__(self, p):
        self.models = {m: re.compile(rx, re.I) for m, rx in p["models"].items()}
        self.related = {m: re.compile(rx, re.I) for m, rx in p.get("related_models", {}).items()}
        self.chassis = re.compile(p["chassis"], re.I) if p.get("chassis") else None
        self.engines = re.compile(p["engines"], re.I) if p.get("engines") else None
        self.kw = re.compile(r"\b(?:" + "|".join(re.sub(r" (?!\?)", r"\\s+", k) for k in p["keywords"]) + r")\b", re.I)
        self.y0, self.y1 = (int(x) for x in (p["years"] + "-" + p["years"]).split("-")[:2])

    def relevant(self, text):
        return bool(self.DTC_RE.search(text) or self.kw.search(text))

    def any_model(self, text):
        return any(rx.search(text) for rx in (*self.models.values(), *self.related.values()))

    def in_years(self, text):
        """True unless the text names years and none is in the profile range (+-1 for typos/lists)."""
        ys = {int(y) for y in self.YEAR4_RE.findall(text)}
        return not ys or any(self.y0 - 1 <= y <= self.y1 + 1 for y in ys)

    @staticmethod
    def _y2(two):
        n = int(two)
        return 1900 + n if n >= 80 else (2000 + n if n <= 29 else None)

    def tag(self, title, body, implied_model):
        text = f"{title}\n{body}"
        models, years = [], set()
        for m, rx in (*self.models.items(), *self.related.items()):
            for hit in rx.finditer(text):
                if m not in models:
                    models.append(m)
                window = text[max(0, hit.start() - 30): hit.end() + 30]
                years.update(int(y) for y in self.YEAR4_RE.findall(window))
                y2 = self.YEAR2_RE.search(text[max(0, hit.start() - 14): hit.start()])
                if y2 and self._y2(y2.group(1)):
                    years.add(self._y2(y2.group(1)))
        implied = 0
        if not models and implied_model:
            models, implied = [implied_model], 1
        if not years and models:
            years.update(int(y) for y in self.YEAR4_RE.findall(title))
        years = {y for y in years if 1981 <= y <= 2027}
        letters = [c for c in text if c.isalpha()]
        shouting = letters and sum(c.isupper() for c in letters) / len(letters) > 0.5
        chassis = [] if shouting or not self.chassis else sorted({c.upper().replace(" ", "") for c in self.chassis.findall(text)})
        engines = sorted({e.upper().replace(" ", "") for e in self.engines.findall(text)}) if self.engines else []
        dtcs = sorted({d.upper() for d in self.DTC_RE.findall(text)})
        return models, sorted(years), chassis, engines, dtcs, implied


# ------------------------------------------------------------------ check
def step_check(c):
    ok = True

    def line(good, what, hint=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'PASS' if good else 'FAIL'}  {what}" + (f"  ({hint})" if hint and not good else ""))

    print(f"profile: {c.p['label']}")
    for f in ("build_subaru_diag_db.py", "scrape_reddit_subaru.py", "load_pushshift_subaru.py",
              "kb/build_subaru_kb.py", "cyberdeck_forge/charm_ingest.py"):
        line(os.path.exists(c.path(f)), f)
    for kind in ("tsbs", "cmpl", "rcl"):
        line(glob.glob(c.path(f"raw/nhtsa/{kind}/*.zip")), f"NHTSA {kind} zips", "run build_subaru_diag_db.py download")
    py = c.venv_py if os.path.exists(c.venv_py) else sys.executable
    for mod in ("lmdb", "bs4", "lxml", "zstandard"):
        r = subprocess.run([py, "-c", f"import {mod}"], capture_output=True)
        line(r.returncode == 0, f"{os.path.basename(os.path.dirname(os.path.dirname(py)))} python has {mod}",
             f"{py} -m pip install {mod if mod != 'bs4' else 'beautifulsoup4'}")
    line(os.path.exists(os.path.expanduser("~/mnt/charm/data.mdb")), "CHARM mounted at ~/mnt/charm",
         "~/miniconda3/envs/charm_fuse/bin/squashfuse -o ro <lmdb-pages.sqsh> ~/mnt/charm")
    line(shutil.which("aria2c"), "aria2c", "sudo pacman -S aria2")
    line(glob.glob(c.path("raw/reddit/pushshift/*.torrent")), "Pushshift subreddits torrent file")
    free = shutil.disk_usage(c.root).free / 1e9
    line(free > 60, f"{free:.0f} GB free on the subaru_cyberdeck disk", "want 60+ GB for dumps")
    print("check:", "ALL PASS" if ok else "some FAIL (steps that need them will say so)")


# ------------------------------------------------------------------ NHTSA (reuses build_subaru_diag_db.py)
def step_nhtsa(c):
    import build_subaru_diag_db as bd
    make = c.p["nhtsa"]["make"].upper()
    models = {k.upper(): v for k, v in c.p["nhtsa"]["models"].items()}

    def model_of(mk, md, yr):
        if mk.strip().upper() != make or yr is None or not (c.y0 <= yr <= c.y1):
            return None
        md = md.strip().upper()
        for k, v in models.items():
            if md == k or md.startswith(k + " "):
                return v
        return None

    conn = bd.connect(c.diag_db)
    for t in ("tsb", "complaint", "recall"):
        conn.execute(f"DELETE FROM {t}")
    raw = c.path("raw")
    src = bd.upsert_source(conn, "NHTSA TSB flat file", bd.NHTSA_URLS["tsbs"][0], "US public domain")
    rows = []
    for r in bd.iter_flat_rows(os.path.join(raw, "nhtsa", "tsbs")):
        if len(r) >= 14 and (m := model_of(r[7], r[8], bd._int(r[9]))):
            rows.append((bd._int(r[0]), r[3].strip(), bd._date(r[4]), r[6].strip(), m, bd._int(r[9]),
                         r[10].strip(), r[11].strip(), r[12].strip(), r[13].strip(), src))
    conn.executemany("INSERT OR REPLACE INTO tsb VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    n_tsb = len(rows)
    src = bd.upsert_source(conn, "NHTSA complaints flat file", bd.NHTSA_URLS["cmpl"][0], "US public domain")
    rows = []
    for r in bd.iter_flat_rows(os.path.join(raw, "nhtsa", "cmpl")):
        if len(r) >= 20 and (m := model_of(r[3], r[4], bd._int(r[5]))):
            rows.append((bd._int(r[0]), bd._int(r[1]), m, bd._int(r[5]), r[11].strip(), bd._date(r[7]),
                         bd._int(r[17]), r[19].strip(), src))
    conn.executemany("INSERT OR REPLACE INTO complaint VALUES(?,?,?,?,?,?,?,?,?)", rows)
    n_cmpl = len(rows)
    src = bd.upsert_source(conn, "NHTSA recalls flat file", bd.NHTSA_URLS["rcl"][0], "US public domain")
    rows = []
    for r in bd.iter_flat_rows(os.path.join(raw, "nhtsa", "rcl")):
        if len(r) >= 22 and (m := model_of(r[2], r[3], bd._int(r[4]))):
            defect = r[19].strip() or (r[22].strip() if len(r) > 22 else "")
            rows.append((r[1].strip(), m, bd._int(r[4]), r[6].strip(), defect, r[20].strip(), r[21].strip(), src))
    conn.executemany("INSERT OR REPLACE INTO recall VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT OR REPLACE INTO source(name,url,license,version,retrieved_at) VALUES "
                 "('profile',?,?,?,?)", (c.p["label"], "", json.dumps(c.p["nhtsa"]), bd.now()))
    conn.commit()
    log(f"nhtsa: {n_tsb} TSB, {n_cmpl} complaint, {len(rows)} recall rows -> {c.diag_db}")


# ------------------------------------------------------------------ VIN (NHTSA vPIC API)
VPIC = "https://vpic.nhtsa.dot.gov/api/vehicles"


def _make_name(s):
    s = (s or "").strip()
    return s if len(s) <= 3 else s.title().replace("Gmc", "GMC").replace("Bmw", "BMW")


def step_vin(c):
    db = c.path("kb/vin.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wmi (wmi TEXT PRIMARY KEY, make TEXT);
        CREATE TABLE IF NOT EXISTS wmi_info (wmi TEXT PRIMARY KEY, makes TEXT, manufacturer TEXT,
            vehicle_type TEXT, fetched_at TEXT);
        CREATE TABLE IF NOT EXISTS vin_decoded (vin TEXT PRIMARY KEY, make TEXT, model TEXT, year INT,
            engine TEXT, json TEXT NOT NULL, fetched_at TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);""")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('source', 'NHTSA vPIC API, US public domain')")
    n_new = 0
    for mfr in c.p["vin"]["manufacturers"]:
        st, body, _ = http_get(f"{VPIC}/GetWMIsForManufacturer/{urllib.parse.quote(mfr)}?format=json")
        if st != 200:
            log(f"vin: vPIC GetWMIsForManufacturer({mfr}) failed: {st}")
            continue
        wmis = sorted({r["WMI"] for r in json.loads(body)["Results"] if r.get("WMI") and len(r["WMI"]) == 3})
        log(f"vin: {mfr}: {len(wmis)} WMIs")
        for w in wmis:
            if conn.execute("SELECT 1 FROM wmi_info WHERE wmi=?", (w,)).fetchone():
                continue
            st, body, _ = http_get(f"{VPIC}/DecodeWMI/{w}?format=json")
            if st != 200:
                continue
            res = (json.loads(body).get("Results") or [{}])[0]
            makes = [_make_name(m) for m in re.split(r"\s*,\s*", res.get("Make") or res.get("MakeName") or "") if m]
            conn.execute("INSERT OR REPLACE INTO wmi_info VALUES (?,?,?,?,datetime('now'))",
                         (w, ",".join(makes), res.get("ManufacturerName"), res.get("VehicleType")))
            if len(makes) == 1:   # a WMI shared by two makes is left to the built-in table / manual pick
                conn.execute("INSERT OR IGNORE INTO wmi VALUES (?,?)", (w, makes[0]))
                n_new += 1
            conn.commit()
    for vin in c.p["vin"].get("vins", []) + (c.a.vin or []):
        vin = vin.strip().upper()
        st, body, _ = http_get(f"{VPIC}/DecodeVinValuesExtended/{vin}?format=json")
        if st != 200:
            log(f"vin: decode {vin} failed: {st}")
            continue
        r = json.loads(body)["Results"][0]
        eng = " ".join(x for x in (r.get("DisplacementL") and f"{float(r['DisplacementL']):.1f}L",
                                   r.get("EngineConfiguration"), r.get("EngineModel")) if x)
        conn.execute("INSERT OR REPLACE INTO vin_decoded VALUES (?,?,?,?,?,?,datetime('now'))",
                     (vin, _make_name(r.get("Make")), r.get("Model"), int(r["ModelYear"]) if r.get("ModelYear") else None,
                      eng or None, json.dumps({k: v for k, v in r.items() if v}, sort_keys=True)))
        log(f"vin: {vin} = {r.get('ModelYear')} {r.get('Make')} {r.get('Model')} {eng} ({r.get('ErrorText', '')[:60]})")
    conn.commit()
    total = conn.execute("SELECT count(*) FROM wmi").fetchone()[0]
    log(f"vin: {n_new} new WMIs, {total} in kb/vin.db")


# ------------------------------------------------------------------ CHARM
def charm_env():
    import charm_ingest as ci
    from pathlib import Path
    return ci, ci.open_charm(Path(os.path.expanduser("~/mnt/charm")))


def charm_models(c):
    """{make: {year: [model_full, ...]}} for the profile's candidate make names and years (prefix seeks only)."""
    ci, env = charm_env()
    only = (c.p["charm"].get("only") or "").lower()
    out = {}
    with env.begin() as txn:
        cur = txn.cursor()
        for make in c.p["charm"]["make_candidates"]:
            for year in range(c.y0, c.y1 + 1):
                pre = ci.enc_prefix(f"{make}/{year}")
                found = []
                key = pre
                while cur.set_range(key) and cur.key().startswith(pre):
                    seg = cur.key()[len(pre):].split(b"/")[0]
                    if not seg:
                        key = cur.key() + b"\x00"
                        continue
                    name = urllib.parse.unquote(seg.decode("utf-8", "replace"))
                    if not only or only in f"{year} {name}".lower():
                        found.append(name)
                    key = pre + seg + b"0"          # '0' (0x30) sorts right after '/' (0x2F): next model
                if found:
                    out.setdefault(make, {})[year] = found
    return out


def step_charm_list(c):
    if not os.path.exists(os.path.expanduser("~/mnt/charm/data.mdb")):
        sys.exit("charm-list: CHARM is not mounted at ~/mnt/charm (see `check`)")
    found = charm_models(c)
    if not found:
        print(f"charm-list: no CHARM vehicles for {c.p['charm']['make_candidates']} {c.p['years']} "
              f"matching '{c.p['charm'].get('only')}'")
    for make, years in found.items():
        for y, names in years.items():
            print(f"  {make} {y}: " + "; ".join(names))
    return found


def run(cmd, logname=None):
    log("$ " + " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode:
        sys.exit(f"failed ({r.returncode}): {' '.join(cmd)}")


def step_kb(c):
    if not os.path.exists(c.diag_db):
        step_nhtsa(c)
    found = charm_models(c) if os.path.exists(os.path.expanduser("~/mnt/charm/data.mdb")) else {}
    if os.path.exists(c.kb_db):          # fresh build each time (CHARM re-runs don't drop stale vehicle rows)
        bak = c.kb_db.replace(".db", f".pre_{time.strftime('%Y%m%d_%H%M')}.db")
        shutil.move(c.kb_db, bak)
        log(f"kb: previous DB moved to {bak}")
    py = c.venv_py if os.path.exists(c.venv_py) else sys.executable
    if found:
        run([py, c.builder, "--db", c.kb_db, "charm", "--charm", "~/mnt/charm", "--makes", ",".join(found),
             "--years", c.p["years"], "--only", c.p["charm"].get("only") or ""])
    else:
        log("kb: CHARM not mounted or no matching vehicles; skipping CHARM (re-run kb once it's mounted)")
    run([sys.executable, c.builder, "--db", c.kb_db, "diag", c.diag_db])
    dtc_names(c)
    conn = sqlite3.connect(c.kb_db)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('make', ?)", (c.p["make"],))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('profile', ?)", (json.dumps(c.p, sort_keys=True),))
    conn.commit()
    conn.close()
    run([sys.executable, c.builder, "--db", c.kb_db, "finish"])


DTC_HEAD_RE = re.compile(r"^\s*(?:DTC\s+)?([PBCU][0-9][0-9A-F]{3})\b[\s:\-–]*(.*)$")


def dtc_names(c):
    """Fill dtc.description (left NULL by `diag`, which only knows Subaru's dtc_subaru table) from the CHARM
    DTC page itself: the title after the code, else the first line of the page that repeats the code."""
    import build_subaru_diag_db as bd
    conn = sqlite3.connect(c.kb_db)
    n = 0
    for code, title, body in conn.execute(
            "SELECT p.code, s.title, s.body FROM dtc_procedure p JOIN section s ON s.id=p.section_id "
            "JOIN dtc d ON d.code=p.code WHERE d.description IS NULL ORDER BY p.code, s.id").fetchall():
        desc = None
        m = DTC_HEAD_RE.match(title or "")
        if m and len(m.group(2)) > 5:
            desc = m.group(2)
        else:
            for ln in (body or "").split("\n")[1:8]:
                m = DTC_HEAD_RE.match(ln)
                if m and m.group(1) == code and len(m.group(2)) > 5:
                    desc = m.group(2)
                    break
        if desc:
            n += conn.execute("UPDATE dtc SET description=?, module=coalesce(module, ?) WHERE code=? AND description IS NULL",
                              (desc[:200], bd.module_for(code), code)).rowcount
    # Codes the CHARM page doesn't name: PCMHammer's GM code list (GPL-3), only for codes already in this DB.
    n_ph, lst = 0, c.path("raw/pcmhammer/Apps/UI/WindowsForms/PcmLogger/DiagnosticCodes.txt")
    if os.path.exists(lst):
        for ln in open(lst, encoding="utf-8", errors="replace"):
            code, _, desc = ln.strip().partition(",")
            if re.fullmatch(r"[PBCU][0-9A-F]{4}", code) and desc:
                n_ph += conn.execute("UPDATE dtc SET description=?, module=coalesce(module, ?) WHERE code=? AND "
                                     "description IS NULL", (desc.strip()[:200], bd.module_for(code), code)).rowcount
    conn.commit()
    left = conn.execute("SELECT count(*) FROM dtc WHERE description IS NULL").fetchone()[0]
    log(f"dtc-names: {n} DTC descriptions from CHARM pages, {n_ph} from PCMHammer's code list, {left} still without one")


def step_embed(c):
    py = c.venv_py if os.path.exists(c.venv_py) else sys.executable
    run([py, c.builder, "--db", c.kb_db, "embed", "--missing"])
    run([sys.executable, c.builder, "--db", c.kb_db, "finish"])


# ------------------------------------------------------------------ Reddit
def _subs_in_torrent(c):
    tor = sorted(glob.glob(c.path("raw/reddit/pushshift/*.torrent")))
    if not tor:
        sys.exit("reddit-get: no .torrent in raw/reddit/pushshift (the Subaru pull's subreddits24 torrent)")
    out = subprocess.run(["aria2c", "-S", tor[0]], capture_output=True, text=True).stdout.splitlines()
    files, last = {}, None
    for ln in out:
        m = re.match(r"^\s*(\d+)\|(.+)$", ln)
        if m:
            last = (int(m.group(1)), m.group(2).strip())
            continue
        m = re.match(r"^\s*\|.*\(([\d,]+)\)", ln)
        if m and last:
            files[os.path.basename(last[1]).lower()] = (last[0], int(m.group(1).replace(",", "")), last[1])
            last = None
    return tor[0], files


def step_reddit_get(c):
    tor, files = _subs_in_torrent(c)
    r = c.p["reddit"]
    pick, total, missing = [], 0, []
    for sub in r["subs"]:
        for kind in ("submissions", "comments"):
            f = files.get(f"{sub.lower()}_{kind}.zst")
            if not f:
                missing.append(f"{sub}_{kind}")
                continue
            if f[1] > r.get("max_dump_gb", 25) * 1e9:
                log(f"reddit-get: skip {f[2]} ({f[1] / 1e9:.1f} GB > max_dump_gb)")
                continue
            pick.append(f)
            total += f[1]
    for idx, size, path in pick:
        log(f"  {path}  {size / 1e9:.2f} GB")
    if missing:
        log("reddit-get: not in the dump (sub doesn't exist or is tiny): " + ", ".join(missing))
    log(f"reddit-get: {len(pick)} files, {total / 1e9:.1f} GB")
    if not pick:
        return
    # Same torrent and folder as the Subaru pull; files that aren't selected are left alone.
    sel = sorted({f[0] for f in pick})
    run(["aria2c", "--dir", c.path("raw/reddit/pushshift"), "--select-file=" + ",".join(map(str, sel)),
         "--seed-time=0", "--file-allocation=none", "--check-integrity=false", "--bt-remove-unselected-file=false",
         "--summary-interval=60", "--console-log-level=warn", tor])


def step_reddit(c):
    import scrape_reddit_subaru as s
    import load_pushshift_subaru as lp
    r = c.p["reddit"]
    tagger = Tagger(c.p)
    need_model = set(r.get("require_model", []))
    cur = {"sub": None}

    def relevant(text):
        if not tagger.relevant(text):
            return False
        if cur["sub"] in need_model and not (tagger.any_model(text) and tagger.in_years(text)):
            return False
        return True

    s.relevant, s.tag, s.SUBREDDITS = relevant, tagger.tag, dict(r["subs"])
    lp.IMPLIED = dict(r["subs"])
    db = c.path(r["db"])
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    conn.executescript(s.SCHEMA)
    conn.execute("CREATE TABLE IF NOT EXISTS dump_state (subreddit TEXT PRIMARY KEY, posts_read INT, posts_kept INT, "
                 "comments_read INT, comments_kept INT, done INT)")
    for sub in r["subs"]:
        hits = [p for p in glob.glob(os.path.join(lp.DUMP_DIR, "*_submissions.zst"))
                if os.path.basename(p).lower() == f"{sub.lower()}_submissions.zst" and os.path.getsize(p)]
        if not hits:
            log(f"reddit: [{sub}] no dump file (run reddit-get, or it isn't in the dump)")
            continue
        name = os.path.basename(hits[0])[:-len("_submissions.zst")]
        cur["sub"] = sub
        lp.IMPLIED[name] = r["subs"][sub]
        lp.load_sub(conn, name, log)
    if c.a.topup:
        api = s.Api(1.0, log)
        s.REDDIT_START = 1735689600          # 2025-01-01: the dumps end 2024-12
        for sub in r.get("topup_subs", []):
            cur["sub"] = sub
            s.crawl_posts(conn, api, sub, log)
            s.crawl_comments(conn, api, sub, log)
    print(s.stats(conn))
    log(f"reddit: done, {os.path.getsize(db) / 1e6:.1f} MB -> {db}")


# ------------------------------------------------------------------ Wayback forums
FORUM_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS capture (thread_id INT, page INT, url TEXT, wayback_ts TEXT, length INT, slug TEXT,
    PRIMARY KEY (thread_id, page, wayback_ts));
CREATE TABLE IF NOT EXISTS page_fetch (thread_id INT, page INT, url TEXT, wayback_ts TEXT, status TEXT,
    n_posts INT, fetched_at TEXT, PRIMARY KEY (thread_id, page));
CREATE TABLE IF NOT EXISTS thread (id INT PRIMARY KEY, url TEXT, title TEXT, forum TEXT, started TEXT,
    total_pages INT, models TEXT, years TEXT, chassis TEXT, engines TEXT, dtcs TEXT, relevant INT);
CREATE TABLE IF NOT EXISTS post (id INTEGER PRIMARY KEY, thread_id INT, page INT, position INT, posted_at TEXT,
    body TEXT, is_op INT, wayback_ts TEXT, UNIQUE (thread_id, page, position));
CREATE INDEX IF NOT EXISTS ix_post_thread ON post(thread_id, position);
CREATE TABLE IF NOT EXISTS thread_dtc (thread_id INT, dtc TEXT, PRIMARY KEY (thread_id, dtc));
CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
"""
THREAD_URL_RE = {
    "xenforo": re.compile(r"/threads/(?:([^/?#]*)\.)?(\d+)/?(?:page-(\d+))?/?$", re.I),
    "invision": re.compile(r"/topic/(\d+)-([^/?#]*)/?(?:page[/-](\d+))?/?$", re.I),
    "vbulletin": re.compile(r"(?:showthread\.php\?t=(\d+)(?:&page=(\d+))?|/threads/(\d+)-([^/?#]*?)(?:/page(\d+))?(?:\.html)?/?$)", re.I),
}


def robots_gate(domain):
    """('ok'|'blocked'|'unknown', note). Blocked = robots.txt disallows AI crawlers (or everyone) from the site root."""
    st, body, hdr = http_get(f"https://{domain}/robots.txt", timeout=30, tries=2)
    cf = any(k.lower() in ("cf-ray", "cf-mitigated") for k in hdr) or "cloudflare" in str(hdr.get("Server", "")).lower()
    if st != 200:
        return "unknown", f"robots.txt HTTP {st}" + (", Cloudflare" if cf else "")
    groups, agents, rules = [], [], []
    for raw in body.decode("utf-8", "replace").splitlines():
        ln = raw.split("#", 1)[0].strip()
        if not ln or ":" not in ln:
            continue
        k, v = (x.strip() for x in ln.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
        elif k in ("disallow", "allow"):
            rules.append((k, v))
    if agents:
        groups.append((agents, rules))
    for ag, rl in groups:
        root_block = ("disallow", "/") in rl and ("allow", "/") not in rl
        if root_block and ("*" in ag or any(b in a for a in ag for b in AI_BOTS)):
            hit = [a for a in ag if a == "*" or any(b in a for b in AI_BOTS)]
            return "blocked", "robots.txt disallows " + ", ".join(hit[:4]) + (" (Cloudflare)" if cf else "")
    return "ok", "robots.txt allows" + (" (Cloudflare-fronted; archived copies only)" if cf else "")


def parse_thread_url(engine, url):
    m = THREAD_URL_RE[engine].search(url.split("#")[0])
    if not m:
        return None
    g = m.groups()
    if engine == "xenforo":
        return int(g[1]), int(g[2] or 1), (g[0] or "")
    if engine == "invision":
        return int(g[0]), int(g[2] or 1), g[1]
    if g[0]:
        return int(g[0]), int(g[1] or 1), ""
    return int(g[2]), int(g[4] or 1), g[3] or ""


def parse_forum_page(engine, raw):
    """-> (title, [(posted_at, author, text)]). Quotes and signatures are dropped."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "lxml")
    for t in soup.select("script, style, blockquote, .bbCodeBlock--quote, .ipsQuote, .signature, .message-signature, "
                         "[data-role='memberSignature'], div[id^='wb-'], #wm-ipp-base"):
        t.decompose()
    title = None
    if engine == "xenforo":
        h = soup.select_one("h1.p-title-value")
        posts = [(a.select_one("time[datetime]"), a.get("data-author"), a.select_one(".bbWrapper"))
                 for a in soup.select("article.message")]
    elif engine == "invision":
        h = soup.select_one("h1.ipsType_pageTitle, h1")
        posts = [(a.select_one("time[datetime]"), (a.select_one("[data-role='commentAuthor'], .cAuthorPane_author") or {}).get_text(" ", strip=True)
                  if a.select_one("[data-role='commentAuthor'], .cAuthorPane_author") else None,
                  a.select_one("[data-role='commentContent']")) for a in soup.select("article[id^='elComment_']")]
    else:
        h = None
        posts = [(None, None, d) for d in soup.select("div[id^='post_message_']")]
    title = (h.get_text(" ", strip=True) if h else (soup.title.get_text(" ", strip=True) if soup.title else "")) or ""
    out = []
    for tm, author, body in posts:
        if body is None:
            continue
        text = re.sub(r"\n{3,}", "\n\n", body.get_text("\n", strip=True))
        if text:
            out.append(((tm.get("datetime") if tm else None), author, text))
    return title, out


def forum_index(c, f, conn):
    rx_engine = f["engine"]
    resume = (conn.execute("SELECT v FROM state WHERE k='cdx_resume'").fetchone() or [None])[0]
    if resume == "DONE":
        return
    n = conn.execute("SELECT count(*) FROM capture").fetchone()[0]
    while True:
        q = {"url": f["cdx_prefix"], "matchType": "prefix", "filter": ["statuscode:200", "mimetype:text/html"],
             "fl": "original,timestamp,length", "collapse": "urlkey", "limit": "20000", "showResumeKey": "true"}
        if resume:
            q["resumeKey"] = resume
        st, body, _ = http_get("https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(q, doseq=True), timeout=300, delay=3.0)
        if st != 200:
            log(f"[{f['name']}] CDX failed ({st}); re-run to resume")
            return
        lines = body.decode("utf-8", "replace").split("\n")
        resume = None
        rows = []
        for i, ln in enumerate(lines):
            if not ln.strip():
                if i + 1 < len(lines) and lines[i + 1].strip():
                    resume = lines[i + 1].strip()
                break
            parts = ln.split(" ")
            if len(parts) < 3:
                continue
            t = parse_thread_url(rx_engine, parts[0])
            if t:
                rows.append((t[0], t[1], parts[0], parts[1], int(parts[2]) if parts[2].isdigit() else None, t[2]))
        conn.executemany("INSERT OR IGNORE INTO capture VALUES (?,?,?,?,?,?)", rows)
        n += len(rows)
        conn.execute("INSERT OR REPLACE INTO state VALUES ('cdx_resume', ?)", (resume or "DONE",))
        conn.commit()
        log(f"[{f['name']}] CDX: {n} thread-page captures")
        if not resume:
            return


def forum_fetch(c, f, conn, tagger):
    import scrape_reddit_subaru as s          # op_hash(): keyed hash, same salt as the Reddit DBs
    engine = f["engine"]
    # Pre-filter by URL slug (cheap): repair-relevant, and for mixed-make forums a target/related model.
    cand = conn.execute("SELECT thread_id, page, url, wayback_ts, slug FROM capture c WHERE NOT EXISTS "
                        "(SELECT 1 FROM page_fetch p WHERE p.thread_id=c.thread_id AND p.page=c.page) "
                        "ORDER BY thread_id, page, length DESC").fetchall()
    best = {}
    for tid, page, url, ts, slug in cand:
        best.setdefault((tid, page), (url, ts, slug))
    keep_threads = set()
    for (tid, page), (url, ts, slug) in best.items():
        words = (slug or "").replace("-", " ")
        if page == 1 and words and tagger.relevant(words) and (not f.get("require_model") or tagger.any_model(words)):
            keep_threads.add(tid)
    known = {r[0] for r in conn.execute("SELECT id FROM thread WHERE relevant=1")}
    todo = sorted(k for k in best if k[0] in keep_threads or k[0] in known)
    log(f"[{f['name']}] {len(todo)} pages to fetch from {len(keep_threads | known)} threads "
        f"(of {len({k[0] for k in best})} unfetched threads)")
    dropped, fails = set(), 0
    for i, (tid, page) in enumerate(todo, 1):
        if tid in dropped:
            continue
        url, ts, slug = best[(tid, page)]
        # archive.org refuses connections for a while when pulled too fast: 3 s apart (as for Ultimate Subaru),
        # few quick retries, and stop this forum after 5 refusals in a row instead of retrying forever.
        st, body, _ = http_get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=120, tries=3, delay=3.0)
        if st is None:
            fails += 1
            if fails >= 5:
                log(f"[{f['name']}] archive.org keeps refusing; stopped at {i}/{len(todo)}. Re-run `forums` later, it resumes.")
                break
            time.sleep(300)
            continue
        fails = 0
        status = "ok"
        n_posts = 0
        if st == 200 and body:
            title, posts = parse_forum_page(engine, body.decode("utf-8", "replace"))
            if page == 1:
                text = title + "\n" + "\n".join(p[2] for p in posts[:3])
                models, years, chassis, engines, dtcs, _ = tagger.tag(title, "\n".join(p[2] for p in posts[:3]), None)
                rel = int(tagger.relevant(text) and (not f.get("require_model") or bool(models))
                          and tagger.in_years(title + " " + " ".join(map(str, years))))
                conn.execute("INSERT OR REPLACE INTO thread VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (tid, url, title, f["name"], posts[0][0] if posts else None, None, ",".join(models),
                              ",".join(map(str, years)), ",".join(chassis), ",".join(engines), ",".join(dtcs), rel))
                conn.executemany("INSERT OR IGNORE INTO thread_dtc VALUES (?,?)", [(tid, d) for d in dtcs])
                if not rel:
                    dropped.add(tid)              # page 1 says off-topic: don't fetch the rest
            op = conn.execute("SELECT v FROM state WHERE k=?", (f"op:{tid}",)).fetchone()
            op_author = op[0] if op else None
            for pos, (at, author, text) in enumerate(posts):
                ah = s.op_hash(author)
                if page == 1 and pos == 0 and ah:
                    op_author = ah
                    conn.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (f"op:{tid}", ah))   # keyed hash, never a name
                conn.execute("INSERT OR REPLACE INTO post(thread_id,page,position,posted_at,body,is_op,wayback_ts) "
                             "VALUES (?,?,?,?,?,?,?)", (tid, page, pos, at, text, int(ah is not None and ah == op_author), ts))
            n_posts = len(posts)
            status = "ok" if posts else "no_posts"
        else:
            status = f"http_{st}"
        conn.execute("INSERT OR REPLACE INTO page_fetch VALUES (?,?,?,?,?,?,datetime('now'))",
                     (tid, page, url, ts, status, n_posts))
        conn.commit()
        if i % 50 == 0:
            n_thr = conn.execute("SELECT count(*) FROM thread WHERE relevant=1").fetchone()[0]
            n_p = conn.execute("SELECT count(*) FROM post").fetchone()[0]
            log(f"[{f['name']}] {i}/{len(todo)} pages, {n_thr} relevant threads, {n_p} posts")
    conn.commit()


def step_forums(c):
    tagger = Tagger(c.p)
    only = set(c.a.only.split(",")) if c.a.only else None
    for f in c.p["forums"]:
        if only and f["name"] not in only:
            continue
        verdict, note = robots_gate(f["domain"])
        log(f"[{f['name']}] {verdict}: {note}")
        if verdict == "blocked" or (verdict == "unknown" and not c.a.allow_unknown):
            log(f"[{f['name']}] SKIPPED (project rule: no sites whose robots.txt blocks AI"
                + ("; robots.txt unreadable, re-run with --allow-unknown after checking it in a browser)" if verdict == "unknown" else ")"))
            continue
        db = c.path(f"raw/forums/{f['name']}.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript(FORUM_SCHEMA)
        conn.execute("INSERT OR REPLACE INTO state VALUES ('robots', ?)", (f"{verdict}: {note} @ {time.strftime('%F')}",))
        forum_index(c, f, conn)
        forum_fetch(c, f, conn, tagger)
        n_thr = conn.execute("SELECT count(*) FROM thread WHERE relevant=1").fetchone()[0]
        n_p = conn.execute("SELECT count(*) FROM post p JOIN thread t ON t.id=p.thread_id WHERE t.relevant=1").fetchone()[0]
        log(f"[{f['name']}] DONE: {n_thr} relevant threads, {n_p} posts -> {db}")


# ------------------------------------------------------------------ GM Mode 22 PID sheet (.xlsx, stdlib only)
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}


def xlsx_sheets(path):
    """Yield (sheet_name, rows) with rows as lists of cell strings. No openpyxl needed."""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    for sh in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS):
        target = rels[sh.get(f"{{{NS['r']}}}id")].lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(target)).iter(f"{{{NS['m']}}}row"):
            cells = {}
            for cel in row.findall("m:c", NS):
                ref = re.match(r"([A-Z]+)", cel.get("r")).group(1)
                col = 0
                for ch in ref:
                    col = col * 26 + ord(ch) - 64
                v = cel.find("m:v", NS)
                if cel.get("t") == "s" and v is not None:
                    val = shared[int(v.text)]
                elif cel.get("t") == "inlineStr":
                    val = "".join(t.text or "" for t in cel.iter(f"{{{NS['m']}}}t"))
                else:
                    val = v.text if v is not None else ""
                cells[col - 1] = (val or "").strip()
            if cells:
                rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
        yield sh.get("name"), rows


COLS = {"name": r"^(long ?)?name$|description", "short": r"short", "pid": r"mode.?and.?pid|^pid$|^pid\b|mode ?22",
        "equation": r"equation|formula", "min": r"^min", "max": r"^max", "units": r"^units?$", "header": r"header"}


PCMHAMMER_XML = "raw/pcmhammer/Apps/UI/WindowsForms/PcmLogger/Parameters.Standard.xml"
PCMHAMMER_BAD = "raw/pcmhammer/Apps/UI/WindowsForms/PcmLogger/Parameters.DidNotWork.xml"


def pcmhammer_pids(path, bad_path, header):
    """PCMHammer PcmLogger Parameters.Standard.xml (GPL-3) -> entries. x = the raw uint8/uint16 (big-endian);
    bit-mapped parameters read one bit, first label = bit set (ParameterDatabase.CreateBooleanConversion)."""
    bad = set()
    if os.path.exists(bad_path):
        bad = {e.get("pid") or e.get("id") for e in ET.parse(bad_path).getroot().iter("Parameter")}
    out = []
    for e in ET.parse(path).getroot().iter("Parameter"):
        pid = (e.get("pid") or "").upper()
        if not re.fullmatch(r"[0-9A-F]{4}", pid) or pid in bad:
            continue
        st = e.get("storageType", "uint8")
        raw = {"uint8": "A", "int8": "SIGNED(A)", "uint16": "(A*256+B)", "int16": "SIGNED16(A*256+B)"}.get(st)
        conv = e.find("Conversion")
        if raw is None or conv is None:
            continue
        expr = conv.get("expression", "x")
        ent = {"pid": "22" + pid, "key": e.get("id"), "name": e.get("name") or e.get("id"), "units": conv.get("units", ""),
               "header": header, "source": f"PCMHammer {os.path.basename(path)} id={e.get('id')}", "verified": False}
        if e.get("bitMapped", "False").lower() == "true":
            labels = expr.split(",")
            if len(labels) != 2:
                continue
            ent.update(equation=f"(({raw}>>{int(e.get('bitIndex', 0))})&1)", labels=labels, units="")
        else:
            if not re.fullmatch(r"[\sx0-9.+\-*/%()]+", expr):
                continue                      # only plain arithmetic on x
            ent["equation"] = re.sub(r"\bx\b", raw, expr)
        out.append(ent)
    return out


def step_gm_pids(c):
    """Mode $22 PIDs from PCMHammer (raw/pcmhammer, preferred) plus any community .xlsx given on the command line."""
    out = c.path(c.p["gm_live"]["pid_file"])
    hdr_default = c.p["gm_live"]["pcm_header"]
    pids, n_rows, sources = {}, 0, []

    def add(e):
        k = e.get("key") or f"{e['pid']}#{len(pids)}"
        eq = re.sub(r"\s+", "", e["equation"])
        origin = e["source"].split(" ")[0]
        others = [x for x in pids.values() if x["pid"] == e["pid"] and x["source"].split(" ")[0] != origin]
        same = [x for x in others if re.sub(r"\s+", "", x["equation"]) == eq]
        if same:                              # another source agrees on this reading: note it, keep the first
            same[0].setdefault("also_in", []).append(e["source"])
            return
        clash = [x for x in others if "labels" not in x and "labels" not in e]
        if clash:                             # another source reads this PID differently: keep the first, flag it
            clash[0].setdefault("conflicts", []).append({x: e[x] for x in ("name", "equation", "source")})
            return
        # Same source, same PID, different conversion = PCMHammer's alternates (e.g. sensor variants): keep each.
        pids.setdefault(k, e)

    ph = c.path(PCMHAMMER_XML)
    if os.path.exists(ph):
        rows = pcmhammer_pids(ph, c.path(PCMHAMMER_BAD), hdr_default)
        for e in rows:
            add(e)
        n_rows += len(rows)
        sources.append("PCMHammer PcmLogger Parameters.Standard.xml (GPL-3, github.com/PcmHammer/PcmHammer)")
    for src in [os.path.expanduser(x) for x in (c.a.xlsx or "").split(",") if x]:
        sources.append(os.path.basename(src))
        for sheet, rows in xlsx_sheets(src):
            hdr_i = next((i for i, r in enumerate(rows[:30]) if any(re.search(COLS["pid"], x, re.I) for x in r)
                          and any(re.search(COLS["equation"], x, re.I) for x in r)), None)
            if hdr_i is None:
                log(f"gm-pids: sheet '{sheet}': no PID/Equation header row, skipped")
                continue
            hdr = rows[hdr_i]
            col = {k: next((j for j, h in enumerate(hdr) if re.search(rx, h, re.I)), None) for k, rx in COLS.items()}
            for i, r in enumerate(rows[hdr_i + 1:], hdr_i + 2):
                get = lambda k: (r[col[k]] if col[k] is not None and col[k] < len(r) else "").strip()
                pid = re.sub(r"[^0-9A-F]", "", re.sub(r"0X|\$", "", get("pid").upper()))
                if not pid.startswith("22") or len(pid) != 6 or not get("equation"):
                    continue
                n_rows += 1
                add({"pid": pid, "name": get("name") or get("short"), "short": get("short"), "equation": get("equation"),
                     "min": get("min"), "max": get("max"), "units": get("units"),
                     "header": re.sub(r"[^0-9A-Fa-f]", "", get("header")).upper() or hdr_default,
                     "source": f"{os.path.basename(src)} sheet '{sheet}' row {i}", "verified": False})
    if not sources:
        sys.exit(f"gm-pids: no source. Clone PCMHammer to {c.path('raw/pcmhammer')} and/or pass a PID .xlsx")
    doc = {"_about": "GM Mode $22 PIDs. Equations use A,B,.. = reply data bytes. PCMHammer entries come from its "
                     "PcmLogger (P01/P59 era PCMs); xlsx entries are community findings. All 'verified': false until "
                     "checked on the car; 'conflicts' = sources disagree. 'labels' = on/off bit (first = bit set).",
           "sources": sources, "built": time.strftime("%F"),
           "pids": sorted(pids.values(), key=lambda e: (e["header"], e["pid"], e.get("key") or ""))}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(doc, f, indent=1)
    log(f"gm-pids: {len(pids)} PIDs ({n_rows} rows from {len(sources)} sources, "
        f"{sum('conflicts' in e for e in pids.values())} with conflicts) -> {out}")


# ------------------------------------------------------------------ status
def step_status(c):
    def q(db, sql):
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as cn:
                return cn.execute(sql).fetchone()[0]
        except Exception:
            return "-"
    print(f"{c.p['label']}")
    print(f"  NHTSA   {c.diag_db}: TSB {q(c.diag_db, 'SELECT count(*) FROM tsb')}, complaints "
          f"{q(c.diag_db, 'SELECT count(*) FROM complaint')}, recalls {q(c.diag_db, 'SELECT count(*) FROM recall')}")
    vin = c.path("kb/vin.db")
    print(f"  VIN     kb/vin.db: {q(vin, 'SELECT count(*) FROM wmi')} WMIs, {q(vin, 'SELECT count(*) FROM vin_decoded')} decoded VINs")
    print(f"  KB      {c.kb_db}: sections {q(c.kb_db, 'SELECT count(*) FROM section')}, vehicles "
          f"{q(c.kb_db, 'SELECT count(*) FROM vehicle')}, specs {q(c.kb_db, 'SELECT count(*) FROM spec')}, labor "
          f"{q(c.kb_db, 'SELECT count(*) FROM labor')}, DTCs {q(c.kb_db, 'SELECT count(*) FROM dtc')} "
          f"(named {q(c.kb_db, 'SELECT count(*) FROM dtc WHERE description IS NOT NULL')}), knowledge "
          f"{q(c.kb_db, 'SELECT count(*) FROM knowledge')}, vectors {q(c.kb_db, 'SELECT count(*) FROM section_vec')}")
    rd = c.path(c.p["reddit"]["db"])
    print(f"  Reddit  {c.p['reddit']['db']}: posts {q(rd, 'SELECT count(*) FROM reddit_post')}, comments "
          f"{q(rd, 'SELECT count(*) FROM reddit_comment')}")
    for f in c.p["forums"]:
        db = c.path(f"raw/forums/{f['name']}.db")
        print(f"  Forum   {f['name']}: {q(db, 'SELECT v FROM state WHERE k=' + chr(39) + 'robots' + chr(39))}; "
              f"captures {q(db, 'SELECT count(*) FROM capture')}, relevant threads "
              f"{q(db, 'SELECT count(*) FROM thread WHERE relevant=1')}, posts {q(db, 'SELECT count(*) FROM post')}")
    pf = c.path(c.p["gm_live"]["pid_file"])
    print(f"  GM PIDs {c.p['gm_live']['pid_file']}: " + (f"{len(json.load(open(pf))['pids'])} PIDs" if os.path.exists(pf) else "not built"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", help="profile JSON (name in profiles/ or a path)")
    ap.add_argument("step", choices=["check", "nhtsa", "vin", "charm-list", "kb", "dtc-names", "reddit-get", "reddit",
                                     "forums", "gm-pids", "embed", "status"])
    ap.add_argument("xlsx", nargs="?", help="gm-pids: optional community PID .xlsx (comma list); PCMHammer is read from raw/pcmhammer")
    ap.add_argument("--root", default="~/subaru_cyberdeck")
    ap.add_argument("--vin", action="append", help="vin: also decode this VIN (repeatable)")
    ap.add_argument("--topup", action="store_true", help="reddit: also pull 2025+ posts from the Arctic Shift API")
    ap.add_argument("--only", help="forums: comma list of forum names")
    ap.add_argument("--allow-unknown", action="store_true", help="forums: pull even when robots.txt can't be read")
    a = ap.parse_args()
    c = Ctx(a)
    {"check": step_check, "nhtsa": step_nhtsa, "vin": step_vin, "charm-list": step_charm_list, "kb": step_kb,
     "dtc-names": dtc_names, "reddit-get": step_reddit_get, "reddit": step_reddit, "forums": step_forums,
     "gm-pids": step_gm_pids, "embed": step_embed, "status": step_status}[a.step](c)


if __name__ == "__main__":
    main()
