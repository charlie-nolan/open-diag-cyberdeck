"""The deck's side of Team mode, no Qt: read the open case, drop the VIN and customer, talk to the PC's team service.

The team service runs on the PC (team/service.py, tailnet :8767). Nothing here writes to the car. The case is the
main app's own open case (its session_active.db, read-only).
"""
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.expanduser("~/subaru_cyberdeck")
SESSION_DB = os.path.join(ROOT, "session_active.db")


def _service_url():
    """TEAMDECK_URL, else the PC that Home mode uses (app/config.json home_url's host) on port 8767."""
    if os.environ.get("TEAMDECK_URL"):
        return os.environ["TEAMDECK_URL"].rstrip("/")
    try:
        with open(os.path.join(ROOT, "app", "config.json")) as f:
            host = urllib.parse.urlparse(json.load(f).get("home_url", "")).hostname
    except (OSError, ValueError):
        host = None
    return f"http://{host or '127.0.0.1'}:8767"


SERVICE = _service_url()
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
# the case fields the main app keeps (CyberdeckApp.CASE_KEYS) that the team may see
CASE_KEYS = ("dtcs", "telemetry", "mode06", "symptoms", "ruled_out", "suspect", "history", "intake_note",
             "intake_summary", "saved_debate", "saved_wildcard", "drive_log", "chart_reads", "chat_notes",
             "maint_selected")
EARLIER = ("earlier_visits", "past_readings")      # sent only when the mechanic ticks it
ROUNDS_MAX = 4                                      # brainstorm rounds per case (quota)


def deck_case(path=SESSION_DB):
    """{vehicle_info, <case keys>} from the main app's open case, or {} when there is none. Read-only."""
    if not os.path.isfile(path):
        return {}
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        kv = dict(c.execute("SELECT key, value FROM session_state"))
        c.close()
    except sqlite3.Error:
        return {}
    try:
        v = json.loads(kv.get("vehicle_info") or "{}")
    except ValueError:
        v = {}
    case = {k: kv.get(k, "") for k in CASE_KEYS + EARLIER if kv.get(k)}
    case["vehicle_info"] = {k: v.get(k) for k in ("year", "make", "model", "engine", "trans") if v.get(k)}
    return case


def scrub(case, earlier=False):
    """What may leave the deck: year, make, model, engine, transmission, codes, live data, notes. No VIN, plate,
    customer or mileage key. Raises ValueError if a VIN-like value survives anywhere (handoff guardrail)."""
    keep = CASE_KEYS + (EARLIER if earlier else ())
    out = {k: VIN_RE.sub("[VIN removed]", str(case[k])) for k in keep if case.get(k)}
    v = case.get("vehicle_info") or {}
    out["vehicle_info"] = {k: v[k] for k in ("year", "make", "model", "engine") if v.get(k)}
    if v.get("trans"):
        out["vehicle_info"]["transmission"] = v["trans"]
    if str(out["vehicle_info"].get("year", "")).isdigit():
        out["vehicle_info"]["year"] = int(out["vehicle_info"]["year"])
    if VIN_RE.search(json.dumps(out)):
        raise ValueError("a VIN-like value is still in the case; not sent")
    return out


def board_only(text):
    """The board part of a round's answer (the deck's lookup list stays on screen, not in the next round)."""
    return (text or "").split("\nLOOKUPS (logged by the deck", 1)[0].strip()


def _call(method, path, body=None, timeout=10):
    req = urllib.request.Request(SERVICE + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.load(e).get("error", str(e)), "status": e.code}
        except ValueError:
            return {"error": str(e), "status": e.code}
    except (OSError, ValueError) as e:
        return {"error": f"PC team service not reachable ({e})", "status": 0}


def health():
    return _call("GET", "/health", timeout=4)


def start(stage, case, question="", board="", web=True):
    return _call("POST", "/job", {"stage": stage, "case": case, "question": question, "board": board, "web": web})


def status(jid, start_at=0):
    return _call("GET", f"/job/{jid}?from={start_at}")


def stop(jid):
    return _call("POST", f"/job/{jid}/stop", {})


def ask(case, prompt, role):
    """One of the main app's model calls, answered by the team (team/run.py stage ask)."""
    return _call("POST", "/job", {"stage": "ask", "case": case, "prompt": VIN_RE.sub("[VIN removed]", prompt),
                                  "role": role}, timeout=15)
