#!/usr/bin/env python3
"""
How well a make's knowledge DB answers, scored with the app's own retrieval (kb_retriever.py, read-only).

Every case and its right answer come from the DB's own rows (fixed random seed), never from a model:
  dtc      "<year> <model> <code>"         right = a section dtc_procedure links to that code for that car
  spec     "<component> torque"            right = a spec line with that row's value (N·m or the US unit)
  problem  "<year> <model> <component>"    right = a TSB or recall for that model/year naming that component
             (the case comes from an NHTSA owner complaint, which is itself never counted as right)
Scores per case type: top-3 hit rate and MRR (rank of the first right answer in the top 10), plus whether the
best hit is for the right car (model + year) or a different one.

The cases are saved to logs/eval_make/<make>.json on the first run and reused after that, so the same cases
can be rescored after each layer is added (NHTSA rows, vision-read charts, cards). --new picks fresh ones.

  python3 eval_make.py kb/acura.db kb/subaru.db          # query embeddings on the CPU (the GPU stays free)
  python3 eval_make.py kb/acura.db --no-embed            # keyword search only
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "logs", "eval_make")
SEED = 20261021
N = {"dtc": 10, "spec": 5, "problem": 5}


def ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def spread(rows, n, key, rng):
    """n rows, at most one per key (model) until every key has had a turn."""
    rng.shuffle(rows)
    out, used = [], set()
    for r in rows:
        if key(r) not in used:
            out.append(r)
            used.add(key(r))
        if len(out) == n:
            return out
    for r in rows:
        if r not in out:
            out.append(r)
        if len(out) == n:
            break
    return out


def make_cases(db):
    rng = random.Random(SEED)
    c = ro(db)
    cases = []
    # DTC: one (code, car) pair per model where possible
    rows = c.execute("SELECT DISTINCT d.code, v.model, v.year FROM dtc_procedure d JOIN vehicle v ON v.id=d.vehicle_id "
                     "ORDER BY 1,2,3").fetchall()
    for code, model, year in spread(rows, N["dtc"], lambda r: r[1], rng):
        cases.append({"type": "dtc", "model": model, "year": year, "code": code, "query": f"{year} {model} {code}"})
    # spec: a torque row from a section tied to a car, with a usable part name
    rows = c.execute("SELECT p.id, p.component, p.value_si, p.value_us, p.unit_us, v.model, v.year FROM spec p "
                     "JOIN section_vehicle sv ON sv.section_id=p.section_id JOIN vehicle v ON v.id=sv.vehicle_id "
                     "WHERE p.value_si IS NOT NULL AND length(p.component) BETWEEN 6 AND 60 "
                     "AND p.component GLOB '*[a-zA-Z][a-zA-Z][a-zA-Z]*' GROUP BY p.id ORDER BY p.id").fetchall()
    for sid, comp, nm, us, unit, model, year in spread(rows, N["spec"], lambda r: r[5], rng):
        cases.append({"type": "spec", "model": model, "year": year, "spec_id": sid, "component": comp,
                      "value_si": nm, "value_us": us, "unit_us": unit, "query": f"{comp}"})
    # problem: an owner complaint whose car also has a TSB/recall naming the same component
    rows = c.execute("""
        SELECT k.id, k.model, k.year_min, k.title FROM knowledge k
        WHERE k.kind='nhtsa_complaint' AND k.title <> '' AND EXISTS (
            SELECT 1 FROM knowledge t WHERE t.kind IN ('tsb','recall') AND upper(t.model)=upper(k.model)
              AND k.year_min BETWEEN coalesce(t.year_min,0) AND coalesce(t.year_max,9999)
              AND instr(upper(t.title), upper(substr(k.title, 1, instr(k.title || ':', ':') - 1))) > 0)
        ORDER BY k.id""").fetchall() if has(c, "knowledge") else []
    for kid, model, year, comp in spread(rows, N["problem"], lambda r: r[1], rng):
        top = comp.split(":")[0].strip()
        right = [r[0] for r in c.execute(
            "SELECT id FROM knowledge WHERE kind IN ('tsb','recall') AND upper(model)=upper(?) AND ? BETWEEN "
            "coalesce(year_min,0) AND coalesce(year_max,9999) AND instr(upper(title), upper(?)) > 0",
            (model, year, top))]
        cases.append({"type": "problem", "model": model, "year": year, "complaint_id": kid, "component": top,
                      "right_ids": right, "query": f"{year} {model} {top.lower()}"})
    return cases


def has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def car_sections(c, model, year):
    return {r[0] for r in c.execute("SELECT sv.section_id FROM section_vehicle sv JOIN vehicle v ON v.id=sv.vehicle_id "
                                    "WHERE v.model=? AND v.year=?", (model, year))}


def score(db, cases, embed):
    sys.path.insert(0, HERE)
    from kb_retriever import Retriever
    c = ro(db)
    results = []
    for case in cases:
        r = Retriever(db, model=case["model"], year=case["year"], embed=embed)
        rank, right_car, got = None, None, ""
        if case["type"] == "dtc":
            right = {s for (s,) in c.execute(
                "SELECT d.section_id FROM dtc_procedure d JOIN vehicle v ON v.id=d.vehicle_id "
                "WHERE d.code=? AND v.model=? AND v.year=?", (case["code"], case["model"], case["year"]))}
            ids = r.search_ids(case["query"], k=10)
            mine = car_sections(c, case["model"], case["year"])
            for i, sid in enumerate(ids, 1):
                if sid in right:
                    rank = i
                    break
            if ids:
                right_car = ids[0] in mine
                got = c.execute("SELECT path FROM section WHERE id=?", (ids[0],)).fetchone()[0][:90]
            case["lookup_ok"] = bool(r.dtc(case["code"], chars=0) or "")   # the app's direct code lookup too
        elif case["type"] == "spec":
            lines = r.specs(case["query"], limit=10)
            want = [f"{case['value_si']:g} N·m"] + ([f"{case['value_us']:g} {case['unit_us'] or 'ft-lb'}"]
                                                     if case["value_us"] is not None else [])
            for i, ln in enumerate(lines, 1):
                if any(w in ln for w in want):
                    rank = i
                    break
            right_car = bool(lines)          # specs() only returns this car's rows when the car is known
            got = lines[0][:90] if lines else ""
        else:
            ids = r.knowledge_ids(case["query"], k=10)
            for i, kid in enumerate(ids, 1):
                if kid in case["right_ids"]:
                    rank = i
                    break
            if ids:
                m, y0, y1, kind, title = c.execute("SELECT model, year_min, year_max, kind, title FROM knowledge "
                                                   "WHERE id=?", (ids[0],)).fetchone()
                right_car = m.upper() == case["model"].upper() and (y0 or 0) <= case["year"] <= (y1 or 9999)
                got = f"[{kind}] {title}"[:90]
        results.append(dict(case, rank=rank, right_car=right_car, top=got))
    return results


def summary(make, results):
    rows = []
    for t in ("dtc", "spec", "problem"):
        rs = [r for r in results if r["type"] == t]
        if not rs:
            rows.append((make, t, 0, None, None, None))
            continue
        top3 = sum(1 for r in rs if r["rank"] and r["rank"] <= 3) / len(rs)
        mrr = sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs)
        car = sum(1 for r in rs if r["right_car"]) / len(rs)
        rows.append((make, t, len(rs), top3, mrr, car))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbs", nargs="+")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--new", action="store_true", help="pick new cases instead of reusing the saved ones")
    ap.add_argument("--history", action="store_true", help="also append the scores to logs/eval_make/history.csv")
    ap.add_argument("--label", default="", help="what changed since the last score, for history.csv")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    embed = None
    if not a.no_embed:
        from embedder import QueryEmbedder
        embed = QueryEmbedder("Qwen/Qwen3-Embedding-0.6B", device="cpu", threads=4)
    table = []
    for db in a.dbs:
        db = os.path.abspath(db)
        make = os.path.splitext(os.path.basename(db))[0]
        path = os.path.join(OUT, f"{make}.json")
        if os.path.isfile(path) and not a.new:
            cases = json.load(open(path))["cases"]
        else:
            cases = make_cases(db)
        res = score(db, cases, embed)
        json.dump({"db": db, "seed": SEED, "embed": embed is not None, "cases": cases, "results": res},
                  open(path, "w"), indent=1)
        table += summary(make, res)
        for r in res:
            if not r["rank"] or r["rank"] > 3:
                print(f"  MISS {make} {r['type']}: {r['query']!r} -> rank {r['rank']}; top: {r['top']}")
    if a.history:
        import time
        hist = os.path.join(OUT, "history.csv")
        new = not os.path.isfile(hist)
        with open(hist, "a") as f:
            if new:
                f.write("time,make,case_type,n,top3,mrr,right_car,embed,label\n")
            for make, t, n, top3, mrr, car in table:
                if n:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M')},{make},{t},{n},{top3:.2f},{mrr:.2f},{car:.2f},"
                            f"{int(embed is not None)},{a.label.replace(',', ';')}\n")
    print("\n| make | case type | n | top-3 | MRR | best hit is the right car |\n|---|---|---|---|---|---|")
    for make, t, n, top3, mrr, car in table:
        print(f"| {make} | {t} | {n} | " + (f"{top3:.0%} | {mrr:.2f} | {car:.0%} |" if n else "- | - | - |"))


if __name__ == "__main__":
    main()
