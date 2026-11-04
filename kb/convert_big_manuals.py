#!/usr/bin/env python3
"""
Convert the big service-manual PDFs that batch_convert_fsm_pdfs.py never finished.

Same marker setup as ingest/batch_convert_fsm_pdfs.py (marker_single, one process, CUDA),
but each PDF is converted in page chunks so a 2,000-page manual can't run the GPU/RAM out,
and a crash only costs one chunk. Re-running resumes where it stopped.

Output matches the existing layout, so build_subaru_kb.py can read it:
  test_batch_out/<name>/<name>.md   (+ images)

Run on the desktop, in the marker env:
  conda activate marker_env
  python3 kb/convert_big_manuals.py            # all 7
  python3 kb/convert_big_manuals.py --only 1999 # names containing "1999"
  python3 kb/convert_big_manuals.py --list      # show status only
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

import pymupdf  # only to count pages; pip install pymupdf if missing

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
PDF_DIR = os.path.join(ROOT, "manual_pdfs")
OUT_DIR = os.path.join(ROOT, "test_batch_out")
CHUNK_DIR = os.path.join(OUT_DIR, "_chunks")

MANUALS = [
    "Forester_SF/USDM Forester FSM 1999 MSA5TCD99F (SF).pdf",
    "Forester_SG/USDM Forester FSM 2004 G8080BE_V14 (SG).pdf",
    "Impreza_GD/Subaru Impreza 2002.pdf",
    "Outback_BP/2004 Service Manual USDM.pdf",
    "Outback_BP/2005 Service Manual USDM.pdf",
    "Outback_BP/2008 Service Manual.pdf",
    "Outback_BP/2009 Service Manual USDM.pdf",
]


# marker_single, but empty images (0 px) are skipped and logged instead of crashing save_output
# ("cannot write empty image as JPEG"), which used to fail the whole chunk.
MARKER_GUARDED = r"""
import sys
import marker.output as mo
import marker.scripts.convert_single as cs
_save = mo.save_output
def save_output(rendered, output_dir, fname_base):
    imgs = getattr(rendered, "images", None) or {}
    for k in [k for k, v in imgs.items() if v is None or v.width == 0 or v.height == 0]:
        print(f"[skip] empty image {k}", flush=True)
        del imgs[k]
    return _save(rendered, output_dir, fname_base)
mo.save_output = cs.save_output = save_output
sys.argv[0] = "marker_single"
sys.exit(cs.convert_single_cli())
"""


def marker_env():
    env = os.environ.copy()          # same settings as batch_convert_fsm_pdfs.py
    env["SURYA_INFERENCE_BACKEND"] = "llamacpp"
    env["INFERENCE_BACKEND"] = "llamacpp"
    env["CUDA_VISIBLE_DEVICES"] = "0"
    env["TORCH_DEVICE"] = "cuda"
    return env


def convert(pdf_rel, chunk_pages):
    pdf = os.path.join(PDF_DIR, pdf_rel)
    name = os.path.splitext(os.path.basename(pdf))[0]
    final_dir = os.path.join(OUT_DIR, name)
    final_md = os.path.join(final_dir, f"{name}.md")
    if os.path.isfile(final_md):
        print(f"[SKIP] {name}: already converted")
        return True
    pages = pymupdf.open(pdf).page_count
    chunks = [(a, min(a + chunk_pages, pages) - 1) for a in range(0, pages, chunk_pages)]
    print(f"[*] {name}: {pages} pages in {len(chunks)} chunks")

    parts = []
    for n, (a, b) in enumerate(chunks, 1):
        cdir = os.path.join(CHUNK_DIR, name, f"part_{a:05d}")
        md = os.path.join(cdir, name, f"{name}.md")
        parts.append((cdir, md))
        if os.path.isfile(md):
            print(f"    [{n}/{len(chunks)}] pages {a}-{b}: done earlier")
            continue
        print(f"    [{n}/{len(chunks)}] pages {a}-{b} ...", flush=True)
        os.makedirs(cdir, exist_ok=True)
        r = subprocess.run([sys.executable, "-c", MARKER_GUARDED, pdf, "--output_dir", cdir, "--page_range", f"{a}-{b}"],
                           env=marker_env())
        if r.returncode != 0 or not os.path.isfile(md):
            print(f"    [!] chunk failed (exit {r.returncode}). Re-run to retry; "
                  f"try --chunk 100 if it ran out of memory.")
            return False

    # Merge chunks into the normal layout
    os.makedirs(final_dir, exist_ok=True)
    with open(final_md + ".tmp", "w", encoding="utf-8") as out:
        for cdir, md in parts:
            src_dir = os.path.dirname(md)
            for f in os.listdir(src_dir):
                if f.endswith(".md") or f.endswith("_meta.json"):
                    continue
                dst = os.path.join(final_dir, f)
                if not os.path.exists(dst):
                    shutil.copy2(os.path.join(src_dir, f), dst)
            out.write(open(md, encoding="utf-8").read().rstrip() + "\n\n")
    os.replace(final_md + ".tmp", final_md)
    print(f"[✓] {name} -> {final_md}")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="convert only manuals whose path contains this text")
    ap.add_argument("--chunk", type=int, default=200, help="pages per marker run (default 200)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    todo = [m for m in MANUALS if not a.only or a.only.lower() in m.lower()]
    if a.list:
        for m in todo:
            name = os.path.splitext(os.path.basename(m))[0]
            done = os.path.isfile(os.path.join(OUT_DIR, name, f"{name}.md"))
            print(("done   " if done else "todo   ") + m)
        return
    if not shutil.which("marker_single"):
        sys.exit("marker_single not found. Run: conda activate marker_env")
    failed = [m for m in todo if not convert(m, a.chunk)]
    if failed:
        sys.exit(f"{len(failed)} manual(s) not finished; re-run the same command to resume.")
    print("All done. Chunk folders in test_batch_out/_chunks can be deleted.")


if __name__ == "__main__":
    main()
