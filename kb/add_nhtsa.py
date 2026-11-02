#!/usr/bin/env python3
"""
Add NHTSA TSBs, complaints and recalls to a per-make knowledge DB (kb/<make>.db), in the same 'knowledge'
rows build_subaru_kb.py's diag step gives subaru.db, so the app's retriever finds them unchanged.

Reads only the NHTSA flat files already on the PC (raw/nhtsa/{tsbs,cmpl,rcl}/*.zip, fetched by
build_subaru_diag_db.py). The first run sorts them by make into kb/nhtsa_all.db (one pass, a few minutes);
later runs read that. Model years 1982-2013, like CHARM. No GPU: run `build_subaru_kb.py --db X embed --missing`
afterwards to give the new rows vectors (keyword search finds them without).

  python3 add_nhtsa.py kb/acura.db kb/audi.db          # these DBs (make from the file name)
  python3 add_nhtsa.py --done                          # every make the CHARM loop has finished (kb/.<db>.done)
"""
import argparse
import glob
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CK = os.path.dirname(HERE)
RAW = os.path.join(CK, "raw")
CACHE = os.path.join(HERE, "nhtsa_all.db")
Y0, Y1 = 1982, 2013

# kb file name -> NHTSA MAKETXT values (Ram trucks live in dodge.db, as CHARM's "Dodge or Ram Truck" does)
MAKES = {"dodge": ["DODGE", "RAM"], "mercedes_benz": ["MERCEDES-BENZ", "MERCEDES BENZ"],
         "nissan": ["NISSAN", "DATSUN"], "land_rover": ["LAND ROVER"], "mitsubishi_fuso": ["MITSUBISHI FUSO"],
         "mini": ["MINI"], "ud": ["UD", "UD TRUCKS"], "srt": ["SRT"]}
SKIP = {"subaru"}           # built separately; not touched here


def build_cache():
    sys.path.insert(0, CK)
    from build_subaru_diag_db import iter_flat_rows, _int, _date
    tmp = CACHE + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript("""
        CREATE TABLE tsb (make TEXT, nhtsa_id INTEGER, bulletin_no TEXT, comm_date TEXT, model TEXT, model_year INTEGER,
                          components TEXT, summary TEXT);
        CREATE TABLE complaint (make TEXT, cmpl_id INTEGER, model TEXT, model_year INTEGER, component TEXT, summary TEXT,
                                miles INTEGER, fail_date TEXT);
        CREATE TABLE recall (make TEXT, campaign TEXT, model TEXT, model_year INTEGER, component TEXT, defect TEXT,
                             consequence TEXT, remedy TEXT);""")
    keep = lambda yr: yr is not None and Y0 <= yr <= Y1   # noqa: E731
    n = {"tsb": 0, "complaint": 0, "recall": 0}
    for r in iter_flat_rows(os.path.join(RAW, "nhtsa", "tsbs")):      # field order: build_subaru_diag_db.step_nhtsa
        if len(r) >= 14 and keep(_int(r[9])):
            c.execute("INSERT INTO tsb VALUES (?,?,?,?,?,?,?,?)", (r[7].strip().upper(), _int(r[0]), r[3].strip(),
                      _date(r[4]), r[8].strip().upper(), _int(r[9]), r[10].strip(), r[13].strip()))
            n["tsb"] += 1
    for r in iter_flat_rows(os.path.join(RAW, "nhtsa", "cmpl")):
        if len(r) >= 20 and keep(_int(r[5])):
            c.execute("INSERT INTO complaint VALUES (?,?,?,?,?,?,?,?)", (r[3].strip().upper(), _int(r[0]),
                      r[4].strip().upper(), _int(r[5]), r[11].strip(), r[19].strip(), _int(r[17]), _date(r[7])))
            n["complaint"] += 1
    for r in iter_flat_rows(os.path.join(RAW, "nhtsa", "rcl")):
        if len(r) >= 22 and keep(_int(r[4])):
            defect = r[19].strip() or (r[22].strip() if len(r) > 22 else "")
            c.execute("INSERT INTO recall VALUES (?,?,?,?,?,?,?,?)", (r[2].strip().upper(), r[1].strip(),
                      r[3].strip().upper(), _int(r[4]), r[6].strip(), defect, r[20].strip(), r[21].strip()))
            n["recall"] += 1
    for t in n:
        c.execute(f"CREATE INDEX {t}_make ON {t}(make)")
    c.commit()
    c.close()
    os.replace(tmp, CACHE)
    print("nhtsa cache:", n, flush=True)


def add_miles(c, makes):
    """nhtsa_miles: odometer miles and failure date per owner complaint (ref = knowledge.ref of its
    'nhtsa_complaint' row), for "most common problems and at what mileage". Rows with no miles are left out."""
    marks = ",".join("?" * len(makes))
    c.execute("CREATE TABLE IF NOT EXISTS nhtsa_miles (ref TEXT PRIMARY KEY, miles INTEGER, fail_date TEXT)")
    c.execute("DELETE FROM nhtsa_miles")
    c.execute("INSERT OR REPLACE INTO nhtsa_miles SELECT CAST(cmpl_id AS TEXT), miles, fail_date FROM n.complaint "
              f"WHERE make IN ({marks}) AND miles > 0", makes)
    return c.execute("SELECT count(*) FROM nhtsa_miles").fetchone()[0]


def miles_only(db):
    """Only (re)build nhtsa_miles in an existing DB; nothing else is touched (also for subaru.db)."""
    name = os.path.splitext(os.path.basename(db))[0]
    makes = MAKES.get(name, [name.replace("_", " ").upper()])
    c = sqlite3.connect(db, uri=True)
    c.execute("ATTACH DATABASE ? AS n", (f"file:{CACHE}?mode=ro",))
    n = add_miles(c, makes)
    c.commit()
    c.close()
    print(f"{name}: nhtsa_miles {n}", flush=True)


def add(db):
    name = os.path.splitext(os.path.basename(db))[0]
    if name in SKIP:
        print(f"skip {name}")
        return
    makes = MAKES.get(name, [name.replace("_", " ").upper()])
    marks = ",".join("?" * len(makes))
    c = sqlite3.connect(db, uri=True)
    c.execute("ATTACH DATABASE ? AS n", (f"file:{CACHE}?mode=ro",))
    for kind in ("tsb", "nhtsa_complaint", "recall"):
        c.execute("DELETE FROM knowledge WHERE kind=?", (kind,))
    # same columns and wording as build_subaru_kb.step_diag
    c.execute("INSERT OR REPLACE INTO knowledge(kind,ref,model,year_min,year_max,title,body,url,confirmed) "
              "SELECT 'tsb', nhtsa_id, model, model_year, model_year, 'TSB ' || coalesce(bulletin_no,'') || ' (' || "
              f"coalesce(comm_date,'') || '): ' || coalesce(components,''), summary, NULL, NULL FROM n.tsb WHERE make IN ({marks})",
              makes)
    c.execute("INSERT OR REPLACE INTO knowledge(kind,ref,model,year_min,year_max,title,body,url,confirmed) "
              "SELECT 'nhtsa_complaint', cmpl_id, model, model_year, model_year, component, summary, NULL, NULL "
              f"FROM n.complaint WHERE make IN ({marks})", makes)
    c.execute("INSERT OR REPLACE INTO knowledge(kind,ref,model,year_min,year_max,title,body,url,confirmed) "
              "SELECT 'recall', campaign || ' ' || coalesce(component,''), model, model_year, model_year, component, "
              "coalesce(defect,'') || char(10) || 'Consequence: ' || coalesce(consequence,'') || char(10) || "
              f"'Remedy: ' || coalesce(remedy,''), NULL, NULL FROM n.recall WHERE make IN ({marks})", makes)
    add_miles(c, makes)
    c.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    c.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('optimize')")
    c.commit()
    counts = dict(c.execute("SELECT kind, count(*) FROM knowledge GROUP BY kind").fetchall())
    c.close()
    open(os.path.join(HERE, f".{name}.nhtsa"), "w").write(str(counts) + "\n")
    print(f"{name} ({'/'.join(makes)}): {counts}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbs", nargs="*")
    ap.add_argument("--done", action="store_true", help="every finished CHARM make not given NHTSA rows yet")
    ap.add_argument("--rebuild-cache", action="store_true")
    ap.add_argument("--miles-only", action="store_true", help="only (re)build nhtsa_miles in the given DBs")
    a = ap.parse_args()
    if a.rebuild_cache or not os.path.isfile(CACHE):
        build_cache()
    dbs = list(a.dbs)
    if a.done:
        for mark in sorted(glob.glob(os.path.join(HERE, ".*.done"))):
            name = os.path.basename(mark)[1:-5]
            if not os.path.isfile(os.path.join(HERE, f".{name}.nhtsa")):
                for d in (os.path.join(HERE, f"{name}.db"),
                          os.path.join(os.environ.get("CYBERDECK_ARCHIVE", os.path.expanduser("~/cyberdeck_archive")), "kb_build", f"{name}.db")):
                    if os.path.isfile(d):
                        dbs.append(d)
                        break
    if a.miles_only:
        for db in dbs:
            miles_only(db)
        return
    for db in dbs:
        add(db)


if __name__ == "__main__":
    main()
