"""Read-only MCP server for one Team case: the make DB's manual, specs, cards and owner reports, scoped to the car.

One server per run, started by team/run.py on 127.0.0.1 (never the tailnet). Every block a tool returns is stored
as an evidence item with a run-unique id (M = manual, S = spec, C = card, O = owner reports, W = web page,
Y = video), so any member's claim can be checked against what it was actually given. Items go to
<run>/evidence.jsonl; read_evidence(id) gives one back.

Two profiles, by URL path:
  /mcp/full   Claude (the coordinator): every tool
  /mcp/cloud  agy members (Gemini, GPT-OSS): the manual tools only when --cloud-manual is on, and then capped at
              --cloud-manual-chars per run (never a whole manual, CHARM or labor/parts table)

Data rules: every DB opens with SQLite mode=ro (kb_retriever and community_ref already do); nothing here writes
anywhere but the run folder. fetch_page refuses the domains in team/blocked_domains.txt and any page whose
robots.txt disallows us. Tool descriptions say what a tool returns, never a car, code or cause.

    python team/kb_mcp.py --run DIR --make Subaru --model Forester --year 2002 [--engine 2.5L] [--port 0]
    python team/kb_mcp.py --stdio ...     (same tools on stdin/stdout, for tests)
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request
import urllib.robotparser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "app"), os.path.join(ROOT, "kb")]

import procedures  # noqa: E402
import vehicle_id  # noqa: E402

PROTOCOL = "2025-06-18"
UA = "teamdeck/0.1 (diagnostic research; single page fetch)"
DB_KINDS = "MSCOP"     # manual, spec, card, owner reports, the manual pack: counted against a member's cap
DTC_RE = re.compile(r"\b[PBCU][0-3][0-9A-F]{3}\b", re.I)


def blocked_domains():
    try:
        with open(os.path.join(HERE, "blocked_domains.txt")) as f:
            return [ln.split("#")[0].strip().lower() for ln in f if ln.split("#")[0].strip()]
    except OSError:
        return ["row52.com"]


class Store:
    """Run evidence: id -> item, appended to <run>/evidence.jsonl; also logs every call to <run>/lookups.jsonl."""

    def __init__(self, run_dir):
        self.dir = run_dir
        os.makedirs(run_dir, exist_ok=True)
        self.items, self.n, self.lock = {}, {}, threading.Lock()
        self.used = {}         # member -> DB characters given out this run (the per-member cap)
        p = os.path.join(run_dir, "evidence.jsonl")
        if os.path.isfile(p):          # a restarted server keeps the ids it already gave out
            with open(p) as f:
                for ln in f:
                    it = json.loads(ln)
                    self.items[it["id"]] = it
                    k = it["id"][0]
                    self.n[k] = max(self.n.get(k, 0), int(it["id"][1:]))

    def get(self, eid):
        """An item by id; re-reads the file for items the runner added (the pack, the baseline)."""
        if eid not in self.items:
            p = os.path.join(self.dir, "evidence.jsonl")
            if os.path.isfile(p):
                with self.lock, open(p) as f:
                    for ln in f:
                        it = json.loads(ln)
                        self.items.setdefault(it["id"], it)
        return self.items.get(eid)

    def add(self, kind, source, text, who):
        with self.lock:
            self.n[kind] = self.n.get(kind, 0) + 1
            it = {"id": f"{kind}{self.n[kind]}", "kind": kind, "source": source, "text": text, "by": who}
            self.items[it["id"]] = it
            with open(os.path.join(self.dir, "evidence.jsonl"), "a") as f:
                f.write(json.dumps(it) + "\n")
            return it["id"]

    def log(self, **kw):
        with self.lock, open(os.path.join(self.dir, "lookups.jsonl"), "a") as f:
            f.write(json.dumps(kw) + "\n")


class KB:
    def __init__(self, make, model=None, year=None, engine=None, codes=(), embed=True):
        path, ok = vehicle_id.kb_path_for(make, os.path.join(ROOT, "kb"))
        if not ok:
            raise FileNotFoundError(f"no knowledge DB for {make!r}")
        from kb_retriever import Retriever
        emb = None
        if embed:
            try:   # CPU query embedder (float32): the GPU belongs to Home mode and the build jobs
                import embedder
                from glob import glob
                dirs = [os.path.join(ROOT, "models", "qwen3emb")] + sorted(glob(os.path.expanduser(
                    "~/.cache/huggingface/hub/models--Qwen--Qwen3-Embedding-0.6B/snapshots/*")), reverse=True)
                d = next((x for x in dirs if os.path.isfile(os.path.join(x, "config.json"))), "")
                emb = embedder.shared(d, device="cpu") if d else None
            except Exception as e:
                print(f"kb_mcp: vector search off: {e}", file=sys.stderr)
        self.path, self.make = path, make
        self.r = Retriever(path, model=model, year=year, engine=engine, embed=emb)
        self.model, self.year, self.engine = model, year, engine
        self.codes = [c.upper() for c in codes]

    def manual(self, query, k):
        out = []
        for h in self.r.search_fsm(query, k=max(1, min(int(k), 5))):
            head = h.split("\n", 1)[0]
            out.append((head, h))
        return out

    def procedure(self, code):
        code = code.upper()
        p = procedures.fetch(self.r, code)
        if p and p["steps"]:
            text = procedures.render(p, sorted(set(self.codes) | {code}), budget=6000)
            return f"[MANUAL DIAGNOSTIC PROCEDURE: {p['title']}]", text
        raw = self.r.dtc(code, chars=4000)
        return (raw.split("\n", 1)[0] if raw else ""), raw

    def specs(self, part):
        lines = self.r.specs(part, limit=10)
        return [(ln.split("]", 1)[0] + "]", ln) for ln in lines]

    def card(self, code):
        c = self.r.card(code)
        return c.split("Manual procedure outline:", 1)[0].strip() if c else ""

    def owners(self, code=None, text=None):
        """[(head, text)]: extracted owner-thread causes (comm_* tables, when the make has them), then the closest
        unchecked threads (kb/<make>.threads.db), the same two sources the app's community_block uses."""
        import community_ref
        out = []
        try:
            res = community_ref.community(self.path, code=code, text=text, model=self.model, year=self.year,
                                          engine=self.engine, top=6)
        except sqlite3.Error:
            res = {}
        if res.get("causes"):
            lines = [f"[OWNER REPORTS: {res['threads']} threads; {res['note']}]"]
            for c in res["causes"]:
                lines.append(f"- cause: {c['cause']} | threads {c['threads']}, owner-confirmed fixes "
                             f"{c['op_confirmed']}, ruled out {c['ruled_out']} | {c['car_match']} | "
                             f"{' '.join(c['urls'][:2])}")
            out.append((lines[0], "\n".join(lines)))
        t = community_ref.raw_threads(self.path, text=text or code, code=code, model=self.model, year=self.year,
                                      embed=self.r.embed, k=4, excerpt=900)
        for x in t.get("threads", []):
            head = f"[OWNER THREAD (unchecked, ideas only): {x['title']} | {x['car_match']} | {x['url']}]"
            out.append((head, f"{head}\n{x['excerpt']}"))
        return out


def _strip_html(raw):
    raw = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def fetch_page(url, max_chars=12000):
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None, "only http(s) URLs"
    host = u.hostname.lower()
    if host in ("localhost",) or re.match(r"^(127\.|10\.|192\.168\.|100\.|169\.254\.|\[|::)", host):
        return None, "local addresses are not fetched"
    if any(host == d or host.endswith("." + d) for d in blocked_domains()):
        return None, f"{host} is on the no-fetch list (site terms)"
    rp = urllib.robotparser.RobotFileParser()
    try:   # read with our own user agent (urllib's default one is refused by some sites, which reads as "disallow all")
        rq = urllib.request.Request(f"{u.scheme}://{u.netloc}/robots.txt", headers={"User-Agent": UA})
        with urllib.request.urlopen(rq, timeout=10) as resp:
            rp.parse(resp.read(500_000).decode("utf-8", "replace").splitlines())
        if not rp.can_fetch(UA, url):
            return None, f"{host} robots.txt disallows this page"
    except Exception:
        pass   # no robots.txt (or unreadable): the page itself still has to answer
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if "html" not in ctype and "text" not in ctype:
                return None, f"not a text page ({ctype})"
            raw = resp.read(3_000_000).decode(resp.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:
        return None, f"fetch failed: {e}"
    return _strip_html(raw)[:max_chars], ""


def youtube_transcript(url, max_chars=12000):
    """(timestamped caption text, '') or (None, why). English captions (uploaded first, else automatic) via yt-dlp;
    nothing is downloaded but the caption file, into a temp folder that is removed."""
    import shutil
    import subprocess
    import tempfile
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower()
    if not (host == "youtu.be" or host.endswith("youtube.com")):
        return None, "not a YouTube URL"
    vid = (u.path.strip("/") if host == "youtu.be" else
           urllib.parse.parse_qs(u.query).get("v", [""])[0] or u.path.rsplit("/", 1)[-1])
    if not re.fullmatch(r"[\w-]{11}", vid or ""):
        return None, "no video id in the URL"
    tmp = tempfile.mkdtemp(prefix="teamyt_")
    try:
        p = subprocess.run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs",
                            "en.*,en", "--sub-format", "vtt", "--no-playlist", "-o", os.path.join(tmp, "%(id)s.%(ext)s"),
                            "--print", "%(title)s", f"https://www.youtube.com/watch?v={vid}"],
                           capture_output=True, text=True, timeout=90)
        subs = sorted(f for f in os.listdir(tmp) if f.endswith(".vtt"))
        if not subs:
            if "429" in p.stderr:
                return None, ("YouTube refused the caption download (HTTP 429, rate limit); this video can't be "
                              "checked now. Cite it only as an unchecked idea (own judgement), never as evidence.")
            return None, "no English captions for this video" + (f" ({p.stderr.strip()[-160:]})" if p.returncode else "")
        auto = [f for f in subs if ".en." in f and "orig" not in f]
        raw = open(os.path.join(tmp, (auto or subs)[0]), encoding="utf-8", errors="replace").read()
    except subprocess.TimeoutExpired:
        return None, "caption download timed out"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    lines, last = [], ""
    for m in re.finditer(r"(\d+):(\d\d):(\d\d)\.\d+ --> [^\n]*\n(.*?)(?:\n\n|\Z)", raw, re.S):
        h, mnt, sec, body = m.groups()
        text = re.sub(r"<[^>]+>", "", body).replace("\n", " ").strip()
        if not text or text == last or (last and text.startswith(last)) and len(text) - len(last) < 3:
            continue
        new = text[len(last):].strip() if last and text.startswith(last) else text
        last = text
        t = int(h) * 3600 + int(mnt) * 60 + int(sec)
        lines.append(f"[{t // 60}:{t % 60:02d} t={t}s] {new}")
    title = (p.stdout.strip().splitlines() or [""])[0]
    body = f"[VIDEO: {title} | https://www.youtube.com/watch?v={vid} | captions, ideas only]\n" + "\n".join(lines)
    return body[:max_chars], ""


TOOLS = [
    {"name": "search_manual", "manual": True,
     "description": "Search this car's factory manual (FSM and CHARM). Returns whole procedures, each with an "
                    "evidence id and its page tag. Use the manual's words.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"},
                                                      "k": {"type": "integer", "minimum": 1, "maximum": 5}},
                     "required": ["query"]}},
    {"name": "dtc_procedure", "manual": True,
     "description": "The manual's diagnostic procedure for one trouble code on this car, as numbered steps; steps "
                    "that only ask which codes are set are answered from the case's scan.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "specs", "manual": True,
     "description": "Specifications printed in this car's manual for a part or job (torque and other values), "
                    "each with its page. Empty when the manual has none: never substitute another source's number.",
     "inputSchema": {"type": "object", "properties": {"part": {"type": "string"}}, "required": ["part"]}},
    {"name": "card", "manual": False,
     "description": "Counted forum/TSB fixes for one trouble code on this car (secondary to the manual).",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}},
    {"name": "owner_reports", "manual": False,
     "description": "What owners reported for a code or symptom on this make: causes with counts of confirmed "
                    "fixes, tests they ran, thread URLs. Ideas only, not verified.",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string"}, "text": {"type": "string"}}}},
    {"name": "fetch_page", "manual": False,
     "description": "Fetch one web page as text and give it an evidence id. Any web page you cite must be fetched "
                    "with this tool, or the claim is dropped. Some sites are refused (site terms, robots.txt).",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "youtube_cache", "manual": False,
     "description": "Repair videos already checked against their captions for this car (url&t= with the quoted "
                    "caption). Empty until the video cache is built.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "youtube_transcript", "manual": False,
     "description": "The captions of one YouTube video (one you found with your own search), as timestamped lines "
                    "with an evidence id. Cite a moment as the id plus its timestamp; a video claim needs the "
                    "caption line that says it. Videos are ideas only, never a spec.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
    {"name": "read_evidence", "manual": False,
     "description": "The full text of one evidence id given out in this case (any member's lookup).",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]


class Server:
    def __init__(self, kb, store, cloud_manual=False, cloud_manual_chars=40000, web=True):
        self.kb, self.store, self.web = kb, store, web
        self.cloud_manual, self.cloud_cap = cloud_manual, cloud_manual_chars

    def tools(self, profile):
        return [{k: v for k, v in t.items() if k != "manual"} for t in TOOLS
                if (profile == "full" or not t["manual"] or self.cloud_manual)
                and (self.web or t["name"] != "fetch_page")]

    def _manual_room(self, profile, text, who):
        """Team members' DB text (manual, CHARM, cards, owner reports) is capped per member per run (Charlie
        2026-11-11: about 40k characters each); returns the text that may go, or None. The lead (full) is not capped."""
        if profile == "full":
            return text
        with self.store.lock:
            used = self.store.used.get(who, 0)
            room = self.cloud_cap - used
            if room <= 200:
                return None
            text = text[:room]
            self.store.used[who] = used + len(text)
            return text

    def call(self, name, args, profile, who):
        allowed = {t["name"] for t in self.tools(profile)}
        if name not in allowed:
            return f"tool {name} is not available to this member", True
        a = args or {}
        out, blocks = "", []
        try:
            if name == "search_manual":
                k = a.get("k", 3) if profile == "full" else 1     # team members: one procedure at a time
                for head, text in self.kb.manual(a["query"], k):
                    blocks.append(("M", head, text))
            elif name == "dtc_procedure":
                code = a["code"].strip().upper()
                if not DTC_RE.fullmatch(code):
                    return "not a trouble code", True
                head, text = self.kb.procedure(code)
                if text:
                    blocks.append(("M", head or f"[DTC] {code}", text))
            elif name == "specs":
                for head, text in self.kb.specs(a["part"]):
                    blocks.append(("S", head, text))
            elif name == "card":
                text = self.kb.card(a["code"].strip())
                if text:
                    blocks.append(("C", "[CARD] " + a["code"].upper(), text))
            elif name == "owner_reports":
                for head, text in self.kb.owners(a.get("code"), a.get("text")):
                    blocks.append(("O", head, text))
            elif name == "fetch_page":
                text, why = fetch_page(a["url"])
                if text is None:
                    self.store.log(who=who, tool=name, args=a, refused=why)
                    return why, True
                blocks.append(("W", a["url"], text))
            elif name == "youtube_transcript":
                text, why = youtube_transcript(a["url"])
                if text is None:
                    self.store.log(who=who, tool=name, args=a, refused=why)
                    return why, True
                blocks.append(("Y", a["url"], text))
            elif name == "youtube_cache":
                p = os.path.join(ROOT, "kb", "youtube", "youtube.db")
                if not os.path.isfile(p):
                    out = "(the video cache is not built yet)"
                else:
                    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    rows = c.execute("SELECT url, quote FROM clip WHERE quote LIKE ? LIMIT 5",
                                     (f"%{a['query'][:60]}%",)).fetchall()
                    for url, quote in rows:
                        blocks.append(("Y", url, quote))
            elif name == "read_evidence":
                it = self.store.get(a["id"].strip().upper())
                if not it:
                    return "no such evidence id in this case", True
                if it["kind"] in "MSP" and profile == "cloud" and not self.cloud_manual:
                    return "manual text is not shared with this member", True
                text = it["text"] if it["kind"] not in DB_KINDS else self._manual_room(profile, it["text"], who)
                return (f"[{it['id']}] {it['source']}\n{text}" if text else "DB allowance for this case used up"), \
                    not text
        except KeyError as e:
            return f"missing argument {e}", True
        except Exception as e:
            self.store.log(who=who, tool=name, args=a, error=repr(e))
            return f"lookup failed: {e}", True
        parts = []
        for kind, head, text in blocks:
            if kind in DB_KINDS:
                text = self._manual_room(profile, text, who)
                if text is None:
                    parts.append("(your DB allowance for this case is used up: about "
                                 f"{self.cloud_cap // 1000}k characters per member)")
                    break
            eid = self.store.add(kind, head, text, who)
            parts.append(f"[{eid}] {head}\n{text}" if not text.startswith(head) else f"[{eid}] {text}")
        self.store.log(who=who, tool=name, args=a, ids=[p.split("]", 1)[0][1:] for p in parts if p.startswith("[")])
        return out or "\n\n".join(parts) or "(nothing found)", False

    def handle(self, msg, profile, who):
        """One JSON-RPC message -> response dict, or None for a notification."""
        mid, method = msg.get("id"), msg.get("method", "")
        if mid is None:
            return None
        if method == "initialize":
            res = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "teamkb", "version": "0.1"},
                   "instructions": "Read-only lookups for one car. Cite the [id] of every item you use."}
        elif method == "tools/list":
            res = {"tools": self.tools(profile)}
        elif method == "tools/call":
            p = msg.get("params", {})
            text, err = self.call(p.get("name"), p.get("arguments"), profile, who)
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        elif method == "ping":
            res = {}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}


def http_server(srv, port=0):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _profile(self):
            parts = self.path.split("?")[0].strip("/").split("/")
            if len(parts) < 2 or parts[0] != "mcp" or parts[1] not in ("full", "cloud"):
                return None, None
            return parts[1], (parts[2] if len(parts) > 2 else parts[1])

        def do_GET(self):
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            profile, who = self._profile()
            if profile is None:
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            msgs = body if isinstance(body, list) else [body]
            out = [r for r in (srv.handle(m, profile, who) for m in msgs if isinstance(m, dict)) if r]
            if not out:
                self.send_response(202)
                self.end_headers()
                return
            data = json.dumps(out if isinstance(body, list) else out[0]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "teamkb")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer(("127.0.0.1", port), H)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--make", required=True)
    ap.add_argument("--model")
    ap.add_argument("--year", type=int)
    ap.add_argument("--engine")
    ap.add_argument("--codes", default="")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--profile", default="full", choices=("full", "cloud"))
    ap.add_argument("--cloud-manual", action="store_true")
    ap.add_argument("--cloud-manual-chars", type=int, default=40000)
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--no-web", action="store_true")
    a = ap.parse_args()
    kb = KB(a.make, a.model, a.year, a.engine, DTC_RE.findall(a.codes), embed=not a.no_embed)
    srv = Server(kb, Store(a.run), a.cloud_manual, a.cloud_manual_chars, web=not a.no_web)
    if a.stdio:
        for ln in sys.stdin:
            if ln.strip():
                r = srv.handle(json.loads(ln), a.profile, a.profile)
                if r:
                    print(json.dumps(r), flush=True)
        return
    httpd = http_server(srv, a.port)
    print(f"PORT {httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
