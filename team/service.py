"""Team service (handoff step 5): the deck sends a case, the PC runs team/run.py, the deck polls for progress.

    venv/bin/python team/service.py [--host <this PC's Tailscale IP>] [--port 8767]
    systemctl --user start teamdeck-service      (team/teamdeck-service.service, installed by --install)

Listens on the PC's Tailscale address only (falls back to 127.0.0.1 when Tailscale is down), so only devices on
Charlie's tailnet reach it. One run at a time: a second job while one runs is refused with 409.

    GET  /health                    {"ok": true, "busy": job id or null}
    POST /job   {"stage": "final" | "brainstorm" | "ask", "case": {...}, "question": "...", "web": true,
                 "prompt": "...", "role": "fast" | "deep" | "think"}      (ask: one of the main app's model calls)
                                    -> {"id": ...}   (400 when a VIN-like value is in the case)
    GET  /job/<id>?from=N           {"state": queued|running|done|failed|stopped, "progress": [lines from N],
                                     "result": {"verdict" | "board", "baseline", "report"} when done}
    POST /job/<id>/stop             stops the run (its process group)

The VIN never has to reach the PC: the deck scrubs it (teamdeck/client.py), and run.py refuses a payload that
still has one. Job folders: logs/team/jobs/<id>/ (case.json, progress.log, trace, verdict).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.path.join(ROOT, "venv", "bin", "python")
JOBS = os.path.join(ROOT, "logs", "team", "jobs")
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


def tailnet_ip():
    """This PC's Tailscale address (TEAM_HOST overrides), else 127.0.0.1 (only this PC can reach the service)."""
    if os.environ.get("TEAM_HOST"):
        return os.environ["TEAM_HOST"]
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        return out[0] if out else "127.0.0.1"
    except (OSError, subprocess.TimeoutExpired):
        return "127.0.0.1"


MAX_BODY = 2_000_000


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}       # id -> Popen
        self.state = {}       # id -> state
        self.busy = None

    def start(self, body):
        stage = body.get("stage", "final")
        if stage not in ("final", "brainstorm", "ask"):
            return 400, {"error": "stage must be final, brainstorm or ask"}
        if stage == "ask" and (not (body.get("prompt") or "").strip() or VIN_RE.search(body["prompt"])):
            return 400, {"error": "ask needs a prompt, without a VIN"}
        case = body.get("case")
        if not isinstance(case, dict) or not (case.get("vehicle_info") or {}).get("make"):
            return 400, {"error": "case needs vehicle_info with a make"}
        if VIN_RE.search(json.dumps(case)):
            return 400, {"error": "the case still holds a VIN-like value; the deck must drop it before sending"}
        if stage == "brainstorm" and not (body.get("question") or "").strip():
            return 400, {"error": "a brainstorm round needs a question"}
        with self.lock:
            if self.busy and self.state.get(self.busy) in ("queued", "running"):
                return 409, {"error": "a Team run is going; one at a time", "busy": self.busy}
            jid = time.strftime("%Y%m%d_%H%M%S") + f"_{stage}"
            out = os.path.join(JOBS, jid)
            os.makedirs(out, exist_ok=True)
            if body.get("board"):
                case = dict(case, board=body["board"])
            with open(os.path.join(out, "case.json"), "w") as f:
                json.dump(case, f, indent=1)
            args = [PY, os.path.join(HERE, "run.py"), "--case", os.path.join(out, "case.json"), "--stage", stage,
                    "--web", "on" if body.get("web", True) else "off", "--out", out]
            if stage == "brainstorm":
                args += ["--question", body["question"][:4000]]
            if stage == "ask":
                with open(os.path.join(out, "prompt.txt"), "w") as f:
                    f.write(body["prompt"][:400_000])
                args += ["--prompt-file", os.path.join(out, "prompt.txt"),
                         "--role", body.get("role") if body.get("role") in ("fast", "deep", "think") else "fast"]
            log = open(os.path.join(out, "runner.out"), "w")
            self.procs[jid] = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.state[jid] = "running"
            self.busy = jid
        threading.Thread(target=self._wait, args=(jid,), daemon=True).start()
        return 200, {"id": jid}

    def _wait(self, jid):
        rc = self.procs[jid].wait()
        with self.lock:
            if self.state.get(jid) == "running":
                self.state[jid] = "done" if rc == 0 and os.path.isfile(os.path.join(JOBS, jid, "report.json")) \
                    else "failed"

    def stop(self, jid):
        p = self.procs.get(jid)
        if p is None:
            return 404, {"error": "no such job"}
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            with self.lock:
                self.state[jid] = "stopped"
        return 200, {"state": self.state.get(jid)}

    def status(self, jid, start=0):
        out = os.path.join(JOBS, jid)
        if not re.fullmatch(r"[\w]+", jid) or not os.path.isdir(out):
            return 404, {"error": "no such job"}
        st = self.state.get(jid) or ("done" if os.path.isfile(os.path.join(out, "report.json")) else "failed")
        lines = []
        p = os.path.join(out, "progress.log")
        if os.path.isfile(p):
            lines = open(p).read().splitlines()
        if st == "failed" and os.path.isfile(os.path.join(out, "runner.out")):
            tail = open(os.path.join(out, "runner.out")).read().strip().splitlines()[-3:]
            lines += [f"(runner) {t}" for t in tail if "Loading weights" not in t]
        res = {"state": st, "progress": lines[start:], "next": len(lines)}
        if st == "done":
            r = {}
            for name in ("verdict", "board", "baseline", "answer", "notes", "lookups", "evidence"):
                f = os.path.join(out, f"{name}.txt")
                if os.path.isfile(f):
                    r[name] = open(f).read()
            r["report"] = json.load(open(os.path.join(out, "report.json")))
            res["result"] = r
        return 200, res


def make_handler(jobs):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} {self.client_address[0]} {fmt % a}\n")

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, q = self.path.partition("?")
            if path == "/health":
                return self._send(200, {"ok": True, "busy": jobs.busy if jobs.state.get(jobs.busy) == "running"
                                        else None})
            m = re.fullmatch(r"/job/(\w+)", path)
            if m:
                start = int(re.search(r"from=(\d+)", q).group(1)) if "from=" in q else 0
                return self._send(*jobs.status(m.group(1), start))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self._send(413, {"error": "case too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"error": "not JSON"})
            if self.path == "/job":
                return self._send(*jobs.start(body))
            m = re.fullmatch(r"/job/(\w+)/stop", self.path)
            if m:
                return self._send(*jobs.stop(m.group(1)))
            self._send(404, {"error": "not found"})
    return H


def install():
    """A user unit (no root): `systemctl --user enable --now teamdeck-service` to keep it running."""
    d = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(d, exist_ok=True)
    unit = os.path.join(d, "teamdeck-service.service")
    with open(unit, "w") as f:
        f.write("[Unit]\nDescription=Cyberdeck Team mode service (tailnet :8767)\nAfter=network-online.target\n\n"
                f"[Service]\nWorkingDirectory={ROOT}\nExecStart={PY} {os.path.join(HERE, 'service.py')}\n"
                "Restart=on-failure\nRestartSec=10\n\n[Install]\nWantedBy=default.target\n")
    print(f"wrote {unit}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=None, help="default: this PC's Tailscale address")
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--install", action="store_true", help="write the systemd user unit")
    a = ap.parse_args()
    if a.install:
        return install()
    os.makedirs(JOBS, exist_ok=True)
    jobs = Jobs()
    try:
        a.host = a.host or tailnet_ip()
        httpd = ThreadingHTTPServer((a.host, a.port), make_handler(jobs))
    except OSError as e:     # Tailscale down: only this PC can reach it
        print(f"can't listen on {a.host} ({e}); using 127.0.0.1", flush=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(jobs))
    print(f"Team service on {httpd.server_address[0]}:{httpd.server_address[1]}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
