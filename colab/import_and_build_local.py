#!/usr/bin/env python3
"""
Local PC post-processing for Colab-extracted Strategy A makes.

Takes finished `<make>.work.db` and/or `<make>.extract.jsonl` files downloaded from
Google Drive (`MyDrive/Cyberdeck_Community_DB/work_dbs/` or `checkpoints/`), merges them
cleanly into `~/subaru_cyberdeck/kb/community/work_9b_run/`, then runs:
  1. `tables` in parallel across multiple CPU cores on the Ryzen 9 5900X
  2. Adds the missing `comm_cause_th` and `comm_chain_th` indexes (fixes full table scans!)
  3. `embed` on the local RTX 3070 (~5-8 min per big make)
  4. `merge` into `~/subaru_cyberdeck/kb/<make>.db` (automatic backup first)

Usage:
  ~/subaru_cyberdeck/venv/bin/python ~/subaru_cyberdeck/colab/import_and_build_local.py \
      --from-dir ~/Downloads/Cyberdeck_Community_DB --makes chevrolet,toyota,ford --jobs 4
"""
import argparse
import concurrent.futures as cf
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
BUILD = os.path.join(ROOT, "kb/build_community.py")
WORK_DST = os.environ.get("CYBERDECK_WORK_DIR", os.path.join(ROOT, "kb/community/work_9b_run"))
WORK_SRC = os.path.join(ROOT, "kb/community/work")
COMM_DIR = os.path.join(ROOT, "kb/community")
KB_DIR = os.path.join(ROOT, "kb")

STRATEGY_A = [
    "chevrolet", "toyota", "honda", "ford", "nissan", "jeep", "dodge", "hyundai", "kia", "gmc",
    "mazda", "volkswagen", "acura", "lexus", "bmw", "volvo", "audi", "mercedes_benz",
    "chrysler", "buick", "cadillac", "lincoln", "scion", "infiniti", "mitsubishi", "mini",
]

def import_make(make, from_dir):
    """Ensure work_9b_run/<make>.work.db exists and merge any Colab DB + JSONL rows into it."""
    os.makedirs(WORK_DST, exist_ok=True)
    dst = os.path.join(WORK_DST, f"{make}.work.db")

    cand_dbs = [
        os.path.join(from_dir, "work_dbs", f"{make}.work.db"),
        os.path.join(from_dir, f"{make}.work.db"),
    ]
    cand_jsonls = [
        os.path.join(from_dir, "checkpoints", f"{make}.extract.jsonl"),
        os.path.join(from_dir, f"{make}.extract.jsonl"),
    ]
    src_db = next((p for p in cand_dbs if os.path.isfile(p)), None)
    src_jsonl = next((p for p in cand_jsonls if os.path.isfile(p)), None)

    if not src_db and not src_jsonl:
        print(f"[{make}] No Colab output found in {from_dir}, skipping.", flush=True)
        return False

    if not os.path.isfile(dst):
        base = src_db or os.path.join(WORK_SRC, f"{make}.work.db")
        if not os.path.isfile(base):
            print(f"[{make}] Missing base work DB ({base}), skipping.", flush=True)
            return False
        shutil.copy2(base, dst)
        print(f"[{make}] Copied base DB -> {dst}", flush=True)

    con = sqlite3.connect(dst, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    added = 0

    if src_db and os.path.abspath(src_db) != os.path.abspath(dst):
        con.execute("ATTACH ? AS colab", (src_db,))
        before = con.execute("SELECT count(*) FROM extract WHERE status='ok'").fetchone()[0]
        con.execute(
            "INSERT INTO main.extract(src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, tries, at) "
            "SELECT src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, tries, at "
            "FROM colab.extract WHERE status='ok' "
            "ON CONFLICT(src, tid) DO UPDATE SET "
            "status=CASE WHEN main.extract.status='ok' THEN main.extract.status ELSE excluded.status END, "
            "raw=CASE WHEN main.extract.status='ok' THEN main.extract.raw ELSE excluded.raw END, "
            "rec=CASE WHEN main.extract.status='ok' THEN main.extract.rec ELSE excluded.rec END, "
            "checks=CASE WHEN main.extract.status='ok' THEN main.extract.checks ELSE excluded.checks END, "
            "llm_model=excluded.llm_model, ms=excluded.ms, prompt_tokens=excluded.prompt_tokens, "
            "eval_tokens=excluded.eval_tokens, at=excluded.at"
        )
        con.commit()
        after = con.execute("SELECT count(*) FROM extract WHERE status='ok'").fetchone()[0]
        con.execute("DETACH colab")
        added += after - before

    if src_jsonl:
        before = con.execute("SELECT count(*) FROM extract WHERE status='ok'").fetchone()[0]
        with open(src_jsonl, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("status") != "ok":
                    continue
                con.execute(
                    "INSERT INTO extract(src, tid, status, raw, rec, checks, llm_model, ms, prompt_tokens, eval_tokens, at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(src, tid) DO UPDATE SET "
                    "status=CASE WHEN extract.status='ok' THEN extract.status ELSE excluded.status END, "
                    "raw=CASE WHEN extract.status='ok' THEN extract.raw ELSE excluded.raw END, "
                    "rec=CASE WHEN extract.status='ok' THEN extract.rec ELSE excluded.rec END, "
                    "checks=CASE WHEN extract.status='ok' THEN extract.checks ELSE excluded.checks END, "
                    "llm_model=excluded.llm_model, ms=excluded.ms, prompt_tokens=excluded.prompt_tokens, "
                    "eval_tokens=excluded.eval_tokens, at=excluded.at",
                    (r["src"], r["tid"], r["status"], r.get("raw"),
                     json.dumps(r["rec"]) if r.get("rec") is not None else None,
                     json.dumps(r.get("checks") or {}), r.get("llm_model", ""),
                     r.get("ms", 0), r.get("prompt_tokens", 0), r.get("eval_tokens", 0), r.get("at", "")),
                )
        con.commit()
        after = con.execute("SELECT count(*) FROM extract WHERE status='ok'").fetchone()[0]
        added += after - before

    total_ok = con.execute("SELECT count(*) FROM extract WHERE status='ok'").fetchone()[0]
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    print(f"[{make}] Ready in work_9b_run: {total_ok:,} ok rows (+{added:,} imported)", flush=True)
    return total_ok > 0

def ensure_comm_indexes(db_path):
    """Add the two missing thread_id indexes that prevent full table scans in community_ref.community()."""
    if not os.path.isfile(db_path):
        return
    con = sqlite3.connect(db_path, timeout=120)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='comm_cause'").fetchone():
        con.execute("CREATE INDEX IF NOT EXISTS comm_cause_th ON comm_cause(thread_id)")
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='comm_chain'").fetchone():
        con.execute("CREATE INDEX IF NOT EXISTS comm_chain_th ON comm_chain(thread_id)")
    con.execute("ANALYZE")
    con.commit()
    con.close()

def run_tables(make):
    t0 = time.time()
    print(f"[{make}] Starting CPU `tables` step...", flush=True)
    cmd = [PY, BUILD, "--make", make, "--work-dir", WORK_DST, "tables"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[{make}] tables FAILED:\n{r.stderr[-1000:]}", flush=True)
        return make, False
    ensure_comm_indexes(os.path.join(COMM_DIR, f"{make}.community.db"))
    print(f"[{make}] tables + indexes done in {(time.time()-t0)/60:.1f} min", flush=True)
    return make, True

def run_embed_and_merge(make, device="auto"):
    t0 = time.time()
    for step in ("embed", "merge"):
        cmd = [PY, BUILD, "--make", make, "--work-dir", WORK_DST]
        if step == "embed":
            cmd += ["--device", device]
        cmd.append(step)
        print(f"[{make}] Running `{step}`...", flush=True)
        r = subprocess.run(cmd)
        if r.returncode != 0:
            print(f"[{make}] {step} FAILED (rc={r.returncode})", flush=True)
            return False
    ensure_comm_indexes(os.path.join(KB_DIR, f"{make}.db"))
    ann = os.path.join(KB_DIR, "ann_index.py")
    if os.path.isfile(ann):
        print(f"[{make}] Updating 1-bit ANN sidecar (incl. comm_doc_vec)...", flush=True)
        subprocess.run([PY, ann, "build", os.path.join(KB_DIR, f"{make}.db")])
    print(f"[{make}] embed + merge + ANALYZE + ANN done in {(time.time()-t0)/60:.1f} min", flush=True)
    return True

def main():
    ap = argparse.ArgumentParser(description="Import Colab outputs and run local tables/embed/merge")
    ap.add_argument("--from-dir", required=True, help="Local folder with downloaded Colab work_dbs/ and/or checkpoints/")
    ap.add_argument("--makes", default=",".join(STRATEGY_A), help="Comma-separated makes (default: Strategy A)")
    ap.add_argument("--jobs", type=int, default=2, help="Parallel CPU workers for `tables` (default: 2 to keep RAM low)")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--skip-embed", action="store_true")
    ap.add_argument("--skip-merge", action="store_true")
    a = ap.parse_args()

    makes = [m.strip() for m in a.makes.split(",") if m.strip()]
    ready = [m for m in makes if import_make(m, os.path.expanduser(a.from_dir))]
    if not ready:
        sys.exit("No makes had Colab outputs to process.")

    print(f"\n=== Phase 1: Parallel CPU `tables` ({len(ready)} makes, {a.jobs} workers) ===", flush=True)
    built = []
    with cf.ProcessPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        for make, ok in ex.map(run_tables, ready):
            if ok:
                built.append(make)

    if a.skip_embed and a.skip_merge:
        print(f"Done (`tables` only): {built}")
        return

    print(f"\n=== Phase 2: Sequential GPU `embed` + `merge` ({len(built)} makes) ===", flush=True)
    for make in built:
        if not a.skip_embed and not a.skip_merge:
            run_embed_and_merge(make, device=a.device)
        elif not a.skip_embed:
            subprocess.run([PY, BUILD, "--make", make, "--work-dir", WORK_DST, "--device", a.device, "embed"])
        elif not a.skip_merge:
            subprocess.run([PY, BUILD, "--make", make, "--work-dir", WORK_DST, "merge"])
            ensure_comm_indexes(os.path.join(KB_DIR, f"{make}.db"))

if __name__ == "__main__":
    main()
