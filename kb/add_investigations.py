#!/usr/bin/env python3
"""
NHTSA defect investigations (FLAT_INV) -> 'nhtsa_investigation' rows in each make's knowledge table.
An investigation is NHTSA looking into a suspected defect across many owners (it may end in a recall):
one row per investigation and model, with the model years it covers. No GPU; seconds per make.
Run `build_subaru_kb.py --db X embed --missing` afterwards for vectors (keyword search finds them without).

  python3 add_investigations.py kb/chevrolet.db kb/subaru.db   # these DBs (make from the file name)
  python3 add_investigations.py --done                         # every finished make not done yet, and subaru
Downloads raw/nhtsa/inv/FLAT_INV.zip (a few MB) the first time.
"""
import argparse
import csv
import io
import os
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from knowledge_load import HERE, make_names, target_dbs, write_kind   # noqa: E402

RAW = os.path.join(os.path.dirname(HERE), "raw", "nhtsa", "inv")
URL = "https://static.nhtsa.gov/odi/ffdd/inv/FLAT_INV.zip"
Y0, Y1 = 1982, 2013
ACTION = re.compile(r"^[A-Z]{2}\d{5,6}$")      # e.g. PE10022, EA11003, DP09001, RQ10002
# FLAT_INV.txt columns (NHTSA INV.txt): action no, make, model, year, component, manufacturer, opened,
# closed, recall campaign, subject, summary


def fetch(dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"downloading {URL}", flush=True)
    req = urllib.request.Request(URL, headers={"User-Agent": "subaru-cyberdeck/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
        while b := r.read(1 << 20):
            f.write(b)
    os.replace(dest + ".part", dest)


def rows(path):
    csv.field_size_limit(1 << 24)
    with zipfile.ZipFile(path) as z:
        for m in z.namelist():
            if m.upper().endswith(".TXT") and "INV" in m.upper() and m.upper() != "INV.TXT":
                with z.open(m) as fh:
                    for line in io.TextIOWrapper(fh, "latin-1", newline=""):
                        yield line.rstrip("\r\n").split("\t")


def offset(path):
    """Column where the action number sits (0 per NHTSA's layout; checked, since an old script was off by one)."""
    hits = defaultdict(int)
    for i, r in enumerate(rows(path)):
        for j in range(min(3, len(r))):
            if ACTION.match(r[j].strip()) and len(r) > j + 3 and re.fullmatch(r"\d{4}", r[j + 3].strip()):
                hits[j] += 1
        if i >= 300:
            break
    if not hits:
        sys.exit(f"{path}: no rows look like investigations (layout changed?)")
    return max(hits, key=hits.get)


def date(s):
    s = (s or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


def load(path):
    """{MAKE: {(action, MODEL): fields}} for model years Y0-Y1."""
    k = offset(path)
    out = defaultdict(dict)
    for r in rows(path):
        r = [x.strip() for x in r[k:]] + [""] * 11
        act, make, model, year = r[0], r[1].upper(), r[2].upper(), r[3]
        if not ACTION.match(act) or not year.isdigit() or not Y0 <= int(year) <= Y1 or not model:
            continue
        e = out[make].setdefault((act, model), {"years": set(), "comp": set(), "odate": date(r[6]),
                                                "cdate": date(r[7]), "camp": r[8], "subj": r[9], "summ": r[10]})
        e["years"].add(int(year))
        if r[4]:
            e["comp"].add(r[4])
    return out


def as_rows(found):
    out = []
    for (act, model), e in sorted(found.items()):
        comp = "; ".join(sorted(e["comp"]))
        when = e["odate"] + (f" to {e['cdate']}" if e["cdate"] else ", open")
        title = f"NHTSA investigation {act} ({when}): {e['subj'] or comp}"
        body = (f"Component: {comp}\n" if comp else "") + e["summ"] + (
            f"\nThis investigation led to recall {e['camp']}." if e["camp"] else "")
        out.append((act, model, min(e["years"]), max(e["years"]), title, body.strip(),
                    None, None))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbs", nargs="*")
    ap.add_argument("--done", action="store_true")
    ap.add_argument("--zip", default=os.path.join(RAW, "FLAT_INV.zip"))
    ap.add_argument("--refresh", action="store_true", help="download a fresh FLAT_INV.zip")
    a = ap.parse_args()
    dbs = target_dbs(a.dbs, a.done, "inv")
    if not dbs:
        print("investigations: nothing to do")
        return
    if a.refresh or not os.path.isfile(a.zip):
        fetch(a.zip)
    data = load(a.zip)
    for db in dbs:
        name = os.path.splitext(os.path.basename(db))[0]
        found = {}
        for m in make_names(name):
            found.update(data.get(m, {}))
        n = write_kind(db, "nhtsa_investigation", as_rows(found), "inv")
        print(f"{name}: {n} investigation rows", flush=True)


if __name__ == "__main__":
    main()
