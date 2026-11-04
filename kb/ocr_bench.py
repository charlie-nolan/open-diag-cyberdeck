#!/usr/bin/env python3
"""
Side-by-side test of page readers on hard manual pages, before any of them goes into the build.

For each page it writes what every reader produced, plus a number check: every number a model wrote
(torque, volts, ohms, step numbers) is looked up in the PDF's own text layer and in tesseract's reading
of the same page. A number found in neither is listed as UNVERIFIED, because a vision model that
invents or changes a spec is worse than one that reads nothing.

Readers:
  text       PyMuPDF text layer (exact, but empty on scanned pages and blind to icons such as YES/NO)
  tesseract  classic OCR (/usr/bin/tesseract), the independent cross-check
  <tag>      any Ollama vision model, e.g. qwen3.6:35b-a3b-q4_K_M, glm-ocr, qwen2.5vl:3b

Run on the desktop (needs pymupdf; Ollama running):
  python3 kb/ocr_bench.py --pdf "<manual.pdf>" --pages 512,513,600 \
      --models qwen3.6:35b-a3b-q4_K_M,glm-ocr --mode chart
  python3 kb/ocr_bench.py --images ~/some/charm_charts/ --models ... --mode chart
Output: logs/ocr_bench/<run>/  (one .md per page and reader, report.md)

--mode text   transcribe the page as markdown (tables as markdown tables)
--mode chart  diagnostic chart / flowchart: every step with its check and where YES and NO lead
"""
import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

PROMPTS = {
    "text": "Transcribe this service-manual page exactly as markdown. Keep every word, number and unit as "
            "printed; put tables in markdown tables with one cell per printed cell. Do not summarize, "
            "correct or add anything. Write [illegible] for anything you can't read.",
    "chart": "This is a page of a vehicle service manual diagnostic chart. Transcribe it exactly. For each "
             "numbered step write:\n### Step N\nCheck: <the check, word for word>\nYES: <what the YES "
             "branch says or the step it goes to>\nNO: <same for NO>\nKeep every number and unit exactly as "
             "printed. YES/NO may be shown as icons, arrows or column positions: report the branch only if it "
             "is visible, else write 'not shown'. Do not add steps or advice. Write [illegible] for anything "
             "you can't read.",
}
NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")


def page_png(pdf, pno, dpi, out):
    import pymupdf
    doc = pymupdf.open(pdf)
    doc[pno].get_pixmap(dpi=dpi).save(out)
    return doc[pno].get_text()


def tesseract(png):
    if not shutil.which("tesseract"):
        return ""
    r = subprocess.run(["tesseract", png, "-", "--psm", "3"], capture_output=True, text=True)
    return r.stdout


def ollama_read(model, png, prompt):
    img = base64.b64encode(open(png, "rb").read()).decode()
    payload = {"model": model, "stream": False, "think": False,
               "messages": [{"role": "user", "content": prompt, "images": [img]}],
               "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 6000}}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            data = json.loads(r.read())
    except Exception as e:  # a missing model or a tag that doesn't take images: report it, keep going
        return f"[error: {e}]", time.time() - t0
    if data.get("error"):
        return f"[error: {data['error']}]", time.time() - t0
    return data.get("message", {}).get("content", ""), time.time() - t0


def numbers(s):
    return {n.replace(",", ".") for n in NUM_RE.findall(s)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf")
    ap.add_argument("--pages", help="0-based page numbers, comma separated")
    ap.add_argument("--images", help="folder of page images (e.g. CHARM chart images) instead of a PDF")
    ap.add_argument("--models", required=True, help="Ollama vision model tags, comma separated")
    ap.add_argument("--mode", choices=PROMPTS, default="text")
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--out")
    a = ap.parse_args()
    run = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.expanduser(a.out or f"{ROOT}/logs/ocr_bench/{run}")
    os.makedirs(out, exist_ok=True)

    items = []   # (label, png, text layer)
    if a.pdf:
        for p in a.pages.split(","):
            pno = int(p)
            png = os.path.join(out, f"p{pno}.png")
            items.append((f"p{pno}", png, page_png(a.pdf, pno, a.dpi, png)))
    if a.images:
        for f in sorted(os.listdir(a.images)):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                src = os.path.join(a.images, f)
                png = os.path.join(out, os.path.splitext(f)[0] + ".png")
                if f.lower().endswith((".gif", ".webp")):   # Ollama wants png/jpeg
                    import pymupdf
                    pymupdf.Pixmap(src).save(png)
                else:
                    shutil.copy(src, png)
                items.append((os.path.splitext(f)[0], png, ""))

    rep = [f"# OCR bench {run}\n", f"mode {a.mode}, models {a.models}\n",
           "| page | reader | seconds | chars | numbers | unverified numbers |", "|---|---|---|---|---|---|"]
    for label, png, layer in items:
        tess = tesseract(png)
        known = numbers(layer) | numbers(tess)
        for name, txt in (("text", layer), ("tesseract", tess)):
            open(os.path.join(out, f"{label}.{name}.md"), "w").write(txt)
            rep.append(f"| {label} | {name} | - | {len(txt)} | {len(numbers(txt))} | - |")
        for m in a.models.split(","):
            txt, secs = ollama_read(m.strip(), png, PROMPTS[a.mode])
            open(os.path.join(out, f"{label}.{m.strip().replace(':', '_').replace('/', '_')}.md"), "w").write(txt)
            bad = sorted(numbers(txt) - known, key=lambda n: float(n))
            # with no text layer and no tesseract there is nothing to check against
            flag = "nothing to check against" if not known else (", ".join(bad[:15]) or "none")
            rep.append(f"| {label} | {m} | {secs:.0f} | {len(txt)} | {len(numbers(txt))} | {flag} |")
        print(f"  {label} done", flush=True)
    open(os.path.join(out, "report.md"), "w").write("\n".join(rep) + "\n")
    print("\n".join(rep))
    print(f"\nPer-page outputs: {out}")


if __name__ == "__main__":
    main()
