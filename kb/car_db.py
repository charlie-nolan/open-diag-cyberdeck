#!/usr/bin/env python3
"""
Helpers for tools/build_car_db.sh: build a knowledge DB for ONE car (make, model, year) in the same layout as a
make DB (kb/<make>.db), so the app and kb_retriever read it unchanged.

The builder steps (build_subaru_kb.py fsm/charm/diag/cards/embed/finish) run on the new DB directly. This file holds
the pieces that had no one-car form, using the existing scripts as libraries (none of them is edited):

  extras    NHTSA TSBs/complaints/recalls (add_nhtsa), NHTSA investigations (add_investigations), Mechanics
            StackExchange (add_stackexchange) into the new DB. The DB must be named <make>.db (those loaders take the
            make from the file name); their marker files go next to the new DB, not into kb/.
  copy-docs Copy one car's manual pages (CHARM and/or FSM docs) from an existing make DB, read-only: sections, the
            car's vehicle rows, specs, labor, CHARM provenance, DTC links, cross refs, CHARM picture refs. Used when
            the CHARM text image is not mounted, or when no FSM markdown is given.
  community Copy the car's community threads (comm_* tables) from the make DB: same model, year within --window
            (community_ref's "same model and years" rule), with their vectors. Manual links are re-pointed to
            the new DB's section ids by page text hash.
  telemetry Copy telem_* (parameters, conversions, conditions: whole; zones, pathways, capabilities: this car's
            model/years or no model), section ids re-pointed the same way, pathway vectors copied.
  trim      Drop rows for other cars: vehicles, their pages, knowledge/cards of other models or years (rows with no
            model are make-wide and stay).
  reuse-vectors  Copy section/knowledge/card vectors from the make DB for rows whose text is identical (same
            embedding model), so `embed --missing` only embeds what is new. Optional fast path.
  images    Store the car's pictures INSIDE the DB (table picture), so a one-car DB needs no picture server and no
            images/ folder: the factory-manual figures (grayscale WebP, like kb/ship_images.py) from the converted
            manual's folder, the CHARM page pictures from the CHARM images LMDB (re-encoded to WebP only when wide
            or big), and any chart readings already made (kb/charm_reads.db). Each source is skipped when missing.
  community-select  Pick one car's forum/Reddit threads from the raw scrape for a fresh extraction: runs
            build_community.py's pass 0 (prefilter) into <work-dir>/_prefilter/, then keeps threads whose scrape tags,
            title/opening post or sub/forum name name the model (kb_retriever's aliases) and that mention no year, or
            a year within --year +- --window. Optional --min-replies/--any-of (the extraction's own filters) and a time
            budget (--budget-hours x --rate threads/h): over budget, a seeded random sample. Writes
            <work-dir>/<make>.work.db (the extraction's work DB) and <work-dir>/select.json (counts).
            Then: build_community.py --work-dir <work-dir> --make <make> extract  (any backend/model).
  community-load  build_community.py's pass 2 (tables, manual links against THIS DB, vectors) on <work-dir>, then
            the car's comm_* rows into this DB (same model/year rule as `community`). Nothing in kb/ is written.
  html-manual  Load a folder of HTML manual pages for one car (a LEMON Manuals single-car download: the zip or its
            extracted folder; also any CHARM-style page folder) with the builder's own CHARM page parser (charm_text,
            charm_parts, charm_specs_labor, CHARM_DTC_RE, labor_from_sections). Breadcrumbs give the page path; listing
            pages are skipped; the pages' pictures go into the DB's picture table under CHARM-style keys.
  check     Row counts vs the make DB for the same car, and a few retriever searches.

Every source DB is opened read-only (file:...?mode=ro).
"""
import argparse
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def ro(path):
    return f"file:{os.path.abspath(os.path.expanduser(path))}?mode=ro"


def open_rw(db):
    c = sqlite3.connect(db, timeout=600, uri=True)    # uri=True: ATTACH file:...?mode=ro works
    c.execute("PRAGMA foreign_keys=OFF")
    return c


def load_vec(c):
    try:
        import sqlite_vec
    except ImportError:
        return False
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return True


def model_names(model):
    """Upper-case names this model's knowledge rows may carry (kb_retriever's aliases, else the name)."""
    try:
        from kb_retriever import MODEL_ALIASES
    except Exception:
        MODEL_ALIASES = {}
    return MODEL_ALIASES.get(model.upper(), [model.upper()])


def has_table(c, name, schema="main"):
    return c.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def section_map(c):
    """{make-DB section id: new section id} by (doc kind, body hash): the same parser gives the same hash, so
    FSM pages loaded fresh and CHARM pages copied both map. Needs the make DB attached as 's'."""
    return dict(c.execute(
        "SELECT ss.id, ns.id FROM s.section ss JOIN s.doc sd ON sd.id=ss.doc_id "
        "JOIN main.section ns ON ns.body_hash=ss.body_hash JOIN main.doc nd ON nd.id=ns.doc_id AND nd.kind=sd.kind"))


def car_vehicle_ids(c, model, year, schema="main"):
    return [r[0] for r in c.execute(f"SELECT id FROM {schema}.vehicle WHERE upper(model)=upper(?) AND year=?",
                                    (model, int(year)))]


# ------------------------------------------------------------------ extras (NHTSA, investigations, StackExchange)
def cmd_extras(a):
    db = os.path.abspath(a.db)
    work = os.path.dirname(db)
    name = os.path.splitext(os.path.basename(db))[0]
    # add_nhtsa: its SKIP list keeps the real subaru.db out of batch runs; this is a new file, so clear it.
    # Marker files (".<make>.nhtsa" etc.) go next to the new DB instead of kb/.
    if not os.path.isfile(os.path.join(HERE, "nhtsa_all.db")):
        print("nhtsa: skipped (kb/nhtsa_all.db missing; build it with kb/add_nhtsa.py --rebuild-cache)")
    else:
        an = importlib.import_module("add_nhtsa")
        an.SKIP = set()
        an.HERE = work
        an.add(db)
    import knowledge_load
    # import both loaders first: their data paths (RAW, CACHE) are computed from knowledge_load.HERE at import
    inv = importlib.import_module("add_investigations")
    se = importlib.import_module("add_stackexchange")
    knowledge_load.HERE = work          # only write_kind's marker file reads it from here on
    if os.path.isfile(os.path.join(inv.RAW, "FLAT_INV.zip")):
        data = inv.load(os.path.join(inv.RAW, "FLAT_INV.zip"))
        found = {}
        for m in knowledge_load.make_names(name):
            found.update(data.get(m, {}))
        print(f"investigations: {knowledge_load.write_kind(db, 'nhtsa_investigation', inv.as_rows(found), 'inv')} rows")
    else:
        print("investigations: skipped (raw/nhtsa/inv/FLAT_INV.zip missing; kb/add_investigations.py downloads it)")
    if os.path.isfile(se.CACHE):
        s = sqlite3.connect(ro(se.CACHE), uri=True)
        qa = se.qa_rows(s).fetchall()
        s.close()
        rows = se.match(db, qa)
        print(f"stackexchange: {knowledge_load.write_kind(db, 'stackexchange', rows, 'se')} rows")
    else:
        print("stackexchange: skipped (kb/stackexchange_qa.db missing; kb/add_stackexchange.py builds it)")


# ------------------------------------------------------------------ copy one car's pages from a make DB
def cmd_copy_docs(a):
    c = open_rw(a.db)
    c.execute("ATTACH DATABASE ? AS s", (ro(a.src),))
    kinds = [k.strip() for k in a.kinds.split(",") if k.strip()]
    vids = car_vehicle_ids(c, a.model, a.year, "s")
    if not vids:
        print(f"copy-docs: no {a.year} {a.model} in {a.src}")
        return
    q = ",".join("?" * len(vids))
    kq = ",".join("?" * len(kinds))
    docs = c.execute(f"SELECT DISTINCT d.id, d.kind, d.title, d.file_name, d.license FROM s.doc d "
                     f"JOIN s.section x ON x.doc_id=d.id JOIN s.section_vehicle sv ON sv.section_id=x.id "
                     f"WHERE sv.vehicle_id IN ({q}) AND d.kind IN ({kq})", vids + kinds).fetchall()
    if not docs:
        print(f"copy-docs: {a.src} has no {'/'.join(kinds)} pages for {a.year} {a.model}")
        return
    # vehicles: same (model, chassis, year, engine, market) -> reuse; else insert
    vmap = {}
    for vid, model, chassis, year, engine, trans, market in c.execute(
            f"SELECT id, model, chassis, year, engine, transmission, market FROM s.vehicle WHERE id IN ({q})", vids):
        row = c.execute("SELECT id FROM vehicle WHERE model=? AND chassis IS ? AND year=? AND engine IS ? AND market=?",
                        (model, chassis, year, engine, market)).fetchone()
        vmap[vid] = row[0] if row else c.execute(
            "INSERT INTO vehicle(model,chassis,year,engine,transmission,market) VALUES (?,?,?,?,?,?)",
            (model, chassis, year, engine, trans, market)).lastrowid
    st = {}
    for did, kind, title, fname, lic in docs:
        if c.execute("SELECT 1 FROM doc WHERE title=?", (title,)).fetchone():
            print(f"copy-docs: '{title}' is already in the new DB; not copied again")
            continue
        new_doc = c.execute("INSERT INTO doc(kind,title,file_name,license) VALUES (?,?,?,?)",
                            (kind, title, fname, lic)).lastrowid
        rows = c.execute(f"SELECT DISTINCT x.id FROM s.section x JOIN s.section_vehicle sv ON sv.section_id=x.id "
                         f"WHERE x.doc_id=? AND sv.vehicle_id IN ({q})", [did] + vids).fetchall()
        smap = {}
        for (sid,) in rows:
            r = c.execute("SELECT chapter,topic,fsm_code,title,path,part,page,body,images,body_hash FROM s.section "
                          "WHERE id=?", (sid,)).fetchone()
            smap[sid] = c.execute("INSERT INTO section(doc_id,chapter,topic,fsm_code,title,path,part,page,body,images,"
                                  "body_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (new_doc,) + r).lastrowid
        c.execute("CREATE TEMP TABLE IF NOT EXISTS m (old INTEGER PRIMARY KEY, new INTEGER)")
        c.execute("DELETE FROM m")
        c.executemany("INSERT INTO m VALUES (?,?)", smap.items())
        c.execute("CREATE TEMP TABLE IF NOT EXISTS vm (old INTEGER PRIMARY KEY, new INTEGER)")
        c.execute("DELETE FROM vm")
        c.executemany("INSERT INTO vm VALUES (?,?)", vmap.items())
        c.execute("INSERT OR IGNORE INTO section_vehicle SELECT vm.new, m.new FROM s.section_vehicle sv "
                  "JOIN m ON m.old=sv.section_id JOIN vm ON vm.old=sv.vehicle_id")
        c.execute("INSERT INTO spec(section_id,kind,component,value_si,tol_si,unit_si,value_us,tol_us,unit_us,raw) "
                  "SELECT m.new,kind,component,value_si,tol_si,unit_si,value_us,tol_us,unit_us,raw FROM s.spec p "
                  "JOIN m ON m.old=p.section_id")
        c.execute("INSERT INTO labor(section_id,operation,hours,part_number,price,raw) "
                  "SELECT m.new,operation,hours,part_number,price,raw FROM s.labor l JOIN m ON m.old=l.section_id")
        c.execute("INSERT OR IGNORE INTO charm_source SELECT m.new, source_key, source_ptr FROM s.charm_source x "
                  "JOIN m ON m.old=x.section_id")
        c.execute("INSERT OR IGNORE INTO dtc(code) SELECT DISTINCT p.code FROM s.dtc_procedure p "
                  "JOIN m ON m.old=p.section_id JOIN vm ON vm.old=p.vehicle_id")
        c.execute("INSERT OR IGNORE INTO dtc_procedure SELECT p.code, vm.new, m.new FROM s.dtc_procedure p "
                  "JOIN m ON m.old=p.section_id JOIN vm ON vm.old=p.vehicle_id")
        c.execute("INSERT OR IGNORE INTO section_ref(from_id, ref_text, to_id) SELECT mf.new, r.ref_text, mt.new "
                  "FROM s.section_ref r JOIN m mf ON mf.old=r.from_id LEFT JOIN m mt ON mt.old=r.to_id")
        if has_table(c, "charm_image", "s"):
            if not has_table(c, "charm_image"):
                c.execute(c.execute("SELECT sql FROM s.sqlite_master WHERE name='charm_image'").fetchone()[0])
            c.execute("INSERT OR IGNORE INTO charm_image SELECT i.img_key, m.new, i.page_title, i.bytes, i.width, "
                      "i.height, i.fmt, i.kind FROM s.charm_image i JOIN m ON m.old=i.section_id")
        st[title] = len(smap)
    c.execute("DELETE FROM spec_fts")
    c.execute("INSERT INTO spec_fts(rowid, component, context) SELECT p.id, p.component, "
              "coalesce(s2.topic,'') || ' ' || s2.title FROM spec p JOIN section s2 ON s2.id=p.section_id")
    c.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
    c.commit()
    n = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("section", "spec", "labor", "dtc_procedure")}
    print("copy-docs:", ", ".join(f"{k}: {v} pages" for k, v in st.items()), "| totals", n)


# ------------------------------------------------------------------ schema copy for comm_* / telem_*
def copy_schema(c, prefix):
    """Create the src tables (and indexes) whose name starts with prefix, vec0/fts5 shadow tables excluded."""
    objs = c.execute("SELECT type, name, tbl_name, sql FROM s.sqlite_master WHERE name LIKE ? AND sql IS NOT NULL",
                     (prefix + "%",)).fetchall()
    virt = [n for t, n, _, sql in objs if sql.upper().startswith("CREATE VIRTUAL")]
    made = []
    for t, n, tbl, sql in objs:
        if t == "table" and any(n.startswith(v + "_") for v in virt):
            continue
        if t == "index" and any(tbl.startswith(v + "_") for v in virt):
            continue
        if t in ("table", "index") and not c.execute("SELECT 1 FROM main.sqlite_master WHERE name=?", (n,)).fetchone():
            if "USING vec0" in sql and not load_vec(c):
                print(f"  {n}: sqlite-vec missing, vectors not copied")
                continue
            c.execute(sql)
            made.append(n)
    return made


def cols(c, table, schema="s"):
    return [r[1] for r in c.execute(f"PRAGMA {schema}.table_info({table})")]


def copy_rows(c, table, where="", args=(), select=None):
    cl = cols(c, table)
    sel = select or ", ".join(f"x.{k}" for k in cl)
    c.execute(f"INSERT OR IGNORE INTO main.{table}({', '.join(cl)}) SELECT {sel} FROM s.{table} x {where}", args)
    return c.execute(f"SELECT count(*) FROM main.{table}").fetchone()[0]


def copy_vectors(c, vec, ids_sql):
    if not c.execute("SELECT 1 FROM s.sqlite_master WHERE name=?", (vec,)).fetchone():
        return 0
    if not load_vec(c):
        return 0
    c.execute(f"INSERT OR IGNORE INTO main.{vec}(id, embedding) SELECT id, embedding FROM s.{vec} WHERE id IN ({ids_sql})")
    return c.execute(f"SELECT count(*) FROM main.{vec}").fetchone()[0]


# ------------------------------------------------------------------ community
COMM_KINDS = {"outcome": ("comm_outcome", "thread_id"), "ruled_out": ("comm_ruled_out", "id"),
              "suggestion": ("comm_suggestion", "id"), "test": ("comm_test", "id"),
              "chain": ("comm_chain", "id"), "symptom": ("comm_symptom", "id")}


def cmd_community(a, remap=True):
    c = open_rw(a.db)
    load_vec(c)
    c.execute("ATTACH DATABASE ? AS s", (ro(a.src),))
    if not has_table(c, "comm_thread", "s"):
        print(f"community: skipped ({a.src} has no comm_* tables)")
        return
    copy_schema(c, "comm_")
    names = model_names(a.model)
    make = os.path.splitext(os.path.basename(a.db))[0].replace("_", " ").upper()   # built as <make>.db
    names = names + [f"{make} {n}" for n in names]          # extracted records also write "<Make> <Model>"
    q = ",".join("?" * len(names))
    y = int(a.year)
    y0, y1 = (int(x) for x in a.years.split("-")) if getattr(a, "years", None) else (y - a.window, y + a.window)
    c.execute("CREATE TEMP TABLE th (id INTEGER PRIMARY KEY)")
    c.execute(f"INSERT INTO th SELECT id FROM s.comm_thread WHERE upper(trim(model)) IN ({q}) AND "
              f"(year IS NULL OR year BETWEEN ? AND ?)", names + [y0, y1])
    n = {"comm_thread": copy_rows(c, "comm_thread", "WHERE x.id IN (SELECT id FROM th)")}
    for t in [r[0] for r in c.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name LIKE 'comm_%'")]:
        if t in ("comm_thread", "comm_meta", "comm_count", "comm_link") or not has_table(c, t, "s"):
            continue
        if "thread_id" in cols(c, t):
            n[t] = copy_rows(c, t, "WHERE x.thread_id IN (SELECT id FROM th)")
    if has_table(c, "comm_meta", "s"):
        copy_rows(c, "comm_meta")
        c.execute("INSERT OR REPLACE INTO comm_meta VALUES ('one_car', ?)",
                  (json.dumps({"model": a.model, "year": y, "years": [y0, y1],
                               "source": "make DB" if remap else "fresh extraction"}),))
    if has_table(c, "comm_count", "s"):
        n["comm_count"] = copy_rows(c, "comm_count", f"WHERE x.model IS NULL OR upper(x.model) IN ({q})", names)
    if has_table(c, "comm_link", "s"):
        c.execute("CREATE TEMP TABLE m (old INTEGER PRIMARY KEY, new INTEGER)")
        if remap:     # make DB: its section ids -> this DB's by page text hash
            c.executemany("INSERT INTO m VALUES (?,?)", section_map(c).items())
        else:         # fresh tables step: links were made against this DB already
            c.execute("INSERT INTO m SELECT id, id FROM main.section")
        for kind, (tab, key) in COMM_KINDS.items():
            if not has_table(c, tab):
                continue
            c.execute(f"INSERT OR IGNORE INTO main.comm_link(kind,row_id,section_id,rank,method) "
                      f"SELECT l.kind, l.row_id, m.new, l.rank, l.method FROM s.comm_link l JOIN m ON m.old=l.section_id "
                      f"WHERE l.kind=? AND l.row_id IN (SELECT {key} FROM main.{tab})", (kind,))
        n["comm_link"] = c.execute("SELECT count(*) FROM main.comm_link").fetchone()[0]
    if has_table(c, "comm_doc"):
        c.execute("INSERT INTO comm_doc_fts(comm_doc_fts) VALUES ('rebuild')")
        n["comm_doc_vec"] = copy_vectors(c, "comm_doc_vec", "SELECT id FROM main.comm_doc")
    c.commit()
    print("community:", ", ".join(f"{k} {v}" for k, v in n.items()))


# ------------------------------------------------------------------ community from the raw scrape (fresh extraction)
def _bc(work_dir, make, log=None):
    """build_community.py as a library, its work/output folders pointed at work_dir (nothing in kb/ is written)."""
    bc = importlib.import_module("build_community")
    bc.WORK = bc.OUT = os.path.abspath(work_dir)
    bc.LOG = log or os.path.join(os.path.dirname(HERE), "logs", f"community_{make}_{os.path.basename(bc.WORK)}.log")
    os.makedirs(bc.WORK, exist_ok=True)
    return bc


def _word_re(names):
    import re
    alts = sorted({re.escape(n.lower()).replace(r"\ ", r"[\s_-]*") for n in names}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


def _years_in(text):
    import re
    return {int(y) for y in re.findall(r"(?<![0-9])((?:19|20)[0-9]{2})(?![0-9])", text or "")}


def cmd_community_select(a):
    import random
    make = a.make.lower()
    names = model_names(a.model)
    wre = _word_re(names)
    y0, y1 = a.year - a.window, a.year + a.window
    if a.years:                      # explicit range, e.g. the car's generation: 1998-2002
        y0, y1 = (int(x) for x in a.years.split("-"))
    stage = os.path.join(os.path.abspath(a.work_dir), "_prefilter")
    bc = _bc(stage, make, a.log)
    w0 = bc.work_db(make)
    t0 = time.time()
    if a.reprefilter or not w0.execute("SELECT 1 FROM thread LIMIT 1").fetchone():
        if not bc.sources(make):
            sys.exit(f"community-select: build_community.py finds no Reddit/forum dump for '{make}'")
        bc.step_prefilter(make)
    t_pre = time.time() - t0
    total = w0.execute("SELECT count(*) FROM thread").fetchone()[0]
    keep, why = [], {"model_tag": 0, "model_text": 0, "model_sub": 0, "no_year": 0, "year_in_window": 0}
    for src, tid, sub, title, segs, tags in w0.execute("SELECT src, tid, sub, title, segs, tags FROM thread"):
        tg = json.loads(tags or "{}")
        op = (json.loads(segs)[0]["t"] if segs else "") or ""
        hit = None
        if tg.get("models") and wre.search(str(tg["models"]).lower()):
            hit = "model_tag"
        elif wre.search(f"{title or ''}\n{op}".lower()):
            hit = "model_text"
        elif sub and wre.search(sub.lower().replace("_", " ")):
            hit = "model_sub"
        if not hit:
            continue
        ys = _years_in(str(tg.get("years") or "")) or _years_in(f"{title or ''}\n{op}")
        if ys and not any(y0 <= y <= y1 for y in ys):
            continue
        why[hit] += 1
        why["year_in_window" if ys else "no_year"] += 1
        keep.append((src, tid))
    # the extraction's own filters (build_community.thread_filter), so the budget counts what would really run
    ns = argparse.Namespace(pilot=False, min_replies=a.min_replies, any_of=a.any_of)
    where, args = bc.thread_filter(ns)
    w0.execute("CREATE TEMP TABLE k (src TEXT, tid TEXT, PRIMARY KEY (src, tid))")
    w0.executemany("INSERT OR IGNORE INTO k VALUES (?,?)", keep)
    runnable = w0.execute(f"SELECT t.src, t.tid FROM thread t JOIN k ON k.src=t.src AND k.tid=t.tid WHERE {where}",
                          args).fetchall()
    budget = int(a.budget_hours * a.rate) if a.budget_hours and a.rate else 0
    chosen = runnable
    if budget and len(runnable) > budget:
        chosen = random.Random(a.seed).sample(sorted(runnable), budget)
    out = os.path.abspath(a.work_dir)
    bc.WORK = out
    w = bc.work_db(make)
    if w.execute("SELECT count(*) FROM extract").fetchone()[0] and not a.force:
        sys.exit(f"community-select: {out}/{make}.work.db already has extractions; --force replaces the selection")
    w.execute("DELETE FROM thread")
    w.execute("ATTACH DATABASE ? AS p", (os.path.join(stage, f"{make}.work.db"),))   # our own staging copy
    w.execute("CREATE TEMP TABLE c (src TEXT, tid TEXT, PRIMARY KEY (src, tid))")
    w.executemany("INSERT INTO c VALUES (?,?)", chosen)
    w.execute("INSERT INTO thread SELECT t.* FROM p.thread t JOIN c ON c.src=t.src AND c.tid=t.tid")
    w.execute("INSERT OR REPLACE INTO sub_score SELECT * FROM p.sub_score")
    w.commit()
    info = {"make": make, "model": a.model, "names": names, "year": a.year, "years": [y0, y1],
            "min_replies": a.min_replies, "any_of": a.any_of, "seed": a.seed, "prefilter_kept": total,
            "model_and_year_match": len(keep), "match_reasons": why, "after_extract_filters": len(runnable),
            "budget_threads": budget or None, "selected": len(chosen), "sampled": len(chosen) < len(runnable),
            "prefilter_s": round(t_pre, 1)}
    with open(os.path.join(out, "select.json"), "w") as f:
        json.dump(info, f, indent=1)
    print("community-select:", json.dumps(info))


def cmd_community_load(a):
    """Pass 2 on a fresh extraction, linked to THIS DB's pages, then the car's rows into it."""
    make = a.make.lower()
    db = os.path.abspath(a.db)
    if os.path.splitext(os.path.basename(db))[0] != make:
        sys.exit(f"community-load: the DB must be named {make}.db while building (tables links through it)")
    bc = _bc(a.work_dir, make, a.log)
    if not os.path.isfile(os.path.join(bc.WORK, f"{make}.work.db")):
        sys.exit(f"community-load: no {bc.WORK}/{make}.work.db (run community-select and the extraction first)")
    bc.KB = os.path.dirname(db)          # links(): the manual sections searched are this DB's
    t0 = time.time()
    bc.step_tables(make, argparse.Namespace(pilot=False, vec=False))
    t1 = time.time()
    if a.device != "none":
        bc.step_embed(make, a.device)
    t2 = time.time()
    a.src = bc.cdb_path(make)
    cmd_community(a, remap=False)
    print(f"community-load: tables {t1 - t0:.0f} s, embed {t2 - t1:.0f} s, copy {time.time() - t2:.0f} s")


# ------------------------------------------------------------------ telemetry
def cmd_telemetry(a):
    c = open_rw(a.db)
    load_vec(c)
    c.execute("ATTACH DATABASE ? AS s", (ro(a.src),))
    if not has_table(c, "telem_param", "s"):
        print(f"telemetry: skipped ({a.src} has no telem_* tables)")
        return
    copy_schema(c, "telem_")
    names = model_names(a.model)
    q = ",".join("?" * len(names))
    y = int(a.year)
    scope = f"(x.model IS NULL OR upper(x.model) IN ({q})) AND ? BETWEEN coalesce(x.year_min,0) AND coalesce(x.year_max,9999)"
    n = {}
    for t in ("telem_param", "telem_condition", "telem_coverage", "telem_source_map", "telem_meta"):
        if has_table(c, t, "s"):
            n[t] = copy_rows(c, t)
    if has_table(c, "telem_capability", "s"):
        n["telem_capability"] = copy_rows(c, "telem_capability", "WHERE " + scope, names + [y])
    smap = section_map(c)
    c.execute("CREATE TEMP TABLE m (old INTEGER PRIMARY KEY, new INTEGER)")
    c.executemany("INSERT INTO m VALUES (?,?)", smap.items())
    for t, need in (("telem_zone", False), ("telem_path", True)):
        if not has_table(c, t, "s"):
            continue
        cl = cols(c, t)
        sel = ", ".join("m.new" if k == "section_id" else f"x.{k}" for k in cl)
        join = "JOIN m ON m.old=x.section_id" if need else "LEFT JOIN m ON m.old=x.section_id"
        # zones quote their row, so a zone whose page isn't in this DB keeps the quote with no page id
        c.execute(f"INSERT OR IGNORE INTO main.{t}({', '.join(cl)}) SELECT {sel} FROM s.{t} x {join} WHERE {scope}",
                  names + [y])
        n[t] = c.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
    if has_table(c, "telem_path"):
        n["telem_path_vec"] = copy_vectors(c, "telem_path_vec", "SELECT id FROM main.telem_path")
    c.commit()
    print("telemetry:", ", ".join(f"{k} {v}" for k, v in n.items()))


# ------------------------------------------------------------------ trim
def cmd_trim(a):
    c = open_rw(a.db)
    load_vec(c)
    names = model_names(a.model)
    q = ",".join("?" * len(names))
    y0, y1 = int(a.year) - a.window, int(a.year) + a.window
    before = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("vehicle", "section", "knowledge", "dtc_card")}
    keep_v = [r[0] for r in c.execute(f"SELECT id FROM vehicle WHERE upper(model) IN ({q}) AND year BETWEEN ? AND ?",
                                      names + [y0, y1])]
    kv = ",".join("?" * len(keep_v)) or "NULL"
    c.execute(f"DELETE FROM section_vehicle WHERE vehicle_id NOT IN ({kv})", keep_v)
    c.execute(f"DELETE FROM dtc_procedure WHERE vehicle_id NOT IN ({kv})", keep_v)
    c.execute(f"DELETE FROM vehicle WHERE id NOT IN ({kv})", keep_v)
    gone = "SELECT id FROM section WHERE id NOT IN (SELECT section_id FROM section_vehicle)"
    for t, col in (("spec", "section_id"), ("labor", "section_id"), ("charm_source", "section_id"),
                   ("section_ref", "from_id"), ("dtc_procedure", "section_id")):
        c.execute(f"DELETE FROM {t} WHERE {col} IN ({gone})")
    for t in ("charm_image",):
        if has_table(c, t):
            c.execute(f"DELETE FROM {t} WHERE section_id IN ({gone})")
    c.execute(f"UPDATE section_ref SET to_id=NULL WHERE to_id IN ({gone})")
    if has_table(c, "section_vec"):
        c.execute(f"DELETE FROM section_vec WHERE id IN ({gone})")
    c.execute(f"DELETE FROM section WHERE id IN ({gone})")
    # knowledge: rows with no model are make-wide and stay; others need this model and an overlapping year range
    kdel = (f"SELECT id FROM knowledge WHERE model IS NOT NULL AND trim(model) != '' AND NOT ("
            f"upper(model) IN ({q}) AND coalesce(year_min, 0) <= ? AND coalesce(year_max, year_min, 9999) >= ?)")
    if has_table(c, "knowledge_vec"):
        c.execute(f"DELETE FROM knowledge_vec WHERE id IN ({kdel})", names + [y1, y0])
    c.execute(f"DELETE FROM knowledge WHERE id IN ({kdel})", names + [y1, y0])
    if has_table(c, "nhtsa_miles"):
        c.execute("DELETE FROM nhtsa_miles WHERE ref NOT IN (SELECT ref FROM knowledge WHERE kind='nhtsa_complaint')")
    cdel = f"SELECT rowid FROM dtc_card WHERE NOT (upper(model) IN ({q}) AND year_min <= ? AND year_max >= ?)"
    if has_table(c, "card_vec"):
        c.execute(f"DELETE FROM card_vec WHERE id IN ({cdel})", names + [y1, y0])
    c.execute(f"DELETE FROM dtc_card WHERE rowid IN ({cdel})", names + [y1, y0])
    c.execute("DELETE FROM spec_fts")
    c.execute("INSERT INTO spec_fts(rowid, component, context) SELECT p.id, p.component, "
              "coalesce(s.topic,'') || ' ' || s.title FROM spec p JOIN section s ON s.id=p.section_id")
    for t in ("section_fts", "knowledge_fts", "dtc_card_fts"):
        c.execute(f"INSERT INTO {t}({t}) VALUES ('rebuild')")
    c.execute("INSERT OR REPLACE INTO meta VALUES ('one_car', ?)",
              (json.dumps({"model": a.model, "year": int(a.year), "window": a.window}),))
    c.commit()
    after = {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    print("trim:", ", ".join(f"{t} {before[t]} -> {after[t]}" for t in before))


# ------------------------------------------------------------------ reuse vectors (optional)
def cmd_reuse_vectors(a):
    c = open_rw(a.db)
    if not load_vec(c):
        sys.exit("reuse-vectors needs sqlite-vec")
    c.execute("ATTACH DATABASE ? AS s", (ro(a.src),))
    em = c.execute("SELECT v FROM s.meta WHERE k='embed_model'").fetchone()
    tx = c.execute("SELECT v FROM s.meta WHERE k='embed_text'").fetchone()
    if not em or (tx and tx[0] != "plain:2000"):
        sys.exit(f"reuse-vectors: {a.src} vectors were made differently ({em}, {tx}); run embed instead")
    pairs = {
        "section_vec": "SELECT ns.id, ss.id FROM main.section ns JOIN main.doc nd ON nd.id=ns.doc_id "
                       "JOIN s.section ss ON ss.body_hash=ns.body_hash AND ss.path=ns.path "
                       "JOIN s.doc sd ON sd.id=ss.doc_id AND sd.kind=nd.kind",
        "knowledge_vec": "SELECT n.id, k.id FROM main.knowledge n JOIN s.knowledge k ON k.kind=n.kind AND "
                         "k.ref IS n.ref AND k.model IS n.model AND k.year_min IS n.year_min AND k.title=n.title AND k.body=n.body",
        "card_vec": "SELECT n.rowid, k.rowid FROM main.dtc_card n JOIN s.dtc_card k ON k.code=n.code AND "
                    "k.model=n.model AND k.year_min=n.year_min AND k.year_max=n.year_max AND k.text=n.text AND k.title=n.title",
    }
    for vec, sql in pairs.items():
        if not has_table(c, vec.replace("_vec", "") if vec != "card_vec" else "dtc_card"):
            continue
        if not c.execute("SELECT 1 FROM s.sqlite_master WHERE name=?", (vec,)).fetchone():
            continue
        if not c.execute("SELECT 1 FROM main.sqlite_master WHERE name=?", (vec,)).fetchone():
            c.execute(f"CREATE VIRTUAL TABLE {vec} USING vec0(id INTEGER PRIMARY KEY, embedding int8[1024])")
        done = 0
        for new_id, old_id in c.execute(sql).fetchall():
            v = c.execute(f"SELECT embedding FROM s.{vec} WHERE id=?", (old_id,)).fetchone()
            if v:
                c.execute(f"INSERT OR IGNORE INTO main.{vec}(id, embedding) VALUES (?, vec_int8(?))", (new_id, v[0]))
                done += 1
        print(f"reuse-vectors: {vec} {done} copied")
    c.execute("INSERT OR REPLACE INTO meta SELECT k, v FROM s.meta WHERE k IN ('embed_model','embed_text')")
    c.commit()


# ------------------------------------------------------------------ pictures inside the DB
PICTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS picture (key TEXT PRIMARY KEY, data BLOB NOT NULL, fmt TEXT, width INTEGER, height INTEGER);
CREATE TABLE IF NOT EXISTS picture_read (key TEXT PRIMARY KEY, state TEXT NOT NULL, text TEXT, unverified TEXT,
  checked INTEGER, model TEXT);
"""


def fsm_picture_key(doc_id, name):
    """Key of a factory-manual figure: the same doc id/stem the app uses for images/<doc id>/<stem>.webp."""
    return f"fsm/{doc_id}/{os.path.splitext(name)[0]}"


def _webp(data, max_px, quality, gray):
    import io
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("L" if gray or im.mode in ("L", "1") else "RGB")
        im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=quality, method=6)
        return buf.getvalue(), im.width, im.height


def cmd_images(a):
    if importlib.util.find_spec("PIL") is None:
        sys.exit("images needs Pillow (pip install Pillow)")
    c = open_rw(a.db)
    c.executescript(PICTURE_SCHEMA)
    n = {"fsm": 0, "fsm missing": 0, "charm": 0, "charm missing": 0, "readings": 0}
    size = 0
    # factory-manual figures: <fsm-dir>/<doc title>/<name>, as the PDF converter wrote them
    fsm_dirs = [os.path.expanduser(d) for d in (a.fsm_dir or [])]
    for doc_id, title in c.execute("SELECT id, title FROM doc WHERE kind='fsm'").fetchall():
        names = set()
        for (imgs,) in c.execute("SELECT images FROM section WHERE doc_id=? AND images IS NOT NULL", (doc_id,)):
            names.update(x for x in json.loads(imgs) if not x.startswith("/images/"))
        for name in sorted(names):
            key = fsm_picture_key(doc_id, name)
            if c.execute("SELECT 1 FROM picture WHERE key=?", (key,)).fetchone():
                continue
            src = next((p for p in (os.path.join(d, title, name) for d in fsm_dirs) if os.path.isfile(p)),
                       os.path.join(fsm_dirs[0] if fsm_dirs else "", title, name))
            if not os.path.isfile(src):
                n["fsm missing"] += 1
                continue
            try:
                with open(src, "rb") as f:
                    data, w, h = _webp(f.read(), a.max_px, a.quality, gray=True)   # line art: gray is enough
            except Exception as e:
                print(f"  skipped {src}: {e}", file=sys.stderr)
                continue
            c.execute("INSERT INTO picture VALUES (?,?,?,?,?)", (key, data, "webp", w, h))
            n["fsm"] += 1
            size += len(data)
        c.commit()
    # CHARM page pictures: section.images keys ("/images/..."), bytes from the images LMDB
    keys = set()
    for (imgs,) in c.execute("SELECT images FROM section WHERE images LIKE '%/images/%'"):
        keys.update(x for x in json.loads(imgs) if x.startswith("/images/"))
    all_keys = set(keys)
    keys = {k for k in keys if not c.execute("SELECT 1 FROM picture WHERE key=?", (k,)).fetchone()}  # stored already
    lmdb_dir = os.path.expanduser(a.lmdb) if a.lmdb else ""
    if keys and lmdb_dir and os.path.isfile(os.path.join(lmdb_dir, "data.mdb")):
        import io
        import lmdb
        from PIL import Image
        env = lmdb.open(lmdb_dir, readonly=True, lock=False, readahead=False, subdir=True)
        with env.begin(buffers=False) as txn:
            for key in sorted(keys):
                if c.execute("SELECT 1 FROM picture WHERE key=?", (key,)).fetchone():
                    continue
                data = txn.get(key[len("/images"):].encode("utf-8"))
                if data is None:
                    n["charm missing"] += 1
                    continue
                fmt, w, h = "", None, None
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        fmt, w, h = (im.format or "").lower(), im.width, im.height
                    if w > a.charm_max_px or len(data) > a.charm_max_bytes:   # wiring diagrams keep their colour
                        data, w, h = _webp(data, a.charm_max_px, 82, gray=False)
                        fmt = "webp"
                except Exception:
                    pass                                                       # stored as it came
                c.execute("INSERT INTO picture VALUES (?,?,?,?,?)", (key, data, fmt, w, h))
                n["charm"] += 1
                size += len(data)
                if n["charm"] % 500 == 0:
                    c.commit()
                    print(f"  {n['charm']} CHARM pictures", flush=True)
        env.close()
    elif keys:
        print(f"images: {len(keys)} CHARM pictures listed, but no CHARM images LMDB at '{lmdb_dir}' (--lmdb); "
              "the app will fetch them from a picture server instead, if one is set")
    # chart readings the PC's vision model already made (optional)
    reads = os.path.expanduser(a.reads_db)
    keys = all_keys
    if keys and os.path.isfile(reads):
        c.execute("ATTACH DATABASE ? AS r", (ro(reads),))
        c.execute("CREATE TEMP TABLE k (key TEXT PRIMARY KEY)")
        c.executemany("INSERT OR IGNORE INTO k VALUES (?)", [(x[len("/images"):],) for x in keys])
        c.execute("INSERT OR REPLACE INTO picture_read SELECT '/images' || r.img_key, r.state, r.text, r.unverified, "
                  "r.checked, r.model FROM r.reading r JOIN k ON k.key=r.img_key WHERE r.state IN ('done','skipped')")
        n["readings"] = c.execute("SELECT count(*) FROM picture_read").fetchone()[0]
    c.execute("INSERT OR REPLACE INTO meta VALUES ('pictures', ?)", (json.dumps(n),))
    c.commit()
    total = c.execute("SELECT coalesce(sum(length(data)),0) FROM picture").fetchone()[0]
    print("images:", ", ".join(f"{k} {v}" for k, v in n.items()), f"| added {size / 1e6:.0f} MB, "
          f"{total / 1e6:.0f} MB of pictures in the DB")



# ------------------------------------------------------------------ HTML manual folder (LEMON download, CHARM-style pages)
def _html_root(path, scratch):
    """A .zip is extracted into scratch; returns the folder that holds pages/ (or the HTML files)."""
    import zipfile
    path = os.path.abspath(os.path.expanduser(path))
    if path.lower().endswith(".zip"):
        dest = os.path.join(scratch, os.path.splitext(os.path.basename(path))[0])
        if not os.path.isdir(dest):
            with zipfile.ZipFile(path) as z:
                for m in z.namelist():                     # no absolute or ../ paths out of dest
                    if m.startswith("/") or ".." in m.split("/"):
                        sys.exit(f"html-manual: unsafe path in zip: {m}")
                z.extractall(dest)
        path = dest
    for root, dirs, files in os.walk(path):
        if "pages" in dirs and any(f.endswith(".html") for f in os.listdir(os.path.join(root, "pages"))):
            return root
    return path


def cmd_html_manual(a):
    import glob
    import hashlib
    import urllib.parse
    from bs4 import BeautifulSoup
    b = importlib.import_module("build_subaru_kb")
    t0 = time.time()
    root = _html_root(a.path, a.scratch or os.path.join(os.path.dirname(os.path.abspath(a.db)), "html_src"))
    files = sorted(glob.glob(os.path.join(root, "pages", "*.html"))) or sorted(
        f for f in glob.glob(os.path.join(root, "**", "*.html"), recursive=True))
    if not files:
        sys.exit(f"html-manual: no HTML pages under {root}")
    c = open_rw(a.db)
    c.executescript(PICTURE_SCHEMA)
    title = a.title or os.path.basename(root.rstrip("/"))
    if c.execute("SELECT 1 FROM doc WHERE title=?", (title,)).fetchone():
        sys.exit(f"html-manual: '{title}' is already in {a.db}")
    doc_id = c.execute("INSERT INTO doc(kind,title,file_name,license) VALUES (?,?,?,?)",
                       (a.kind, title, os.path.basename(a.path.rstrip("/")), a.license)).lastrowid
    st = dict(files=len(files), listing=0, empty=0, image_only=0, pages=0, new=0, specs=0, dtc=0, pictures=0,
              pictures_missing=0, picture_mb=0.0)
    vids, by_hash, pics = {}, {}, {}

    def vehicle(engine):
        if engine not in vids:
            row = c.execute("SELECT id FROM vehicle WHERE model=? AND year=? AND engine IS ?",
                            (a.model, a.year, engine)).fetchone()
            vids[engine] = row[0] if row else c.execute(
                "INSERT INTO vehicle(model,chassis,year,engine,market) VALUES (?,?,?,?,?)",
                (a.model, a.chassis or None, a.year, engine, a.market)).lastrowid
        return vids[engine]

    for fp in files:
        raw = open(fp, encoding="utf-8", errors="replace").read()
        if "id='expand-all'" in raw[:4000] or 'id="expand-all"' in raw[:4000]:
            st["listing"] += 1                              # a list of links, no content
            continue
        soup = BeautifulSoup(raw, "lxml")
        crumbs = [x.get_text(" ", strip=True) for x in soup.select(".breadcrumb-part")]
        model_full = None
        if len(crumbs) >= 4 and crumbs[2] == str(a.year):   # Home > Make > Year > Model ... (LEMON/CHARM layout)
            model_full, crumbs = crumbs[3], crumbs[4:]
        elif not crumbs:
            h1 = soup.find("h1")
            crumbs = [h1.get_text(" ", strip=True) if h1 else os.path.splitext(os.path.basename(fp))[0]]
        for t in soup.select(".header, .footer, .theme-colors"):
            t.decompose()
        main = soup.select_one(".main") or soup
        for h in main.find_all("h1"):
            h.decompose()
        imgs = []
        for i in main.find_all("img", src=True):
            src = i["src"]
            if "/icons/" in src or src.endswith(".svg"):
                continue
            rel = os.path.normpath(os.path.join(os.path.dirname(fp), urllib.parse.unquote(src)))
            key = "/images/" + os.path.splitext(os.path.relpath(rel, os.path.join(root, "images")))[0] + "/"
            imgs.append(key)
            pics.setdefault(key, rel)
        imgs = imgs[:20]
        body, rows = b.charm_text(main)
        if len(body) < 25 and not any(len(r) >= 2 for r in rows):
            st["image_only" if imgs else "empty"] += 1
            continue
        st["pages"] += 1
        engine = None
        if model_full:
            engine = b.charm_vehicle(model_full)[1]
        vid = vehicle(engine)
        section = next((x for x in reversed(crumbs) if x.lower() in ("labor times", "parts", "specifications",
                                                                      "parts information")), "")
        ptitle = crumbs[-1] if crumbs else title
        path = " > ".join(crumbs)
        head = " > ".join(crumbs[-2:])
        for n, part in enumerate(b.charm_parts(body), 1):
            text = f"{head}\n{part}" if n == 1 else f"{head} (continued, part {n})\n{part}"
            h = hashlib.blake2b(text.encode(), digest_size=8).digest()
            sid = by_hash.get(h)
            if sid is None:
                sid = c.execute("INSERT INTO section(doc_id,chapter,topic,fsm_code,title,path,part,page,body,images,"
                                "body_hash) VALUES (?,?,?,NULL,?,?,?,NULL,?,?,?)",
                                (doc_id, crumbs[0] if crumbs else None, crumbs[-2] if len(crumbs) > 1 else None,
                                 ptitle, path, n, text, json.dumps(imgs) if imgs and n == 1 else None, h)).lastrowid
                by_hash[h] = sid
                st["new"] += 1
                if n == 1:
                    st["specs"] += b.charm_specs_labor(c, sid, section, rows, st)
            c.execute("INSERT OR IGNORE INTO section_vehicle VALUES (?,?)", (vid, sid))
            m = b.CHARM_DTC_RE.match(ptitle)
            if m and n == 1:
                c.execute("INSERT OR IGNORE INTO dtc(code) VALUES (?)", (m.group(1),))
                c.execute("INSERT OR IGNORE INTO dtc_procedure VALUES (?,?,?)", (m.group(1), vid, sid))
                st["dtc"] += 1
    c.commit()
    st["labor"] = b.labor_from_sections(c)
    c.execute("DELETE FROM spec_fts")
    c.execute("INSERT INTO spec_fts(rowid, component, context) SELECT p.id, p.component, "
              "coalesce(s.topic,'') || ' ' || s.title FROM spec p JOIN section s ON s.id=p.section_id")
    c.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
    if not a.no_images:
        for key, fp in sorted(pics.items()):
            if c.execute("SELECT 1 FROM picture WHERE key=?", (key,)).fetchone():
                continue
            if not os.path.isfile(fp):
                st["pictures_missing"] += 1
                continue
            data = open(fp, "rb").read()
            fmt, w, h = os.path.splitext(fp)[1].lstrip(".").lower(), None, None
            try:
                import io
                from PIL import Image
                with Image.open(io.BytesIO(data)) as im:
                    w, h = im.width, im.height
                if w > a.max_px or len(data) > a.max_bytes:
                    data, w, h = _webp(data, a.max_px, 82, gray=False)
                    fmt = "webp"
            except Exception:
                pass
            c.execute("INSERT INTO picture VALUES (?,?,?,?,?)", (key, data, fmt, w, h))
            st["pictures"] += 1
            st["picture_mb"] += len(data) / 1e6
    st["picture_mb"] = round(st["picture_mb"], 1)
    st["seconds"] = round(time.time() - t0, 1)
    c.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (f"html_manual:{title}", json.dumps(st)))
    c.commit()
    print("html-manual:", ", ".join(f"{k} {v}" for k, v in st.items()))


# ------------------------------------------------------------------ check
QUERIES = ("P0301", "P0420", "coolant capacity", "front brake caliper bolt torque", "spark plug replacement",
           "engine oil capacity")


def cmd_check(a):
    new = sqlite3.connect(ro(a.db), uri=True)
    src = sqlite3.connect(ro(a.src), uri=True) if a.src and os.path.isfile(a.src) else None
    print(f"check: {a.db} {os.path.getsize(a.db) / 1e6:.1f} MB")

    def counts(c):
        v = car_vehicle_ids(c, a.model, a.year)
        q = ",".join("?" * len(v)) or "NULL"
        names = model_names(a.model)
        nq = ",".join("?" * len(names))
        out = {
            "vehicles": len(v),
            "sections (car)": c.execute(f"SELECT count(DISTINCT section_id) FROM section_vehicle WHERE vehicle_id IN ({q})", v).fetchone()[0],
            "specs (car)": c.execute(f"SELECT count(*) FROM spec WHERE section_id IN (SELECT section_id FROM section_vehicle "
                                     f"WHERE vehicle_id IN ({q}))", v).fetchone()[0],
            "labor (car)": c.execute(f"SELECT count(*) FROM labor WHERE section_id IN (SELECT section_id FROM section_vehicle "
                                     f"WHERE vehicle_id IN ({q}))", v).fetchone()[0],
            "dtc_procedure (car)": c.execute(f"SELECT count(*) FROM dtc_procedure WHERE vehicle_id IN ({q})", v).fetchone()[0],
            "dtc": c.execute("SELECT count(*) FROM dtc").fetchone()[0],
            "dtc_card (car)": c.execute(f"SELECT count(*) FROM dtc_card WHERE upper(model) IN ({nq}) AND ? BETWEEN year_min "
                                        f"AND year_max", names + [int(a.year)]).fetchone()[0],
        }
        if has_table(c, "ssm_param"):
            out["ssm_param"] = c.execute("SELECT count(*) FROM ssm_param").fetchone()[0]
        for kind, n in c.execute(f"SELECT kind, count(*) FROM knowledge WHERE (upper(model) IN ({nq}) AND ? BETWEEN "
                                 f"coalesce(year_min,0) AND coalesce(year_max,year_min,9999)) OR model IS NULL OR model='' "
                                 f"GROUP BY kind", names + [int(a.year)]):
            out[f"knowledge {kind} (car + make-wide)"] = n
        for t in ("comm_thread", "telem_zone", "telem_path", "telem_capability"):
            if has_table(c, t):
                if t == "comm_thread":
                    y = int(a.year)
                    out[t + " (car)"] = c.execute(f"SELECT count(*) FROM comm_thread WHERE upper(model) IN ({nq}) AND "
                                                  f"(year IS NULL OR year BETWEEN ? AND ?)", names + [y - a.window, y + a.window]).fetchone()[0]
                else:
                    out[t + " (car)"] = c.execute(f"SELECT count(*) FROM {t} WHERE (model IS NULL OR upper(model) IN ({nq})) "
                                                  f"AND ? BETWEEN coalesce(year_min,0) AND coalesce(year_max,9999)",
                                                  names + [int(a.year)]).fetchone()[0]
        for vec in ("section_vec", "knowledge_vec", "card_vec", "comm_doc_vec", "telem_path_vec"):
            if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (vec + "_rowids",)).fetchone():
                out[vec + " (all rows in DB)"] = c.execute(f"SELECT count(*) FROM {vec}_rowids").fetchone()[0]
        return out

    a_new = counts(new)
    a_src = counts(src) if src else {}
    report = {"db": a.db, "mb": round(os.path.getsize(a.db) / 1e6, 1), "counts": a_new, "make_db_counts": a_src,
              "tables": {t: new.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for (t,) in new.execute(
                  "SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%' AND name NOT "
                  "LIKE '%_fts_%' AND name NOT LIKE '%vec_%' AND name NOT LIKE 'sqlite_%'")}, "searches": {}}
    w = max(len(k) for k in a_new)
    print(f"  {'':{w}}  {'new':>8}  {'make DB':>8}")
    for k in a_new:
        print(f"  {k:{w}}  {a_new[k]:>8}  {a_src.get(k, ''):>8}")
    if a.no_search:
        if a.json:
            with open(a.json, "w") as f:
                json.dump(report, f, indent=1)
        return
    # retriever: open the DB under the make's file name (a symlink dir), as the app does
    app_db = a.app_db or a.db
    try:
        from kb_retriever import Retriever
        emb = None
        if not a.keywords_only:
            from embedder import QueryEmbedder
            emb = QueryEmbedder(device="cpu") if "device" in QueryEmbedder.__init__.__code__.co_varnames else QueryEmbedder()
        t0 = time.time()
        r = Retriever(app_db, model=a.model, year=a.year, embed=emb)
        print(f"  retriever opened {app_db} in {time.time() - t0:.1f} s (vectors {'on' if r.embed else 'off'})")
        for qtext in (a.queries.split("|") if a.queries else QUERIES):
            t0 = time.time()
            ids = r.search_ids(qtext, k=3)
            paths = [p for (p,) in (new.execute("SELECT path FROM section WHERE id=?", (i,)).fetchone() or ("?",)
                                    for i in ids)]
            print(f"  [{time.time() - t0:4.1f}s] {qtext!r}:")
            for p in paths:
                print(f"      - {p[:110]}")
            report["searches"][qtext] = {"seconds": round(time.time() - t0, 2), "paths": paths}
    except Exception as e:
        print(f"  retriever FAILED: {type(e).__name__}: {e}")
        report["retriever_error"] = f"{type(e).__name__}: {e}"
    if a.json:
        with open(a.json, "w") as f:
            json.dump(report, f, indent=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extras", "copy-docs", "html-manual", "community", "community-select", "community-load", "telemetry", "trim",
                 "reuse-vectors", "images", "check"):
        p = sub.add_parser(name)
        if name == "community-select":
            p.add_argument("--work-dir", required=True, help="new folder for this car's extraction work DB")
            p.add_argument("--make", required=True, help="make DB name build_community.py uses (e.g. the kb/<make>.db name)")
            p.add_argument("--model", required=True)
            p.add_argument("--year", type=int, required=True)
            p.add_argument("--window", type=int, default=2, help="years either side of --year (threads with no year stay)")
            p.add_argument("--years", help="explicit year range A-B instead of --window (e.g. the car's generation)")
            p.add_argument("--min-replies", type=int, default=0)
            p.add_argument("--any-of", default="", help="pass-0 flags, e.g. test,outcome,code")
            p.add_argument("--budget-hours", type=float, default=0, help="time budget for the extraction (0: no cap)")
            p.add_argument("--rate", type=float, default=0, help="threads/hour the extraction runs at on this machine")
            p.add_argument("--seed", type=int, default=20261112)
            p.add_argument("--reprefilter", action="store_true", help="re-run pass 0 even if _prefilter has threads")
            p.add_argument("--force", action="store_true")
            p.add_argument("--log")
            continue
        p.add_argument("--db", required=True, help="the one-car DB being built")
        if name == "html-manual":
            p.add_argument("--path", required=True, help="the .zip or the extracted folder")
            p.add_argument("--model", required=True)
            p.add_argument("--year", type=int, required=True)
            p.add_argument("--chassis")
            p.add_argument("--market", default="USDM")
            p.add_argument("--title", help="doc title (default: the folder name)")
            p.add_argument("--kind", default="charm", help="doc kind for the retriever (CHARM-style pages: charm)")
            p.add_argument("--license", default="as downloaded; personal use")
            p.add_argument("--scratch", help="where a zip is extracted (default: next to --db)")
            p.add_argument("--no-images", action="store_true")
            p.add_argument("--max-px", type=int, default=1600)
            p.add_argument("--max-bytes", type=int, default=400_000)
            continue
        if name == "community-load":
            p.add_argument("--work-dir", required=True)
            p.add_argument("--make", required=True)
            p.add_argument("--model", required=True)
            p.add_argument("--year", type=int, required=True)
            p.add_argument("--window", type=int, default=2)
            p.add_argument("--years", help="explicit year range A-B instead of --window")
            p.add_argument("--device", default="auto", help="vectors: auto|cuda|cpu|none")
            p.add_argument("--log")
            continue
        if name == "images":
            p.add_argument("--fsm-dir", action="append",
                           help="folder holding one folder per converted manual (named after the manual's title); "
                                "repeat for several (default test_batch_out)")
            p.add_argument("--lmdb", default=os.environ.get("CHARM_IMAGES", ""),
                           help="the CHARM images LMDB folder (holds data.mdb); default $CHARM_IMAGES")
            p.add_argument("--reads-db", default=os.path.join(HERE, "charm_reads.db"))
            p.add_argument("--max-px", type=int, default=1000, help="manual figures: longest side")
            p.add_argument("--quality", type=int, default=60)
            p.add_argument("--charm-max-px", type=int, default=1600, help="CHARM pictures wider than this get shrunk")
            p.add_argument("--charm-max-bytes", type=int, default=400_000)
        elif name != "extras":
            p.add_argument("--model")
            p.add_argument("--year", type=int)
            p.add_argument("--src", help="the make DB to copy from (opened read-only)")
            p.add_argument("--window", type=int, default=2 if name == "community" else 0,
                           help="years either side of --year to keep (community default 2, others 0)")
        if name == "community":
            p.add_argument("--years", help="explicit year range A-B instead of --window")
        if name == "copy-docs":
            p.add_argument("--kinds", default="charm", help="doc kinds to copy, e.g. charm or charm,fsm")
        if name == "check":
            p.add_argument("--app-db", help="the same DB under its make's file name (for the retriever's make rules)")
            p.add_argument("--keywords-only", action="store_true")
            p.add_argument("--no-search", action="store_true")
            p.add_argument("--queries", help="search checks, separated by |")
            p.add_argument("--json", help="also write counts, table sizes and search results here")
    a = ap.parse_args()
    if a.cmd == "images" and not a.fsm_dir:
        a.fsm_dir = [os.path.join(os.path.dirname(HERE), "test_batch_out")]
    {"html-manual": cmd_html_manual, "community-select": cmd_community_select, "community-load": cmd_community_load, "extras": cmd_extras, "copy-docs": cmd_copy_docs, "community": cmd_community, "telemetry": cmd_telemetry,
     "trim": cmd_trim, "reuse-vectors": cmd_reuse_vectors, "images": cmd_images, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    main()
