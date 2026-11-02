"""Live values the deck can read for a manual step, through the shared app/telemetry_ref.py (built by the
telemetry session). Which parameter a step means is matched from the step's own words against the
telemetry DB's parameter names; what the car can report comes from telemetry_ref.capabilities()."""
import os
import re
import sqlite3

from . import ROOT

READ_RX = re.compile(r"read (?:the )?data of (.+?)(?: signal)? (?:using|with|on)\b", re.I)
GENERIC = {"the", "of", "and", "signal", "data", "value", "using", "sensor", "bank"}


def telemetry_db(make, kb_path):
    """The make's telemetry tables: merged into kb/<make>.db when the merge has run, else the standalone file."""
    try:
        with sqlite3.connect(f"file:{kb_path}?mode=ro", uri=True) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='telem_param'").fetchone():
                return kb_path
    except sqlite3.Error:
        pass
    p = os.path.join(ROOT, "kb", "telemetry", os.path.basename(kb_path).replace(".db", ".telemetry.db"))
    return p if os.path.isfile(p) else ""


def wanted(step_text):
    """The parameter phrase a step asks to read off the scan tool, or None."""
    m = READ_RX.search(step_text or "")
    return m.group(1).strip() if m else None


def param_for(phrase, tdb):
    """The canonical key whose name best covers the phrase (two or more of the name's words, at least two-thirds
    of them), or None. Equal best matches mean not sure (None), unless exactly one of them is a canonical key
    and the rest are one source's own copies ('ssm.P7') of values."""
    if not phrase or not tdb:
        return None
    want = {w for w in re.findall(r"[a-z0-9]+", phrase.lower()) if w not in GENERIC}
    scored = []
    try:
        with sqlite3.connect(f"file:{tdb}?mode=ro", uri=True) as c:
            for key, name in c.execute("SELECT key, name FROM telem_param"):
                nw = {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in GENERIC}
                if len(nw) < 2:
                    continue
                hit = len(nw & want)
                if hit >= 2 and hit / len(nw) >= 0.66:
                    scored.append((hit / len(nw), hit, key))
    except sqlite3.Error:
        return None
    if not scored:
        return None
    top = max(scored)[:2]
    best = [k for sc, h, k in scored if (sc, h) == top]
    if len(best) == 1:
        return best[0]
    canon = [k for k in best if "." not in k]
    return canon[0] if len(canon) == 1 else None


def readable(car, cable, key):
    """True when an open definition (or this car itself) says this cable can read key on this car."""
    try:
        import telemetry_ref
    except ImportError:
        return False
    tcar = {k: car.get(k) for k in ("make", "model", "year", "engine", "vin")}
    tcar.update(ecu_id=car.get("ecu") or "", db=car.get("telemetry_db") or "")
    try:
        caps = telemetry_ref.capabilities(tcar, cable)
    except Exception:
        return False
    return any(c["key"] == key and c["status"] in ("defined", "confirmed") for c in caps)
