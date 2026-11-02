"""The case object (state is this, not a chat log) and its SQLite store. Saved after every tool call, so a
crash or a long pause at the car loses nothing; `python -m diagent run --resume ID` picks it up again."""
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field

SCHEMA = """
CREATE TABLE IF NOT EXISTS case_ (id INTEGER PRIMARY KEY, created REAL, updated REAL, status TEXT, label TEXT,
                                  state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, case_id INTEGER, ts REAL, turn INTEGER, kind TEXT,
                                  tool TEXT, args TEXT, result TEXT, source_ids TEXT, ok INTEGER);
CREATE INDEX IF NOT EXISTS audit_case ON audit(case_id, id);
-- milestone 2 records live watch sessions here so runs can be replayed offline
CREATE TABLE IF NOT EXISTS watch (id INTEGER PRIMARY KEY, case_id INTEGER, ts REAL, step INTEGER, params TEXT,
                                  samples TEXT, summary TEXT);
"""


@dataclass
class Case:
    car: dict                                        # make, model, year, engine, vin, ecu_id, kb
    codes: list = field(default_factory=list)        # from the scan (or typed), replaced on each scan
    symptoms: str = ""                               # in the user's words
    readings: list = field(default_factory=list)     # {id, step, value, unit, raw, how, ts, assumed_unit}
    live: list = field(default_factory=list)         # snapshots from read_car: {id, ts, text, meanings}
    proc: dict = field(default_factory=dict)         # code, variant, current, path, ended, end_text, end_src
    hypotheses: list = field(default_factory=list)   # {cause, status, evidence, general, ts}
    sources: dict = field(default_factory=dict)      # id -> {kind, label, sid, page, text}
    scan_ts: float = 0.0                             # when codes last came from a scan of this car (0 = typed)
    state_ok: str = ""                               # last safety state the user confirmed
    status: str = "open"                             # open, finished, stopped, stalled, timeout
    result: dict = field(default_factory=dict)
    turn: int = 0
    id: int = 0

    def add_source(self, kind, label, text="", sid=None, page=None):
        """Registers something a claim may cite; returns its id (same label = same id)."""
        for k, v in self.sources.items():
            if v["label"] == label:
                return k
        prefix = {"step": "M", "section": "M", "spec": "SP", "labor": "L", "scan": "SC", "nhtsa": "N", "field": "FR", "watch": "W",
                  "figure": "F"}.get(kind, "X")
        n = 1 + sum(1 for k in self.sources if k.rstrip("0123456789") == prefix)
        key = f"{prefix}{n}"
        self.sources[key] = {"kind": kind, "label": label, "sid": sid, "page": page, "text": text[:4000]}
        return key

    def add_reading(self, step, value, unit, raw, how, assumed_unit=False):
        rid = f"R{len(self.readings) + 1}"
        self.readings.append({"id": rid, "step": step, "value": value, "unit": unit, "raw": raw, "how": how,
                              "ts": time.time(), "assumed_unit": assumed_unit})
        return rid

    def known_ids(self):
        return set(self.sources) | {r["id"] for r in self.readings} | {s["id"] for s in self.live}


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def new(self, case, label=""):
        cur = self.db.execute("INSERT INTO case_ (created, updated, status, label, state) VALUES (?,?,?,?,?)",
                              (time.time(), time.time(), case.status, label, "{}"))
        case.id = cur.lastrowid
        self.save(case)
        return case.id

    def save(self, case):
        self.db.execute("UPDATE case_ SET updated=?, status=?, state=? WHERE id=?",
                        (time.time(), case.status, json.dumps(asdict(case), default=str), case.id))
        self.db.commit()

    def load(self, case_id):
        row = self.db.execute("SELECT state FROM case_ WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise KeyError(f"no case {case_id}")
        d = json.loads(row[0])  # inf readings (open circuit) round-trip as Infinity
        return Case(**d)

    def audit(self, case, kind, tool="", args=None, result="", source_ids=(), ok=True):
        self.db.execute("INSERT INTO audit (case_id, ts, turn, kind, tool, args, result, source_ids, ok) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (case.id, time.time(), case.turn, kind, tool, json.dumps(args or {}, default=str),
                         result, json.dumps(list(source_ids)), int(ok)))
        self.db.commit()

    def audit_rows(self, case_id):
        return self.db.execute("SELECT ts, turn, kind, tool, args, result, source_ids, ok FROM audit "
                               "WHERE case_id=? ORDER BY id", (case_id,)).fetchall()

    def audit_text(self, case_id):
        """Every tool result of this case run, for the citation check."""
        return "\n".join(r[0] for r in self.db.execute(
            "SELECT result FROM audit WHERE case_id=? AND kind='tool' AND ok=1", (case_id,)))

