#!/usr/bin/env python3
"""
CHARM picture server for the deck: hands out single pictures from the Operation CHARM images LMDB (on the
Seagate) so the app can show a manual page's pictures without copying 650 GB to the deck.

The LMDB is opened readonly + lock=False and nothing is ever written to it. Requests:
  GET /img?k=<key>    one picture. <key> is the page's "/images/..." src (with or without "/images", the same
                      key kb/charm_images.py uses). Big pictures are shrunk to --max-w px and sent as WebP.
  GET /read?k=<key>   the vision model's reading of a chart picture (JSON). Not read yet: it is queued ahead
                      of the background list and read when the GPU is free (--busy-cmd), then kept in
                      --reads-db, so each picture is read once. Every number in a reading is checked against
                      tesseract's plain OCR of the same picture; numbers OCR didn't see come back as
                      "unverified" so the app can say check them on the picture.
  GET /ping

    ~/subaru_cyberdeck/venv/bin/python tools/charm_pic_server.py serve --host <your PC tailnet address>
    ~/subaru_cyberdeck/venv/bin/python tools/charm_pic_server.py queue --db kb/subaru.db --car Forester --year 2002
    ~/subaru_cyberdeck/venv/bin/python tools/charm_pic_server.py status

Bind it to the Tailscale address only (the default), so only the user's own devices can reach it.
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import lmdb

try:
    from PIL import Image
except ImportError:            # still serves, just never shrinks
    Image = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?(?![\w.])")
# ocr_bench.py's chart prompt (best on the user's hard pages), plus a way out for pictures that aren't charts
PROMPT = ("This is a picture from a vehicle service manual diagnostic procedure. If it is a diagnostic chart or "
          "flowchart, transcribe it exactly. For each numbered step write:\n### Step N\nCheck: <the check, word "
          "for word>\nYES: <what the YES branch says or the step it goes to>\nNO: <same for NO>\nKeep every "
          "number and unit exactly as printed. YES/NO may be shown as icons, arrows or column positions: report "
          "the branch only if it is visible, else write 'not shown'. If it is a table, transcribe it as a markdown "
          "table. If it is not a chart or table (a wiring diagram, connector view, photo or drawing), reply with "
          "one line starting 'NOT A CHART:' saying what it shows, then list the printed labels and numbers. Do "
          "not add steps or advice. Write [illegible] for anything you can't read.")
SCHEMA = """
CREATE TABLE IF NOT EXISTS reading (img_key TEXT PRIMARY KEY, state TEXT NOT NULL, text TEXT, unverified TEXT,
  checked INTEGER, model TEXT, secs REAL, at TEXT);
CREATE TABLE IF NOT EXISTS job (img_key TEXT PRIMARY KEY, prio INTEGER NOT NULL, added REAL NOT NULL);
"""


def norm(key):
    return key[len("/images"):] if key.startswith("/images/") else key


def numbers(s):
    return {n.replace(",", ".") for n in NUM_RE.findall(s or "")}


class Reads:
    """The readings cache + the job queue, one SQLite file. A connection per call (threads)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.db() as c:
            c.executescript(SCHEMA)

    def db(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def get(self, key):
        with self.db() as c:
            return c.execute("SELECT state, text, unverified, checked, model FROM reading WHERE img_key=?",
                             (key,)).fetchone()

    def add_job(self, key, prio):
        """prio 0 = someone is waiting on it, 1 = background. A waiting request moves a background job up."""
        with self.db() as c:
            c.execute("INSERT INTO job VALUES (?,?,?) ON CONFLICT(img_key) DO UPDATE SET prio=min(prio, excluded.prio)",
                      (key, prio, time.time()))

    def ahead(self, key):
        with self.db() as c:
            row = c.execute("SELECT prio, added FROM job WHERE img_key=?", (key,)).fetchone()
            if not row:
                return None
            return c.execute("SELECT count(*) FROM job WHERE prio < ? OR (prio = ? AND added < ?)",
                             (row[0], row[0], row[1])).fetchone()[0]

    def next_job(self):
        with self.db() as c:
            row = c.execute("SELECT img_key FROM job ORDER BY prio, added LIMIT 1").fetchone()
            return row[0] if row else None

    def done(self, key, state, text, unverified, checked, model, secs):
        with self.db() as c:
            c.execute("INSERT OR REPLACE INTO reading VALUES (?,?,?,?,?,?,?,?)",
                      (key, state, text, None if unverified is None else json.dumps(unverified), int(checked),
                       model, secs, time.strftime("%Y-%m-%d %H:%M:%S")))
            c.execute("DELETE FROM job WHERE img_key=?", (key,))


def gpu_busy(cmd):
    """True while another GPU job runs (cmd exits 0 when the GPU is busy). No cmd: never busy."""
    if not cmd:
        return False
    try:
        return subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=60).returncode == 0
    except subprocess.TimeoutExpired:
        return True


def own_model_only(a):
    """True when the only model Ollama has loaded is our reader (kept loaded by keep_alive after a read)."""
    try:
        with urllib.request.urlopen(f"{a.ollama}/api/ps", timeout=10) as r:
            names = [m.get("name") or m.get("model") for m in json.loads(r.read()).get("models", [])]
    except Exception:
        return False
    return names == [a.model]


def unload_model(a):
    payload = {"model": a.model, "keep_alive": 0}
    req = urllib.request.Request(f"{a.ollama}/api/generate", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
    except Exception:
        pass


def read_picture(data, a):
    """(state, text, unverified numbers or None, checked by tesseract?)"""
    with Image.open(io.BytesIO(data)) as im:
        if im.width < 200 or im.height < 120:
            return "skipped", "too small to be a chart (icon or arrow)", None, False
        im = im.convert("RGB")
        if im.width < 1000:      # small line art reads better (for both readers) at about 2x
            f = min(2.0, 1000 / im.width)
            im = im.resize((round(im.width * f), round(im.height * f)), Image.LANCZOS)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
            im.save(t, "PNG")
            png = t.name
    try:
        tess = ""
        if shutil.which("tesseract"):
            tess = subprocess.run(["tesseract", png, "-", "--psm", "3"], capture_output=True, text=True,
                                  timeout=120).stdout
        payload = {"model": a.model, "stream": False, "think": False, "keep_alive": "10m",
                   "messages": [{"role": "user", "content": PROMPT,
                                 "images": [base64.b64encode(open(png, "rb").read()).decode()]}],
                   "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 6000}}
        req = urllib.request.Request(f"{a.ollama}/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            out = json.loads(r.read())
        if out.get("error"):
            raise RuntimeError(out["error"])
        text = out.get("message", {}).get("content", "").strip()
        known = numbers(tess)
        bad = sorted(numbers(text) - known, key=float) if tess.strip() else None
        return "done", text, bad, bool(tess.strip())
    finally:
        os.unlink(png)


def worker(env, reads, a, errors):
    while True:
        key = reads.next_job()
        if key is None:
            time.sleep(5)
            continue
        if gpu_busy(a.busy_cmd) and own_model_only(a):
            # The busy check also sees our own reader model, which Ollama keeps loaded after each read
            # (keep_alive): unload it and check again, so the queue isn't stuck waiting on itself.
            unload_model(a)
            time.sleep(3)
        if gpu_busy(a.busy_cmd):
            Handler.gpu_wait = True
            time.sleep(60)
            continue
        Handler.gpu_wait = False
        Handler.reading = key
        t0 = time.time()
        try:
            with env.begin(buffers=False) as txn:
                data = txn.get(key.encode("utf-8"))
            if data is None:
                reads.done(key, "skipped", "picture not in the CHARM images", None, False, "", 0)
            else:
                reads.done(key, *read_picture(data, a), a.model, round(time.time() - t0, 1))
            errors.pop(key, None)
        except Exception as e:   # Ollama down, model missing...: drop the job, say why; a new request retries
            errors[key] = f"{type(e).__name__}: {e}"[:300]
            with reads.db() as c:
                c.execute("DELETE FROM job WHERE img_key=?", (key,))
            sys.stderr.write(f"read failed {key}: {errors[key]}\n")
            time.sleep(10)
        finally:
            Handler.reading = None


TYPES = {b"\x89PNG": "image/png", b"GIF8": "image/gif", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}


def kind_of(data):
    return next((t for sig, t in TYPES.items() if data.startswith(sig)), "application/octet-stream")


def shrink(data, max_w, max_bytes):
    """(bytes, content type): as stored unless it's wider than max_w or bigger than max_bytes."""
    ctype = kind_of(data)
    if Image is None or ctype == "application/octet-stream":
        return data, ctype
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width <= max_w and len(data) <= max_bytes:
                return data, ctype
            im = im.convert("RGB" if im.mode not in ("L", "1") else "L")
            if im.width > max_w:
                im = im.resize((max_w, max(1, round(im.height * max_w / im.width))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=82, method=4)
            return buf.getvalue(), "image/webp"
    except Exception:
        return data, ctype


class Handler(BaseHTTPRequestHandler):
    env = None
    args = None
    reads = None
    errors = {}
    reading = None       # key being read now
    gpu_wait = False     # the queue is waiting for another GPU job to finish
    served = 0
    lock = threading.Lock()

    def log_message(self, fmt, *a):      # quiet: one line per 200 pictures, and every error
        pass

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ping":
            return self._send(200, b"charm pictures ok")
        if u.path not in ("/img", "/read"):
            return self._send(404, b"only /img?k=<key>, /read?k=<key> and /ping")
        key = norm((parse_qs(u.query).get("k") or [""])[0])
        if not key.startswith("/") or len(key) > 1000:
            return self._send(400, b"bad key")
        if u.path == "/read":
            return self._read(key)
        with self.env.begin(buffers=False) as txn:
            data = txn.get(key.encode("utf-8"))
        if data is None:
            sys.stderr.write(f"missing: {key}\n")
            return self._send(404, b"no such picture")
        body, ctype = shrink(data, self.args.max_w, self.args.max_bytes)
        self._send(200, body, ctype)
        with Handler.lock:
            Handler.served += 1
            if Handler.served % 200 == 0:
                sys.stderr.write(f"served {Handler.served} pictures\n")


    def _json(self, obj):
        self.send_response(200)
        body = json.dumps(obj).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self, key):
        row = self.reads.get(key)
        if row:
            state, text, unverified, checked, model = row
            return self._json({"state": state, "text": text, "model": model, "checked": bool(checked),
                               "unverified": None if unverified is None else json.loads(unverified)})
        if self.reading == key:
            return self._json({"state": "reading"})
        if key in self.errors and self.reads.ahead(key) is None:
            why = self.errors.pop(key)          # reported once; asking again queues it again
            return self._json({"state": "error", "why": why})
        self.reads.add_job(key, 0)
        return self._json({"state": "queued", "ahead": self.reads.ahead(key) or 0, "gpu_busy": self.gpu_wait})


def car_keys(db_path, model, year):
    """Picture keys on this car's CHARM DTC pages (dtc_procedure), in code order."""
    db = sqlite3.connect(f"file:{os.path.expanduser(db_path)}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT p.code, s.images FROM dtc_procedure p JOIN vehicle v ON v.id=p.vehicle_id "
        "JOIN section s ON s.id=p.section_id JOIN doc d ON d.id=s.doc_id "
        "WHERE d.kind='charm' AND s.images IS NOT NULL AND upper(v.model) LIKE upper(?) || '%' AND v.year=? "
        "GROUP BY s.id ORDER BY p.code, s.id", (model, year)).fetchall()
    keys, codes = [], set()
    for code, imgs in rows:
        try:
            names = json.loads(imgs)
        except ValueError:
            continue
        codes.add(code)
        keys += [norm(n) for n in names if isinstance(n, str) and n.startswith("/images/") and norm(n) not in keys]
    return keys, len(codes), len(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="serve", choices=["serve", "queue", "status"])
    ap.add_argument("--lmdb", default=os.path.join(os.environ.get("CYBERDECK_ARCHIVE", os.path.expanduser("~/cyberdeck_archive")), "charm", "operation-charm", "lmdb-images"),
                    help="folder holding data.mdb")
    ap.add_argument("--host", default=os.environ.get("CYBERDECK_PIC_HOST", "127.0.0.1"),
                    help="address to listen on: your PC's Tailscale address for the deck, 127.0.0.1 to test")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--max-w", type=int, default=1400, help="wider pictures are shrunk to this")
    ap.add_argument("--max-bytes", type=int, default=400_000, help="bigger pictures are re-encoded as WebP")
    ap.add_argument("--reads-db", default=f"{ROOT}/kb/charm_reads.db", help="readings cache + queue")
    ap.add_argument("--model", default="qwen3.6:35b-a3b-q4_K_M", help="Ollama vision model tag")
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--busy-cmd", default="", help="shell command that exits 0 while another GPU job runs")
    ap.add_argument("--no-read", action="store_true", help="pictures only, no reading queue")
    ap.add_argument("--db", help="queue: the make's kb DB, e.g. kb/subaru.db")
    ap.add_argument("--car", help="queue: the car's model, e.g. Forester or Tahoe")
    ap.add_argument("--year", type=int)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.cmd == "status":
        r = Reads(a.reads_db)
        with r.db() as c:
            print("readings:", dict(c.execute("SELECT state, count(*) FROM reading GROUP BY state").fetchall()),
                  "| queued:", dict(c.execute("SELECT prio, count(*) FROM job GROUP BY prio").fetchall()),
                  "(0 = asked for in the app, 1 = background)")
            print("average seconds per reading:",
                  c.execute("SELECT round(avg(secs)) FROM reading WHERE state='done'").fetchone()[0])
        return
    if a.cmd == "queue":
        if not (a.db and a.car and a.year):
            sys.exit("queue needs --db, --car (e.g. Forester) and --year")
        keys, ncodes, npages = car_keys(a.db, a.car, a.year)
        print(f"{a.car} {a.year}: {ncodes} codes, {npages} CHARM DTC pages, {len(keys)} pictures")
        if not a.dry_run:
            r = Reads(a.reads_db)
            new = [k for k in keys if not r.get(k)]
            for k in new:
                r.add_job(k, 1)
            print(f"queued {len(new)} for background reading ({len(keys) - len(new)} already read)")
        return
    if not os.path.isfile(os.path.join(a.lmdb, "data.mdb")):
        sys.exit(f"no data.mdb in {a.lmdb} (is the Seagate mounted?)")
    Handler.env = lmdb.open(a.lmdb, readonly=True, lock=False, readahead=False, max_readers=64, subdir=True)
    Handler.args = a
    if not a.no_read:
        if Image is None:
            sys.exit("reading needs Pillow (or run with --no-read)")
        Handler.reads = Reads(a.reads_db)
        threading.Thread(target=worker, args=(Handler.env, Handler.reads, a, Handler.errors), daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    sys.stderr.write(f"CHARM pictures on http://{a.host}:{a.port}/img?k=<key> (read-only)"
                     + ("" if a.no_read else f", readings by {a.model}") + "\n")
    srv.serve_forever()


if __name__ == "__main__":
    main()
