#!/usr/bin/env python3
"""
Embed step benchmark and retrieval checks (desktop). Read-only on the make DBs; writes only to copies and logs/embed_bench/.

  speed <db>            5,000 random section+knowledge rows (fixed seed), old encode (SentenceTransformer.encode,
                        batch 16) vs DocEncoder: rows/s, peak VRAM, and how far the int8 vectors differ.
  cases <db>            vector-sensitive retrieval cases, all taken from the DB's own rows (fixed seed), saved once:
                          dtcdesc    the DTC's own name without the code  -> that car's DTC procedure page
                          complaint  an NHTSA owner complaint's text      -> that car's TSB/recall on the same component
                          specq      "<component> torque specification"  -> the page holding that spec row
  score <db> [<db>...]  eval_make's saved cases plus these cases on each DB copy (query embeddings on the CPU),
                        and the top-10 lists, so two copies can be compared with `compare`.
  compare <a.json> <b.json>   how many queries have the same top-10 / top-3 / first hit.
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(os.path.dirname(HERE), "logs", "embed_bench")
SEED = 20261026
N = {"dtcdesc": 60, "complaint": 40, "specq": 40}


def ro(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def make_of(db):
    return os.path.splitext(os.path.basename(db))[0].split("__")[0]


# ------------------------------------------------------------------ speed
def speed(a):
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    from embedder import DocEncoder, to_int8
    c = ro(a.db)
    rng = random.Random(SEED)
    ns = c.execute("SELECT max(id) FROM section").fetchone()[0]
    nk = c.execute("SELECT max(id) FROM knowledge").fetchone()[0] or 0
    want_s = rng.sample(range(1, ns + 1), min(ns, a.n * 9 // 10 * 2))
    want_k = rng.sample(range(1, nk + 1), min(nk, a.n // 10 * 2)) if nk else []
    q = lambda sql, ids: c.execute(sql + f" WHERE id IN ({','.join(map(str, ids))})").fetchall()
    rows = q("SELECT id, path || char(10) || substr(body, 1, 2000) FROM section", want_s)[:a.n * 9 // 10]
    rows += q("SELECT -id, title || char(10) || substr(body, 1, 2000) FROM knowledge", want_k)[:a.n - len(rows)]
    texts = [t for _, t in rows]
    print(f"speed: {len(rows)} rows, mean {sum(map(len, texts)) / len(texts):.0f} chars", flush=True)
    res = {}

    import subprocess
    import threading

    def others():
        """Other GPU compute jobs (Ollama, another embed) right now: their runs are marked, not trusted."""
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout
        return {l.split(",")[0] for l in out.splitlines() if l.strip() and int(l.split(",")[1]) > 400
                and l.split(",")[0].strip() != str(os.getpid())}

    def run(name, fn):
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        seen, stop = set(), threading.Event()

        def watch():
            while not stop.wait(2):
                seen.update(others())
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        t0 = time.time()
        v = fn()
        torch.cuda.synchronize()
        dt = time.time() - t0
        stop.set()
        w.join()
        res[name] = {"rows_s": len(rows) / dt, "s": dt, "peak_mib": torch.cuda.max_memory_allocated() / 2**20,
                     "shared_gpu": sorted(seen)}
        print(f"  {name}: {len(rows) / dt:.0f} rows/s, {dt:.1f} s, peak VRAM {res[name]['peak_mib']:.0f} MiB"
              + (f"  [GPU SHARED with pid {','.join(sorted(seen))}: not a clean number]" if seen else ""), flush=True)
        return v

    # before: the old step_embed's call, as it was (default dtype, batch 16, chunks of 4096)
    old = SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device="cuda")
    print(f"  old model dtype {old[0].auto_model.dtype}", flush=True)
    old.encode(texts[:64], batch_size=16)       # warm-up (CUDA context, kernels) outside the timing

    def old_encode(bs=16):
        out = [old.encode(texts[i:i + 4096], batch_size=bs, convert_to_numpy=True) for i in range(0, len(texts), 4096)]  # noqa: F821 (old is deleted only after run() returns)
        return to_int8(np.concatenate(out))
    v_old = run("old  encode bs16", old_encode)
    ref32 = None
    if a.fp32_ref:
        old.to(dtype=torch.float32)
        ref32 = run("ref  encode fp32 bs4", lambda: old_encode(4))
    del old
    torch.cuda.empty_cache()
    out = {}
    for dtype in a.dtypes.split(","):
        for budget in map(int, a.budgets.split(",")):
            enc = DocEncoder(device="cuda", dtype=dtype, token_budget=budget, log=lambda *x, **k: None)
            enc.encode(texts[:64])
            name = f"new  {dtype} {budget} tok/batch"
            out[name] = run(name, lambda: enc.encode(texts))  # noqa: F821 (called inside run(), before del enc)
            del enc
            torch.cuda.empty_cache()
    for name, v in out.items():
        for ref_name, ref in (("old", v_old), ("fp32", ref32)):
            if ref is None:
                continue
            d = np.abs(v.astype(int) - ref.astype(int))
            cos = (v.astype(np.float32) * ref).sum(1) / (np.linalg.norm(v, axis=1) * np.linalg.norm(ref, axis=1))
            res[name][f"vs_{ref_name}"] = {"exact_rows": float((d.max(1) == 0).mean()), "max_diff": int(d.max()),
                                          "mean_abs": float(d.mean()), "min_cos": float(cos.min()),
                                          "mean_cos": float(cos.mean())}
            print(f"  {name} vs {ref_name}: rows identical {res[name][f'vs_{ref_name}']['exact_rows']:.1%}, "
                  f"max int8 diff {d.max()}, mean |diff| {d.mean():.3f}, cos min {cos.min():.4f} mean {cos.mean():.5f}")
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(os.path.join(OUT, f"speed_{make_of(a.db)}.json"), "w"), indent=1)


# ------------------------------------------------------------------ cases
DTC_NAME = r"\b{code}\b\s*(?:[:\-–]|\(.*?\)\s*[:\-–]?)\s*([A-Za-z][^\n|]{{8,140}})"


def make_cases(db):
    from eval_make import spread
    rng = random.Random(SEED)
    c = ro(db)
    cases = []
    rows = c.execute("SELECT DISTINCT d.code, v.model, v.year, d.section_id FROM dtc_procedure d "
                     "JOIN vehicle v ON v.id=d.vehicle_id ORDER BY 1,2,3,4").fetchall()
    rng.shuffle(rows)
    picked = []
    for code, model, year, sid in rows:
        body = c.execute("SELECT body FROM section WHERE id=?", (sid,)).fetchone()[0]
        m = re.search(DTC_NAME.format(code=re.escape(code)), body)
        if not m:
            continue
        name = m.group(1).strip(" .")
        if len(re.findall(r"[A-Za-z]{3,}", name)) < 2 or re.search(r"\b[PBCU][0-9A-F]{4}\b", name):
            continue
        picked.append((code, model, year, name))
    seen = set()
    for code, model, year, name in spread(picked, N["dtcdesc"] * 3, lambda r: r[1], rng):
        if (code, model, year) in seen:
            continue
        seen.add((code, model, year))
        cases.append({"type": "dtcdesc", "model": model, "year": year, "code": code, "query": name})
        if sum(x["type"] == "dtcdesc" for x in cases) == N["dtcdesc"]:
            break
    rows = c.execute("""
        SELECT k.id, k.model, k.year_min, k.title, k.body FROM knowledge k
        WHERE k.kind='nhtsa_complaint' AND k.title <> '' AND length(k.body) > 80 AND EXISTS (
            SELECT 1 FROM knowledge t WHERE t.kind IN ('tsb','recall') AND upper(t.model)=upper(k.model)
              AND k.year_min BETWEEN coalesce(t.year_min,0) AND coalesce(t.year_max,9999)
              AND instr(upper(t.title), upper(substr(k.title, 1, instr(k.title || ':', ':') - 1))) > 0)
        ORDER BY k.id""").fetchall()
    for kid, model, year, comp, body in spread(rows, N["complaint"], lambda r: r[1], rng):
        top = comp.split(":")[0].strip()
        right = [r[0] for r in c.execute(
            "SELECT id FROM knowledge WHERE kind IN ('tsb','recall') AND upper(model)=upper(?) AND ? BETWEEN "
            "coalesce(year_min,0) AND coalesce(year_max,9999) AND instr(upper(title), upper(?)) > 0",
            (model, year, top))]
        cases.append({"type": "complaint", "model": model, "year": year, "complaint_id": kid, "right_ids": right,
                      "query": " ".join(body.split())[:400]})
    rows = c.execute("SELECT p.id, p.component, p.section_id, v.model, v.year FROM spec p "
                     "JOIN section_vehicle sv ON sv.section_id=p.section_id JOIN vehicle v ON v.id=sv.vehicle_id "
                     "WHERE p.value_si IS NOT NULL AND length(p.component) BETWEEN 6 AND 60 "
                     "AND p.component GLOB '*[a-zA-Z][a-zA-Z][a-zA-Z]*' GROUP BY p.id ORDER BY p.id").fetchall()
    for pid, comp, sid, model, year in spread(rows, N["specq"], lambda r: r[3], rng):
        cases.append({"type": "specq", "model": model, "year": year, "spec_id": pid, "section_id": sid,
                      "query": f"{comp} torque specification"})
    return cases


def cases(a):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{make_of(a.db)}_cases.json")
    if os.path.isfile(path) and not a.new:
        sys.exit(f"{path} exists (use --new to replace it)")
    cs = make_cases(a.db)
    json.dump(cs, open(path, "w"), indent=1)
    print(f"{len(cs)} cases -> {path}: " + ", ".join(f"{t} {sum(c['type'] == t for c in cs)}" for t in N))


# ------------------------------------------------------------------ score
def score_db(db, embed):
    import eval_make
    from kb_retriever import Retriever
    make = make_of(db)
    base = json.load(open(os.path.join(os.path.dirname(HERE), "logs", "eval_make", f"{make}.json")))["cases"]
    res = [dict(r, set="eval_make") for r in eval_make.score(db, [dict(x) for x in base], embed)]
    c = ro(db)
    for case in json.load(open(os.path.join(OUT, f"{make}_cases.json"))):
        r = Retriever(db, model=case["model"], year=case["year"], embed=embed)
        if case["type"] == "dtcdesc":
            right = {s for (s,) in c.execute(
                "SELECT d.section_id FROM dtc_procedure d JOIN vehicle v ON v.id=d.vehicle_id "
                "WHERE d.code=? AND v.model=? AND v.year=?", (case["code"], case["model"], case["year"]))}
            ids = r.search_ids(case["query"], k=10)
        elif case["type"] == "specq":
            right = {case["section_id"]}
            ids = r.search_ids(case["query"], k=10)
        else:
            right = set(case["right_ids"])
            ids = r.knowledge_ids(case["query"], k=10, kinds=("tsb", "recall"))
        rank = next((i for i, x in enumerate(ids, 1) if x in right), None)
        res.append(dict(case, set="embed_bench", rank=rank, ids=ids))
    return res


def table(res):
    out = {}
    for t in sorted({r["type"] for r in res}, key=["dtc", "spec", "problem", "dtcdesc", "complaint", "specq"].index):
        rs = [r for r in res if r["type"] == t]
        out[t] = (len(rs), sum(1 for r in rs if r["rank"] and r["rank"] <= 3) / len(rs),
                  sum(1 / r["rank"] for r in rs if r["rank"]) / len(rs))
    return out


def score(a):
    from embedder import QueryEmbedder
    embed = QueryEmbedder("Qwen/Qwen3-Embedding-0.6B", device="cpu", threads=a.threads)
    os.makedirs(OUT, exist_ok=True)
    for db in a.dbs:
        t0 = time.time()
        res = score_db(os.path.abspath(db), embed)
        name = os.path.splitext(os.path.basename(db))[0]
        json.dump(res, open(os.path.join(OUT, f"score_{name}.json"), "w"), indent=1)
        print(f"{name} ({time.time() - t0:.0f} s): " +
              "  ".join(f"{t} n={n} top3 {t3:.0%} MRR {m:.2f}" for t, (n, t3, m) in table(res).items()), flush=True)


def compare(a):
    x, y = (json.load(open(p)) for p in (a.a, a.b))
    same10 = same3 = same1 = n = 0
    for r, s in zip(x, y):
        if "ids" not in r or "ids" not in s:
            continue
        n += 1
        same10 += r["ids"] == s["ids"]
        same3 += r["ids"][:3] == s["ids"][:3]
        same1 += r["ids"][:1] == s["ids"][:1]
    print(f"{n} queries: same top-10 {same10}, same top-3 {same3}, same first hit {same1}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("speed")
    p.add_argument("db")
    p.add_argument("-n", type=int, default=5000)
    p.add_argument("--dtypes", default="bfloat16")
    p.add_argument("--budgets", default="65536")
    p.add_argument("--fp32-ref", action="store_true")
    p = sub.add_parser("cases")
    p.add_argument("db")
    p.add_argument("--new", action="store_true")
    p = sub.add_parser("score")
    p.add_argument("dbs", nargs="+")
    p.add_argument("--threads", type=int, default=8)
    p = sub.add_parser("compare")
    p.add_argument("a")
    p.add_argument("b")
    a = ap.parse_args()
    {"speed": speed, "cases": cases, "score": score, "compare": compare}[a.cmd](a)


if __name__ == "__main__":
    main()
