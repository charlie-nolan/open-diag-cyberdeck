"""
One working SQLite file per car (records/<VIN>.db, or records/novin_<customer>_<car>_<id>.db for a car with no
VIN): every READ CAR and test-drive reading with its time, each value parsed out and judged against the make
DB's zones (manual ranges, worked out by telemetry_ref, never by a model), the chat, and each visit's case
(fields, verdict, maintenance ticks). For record keeping and comparing a car's readings over time.

The app's session DB still runs the open case; this file is the car's history and is only ever added to.
Open it with any SQLite tool (DB Browser for SQLite), e.g.
    SELECT datetime(r.t,'unixepoch','localtime'), v.key, v.value, v.unit, v.status
    FROM reading r JOIN reading_value v ON v.reading_id = r.id WHERE v.key = 'ect' ORDER BY r.t;
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "records")
SCHEMA = """
CREATE TABLE IF NOT EXISTS car (k TEXT PRIMARY KEY, v TEXT);   -- key, vin, year, make, model, engine, trans,
                                                                -- customer, first_seen, last_seen
CREATE TABLE IF NOT EXISTS visit (visit_ref TEXT PRIMARY KEY, started TEXT, updated TEXT, vehicle TEXT,
    miles TEXT, case_json TEXT);                                -- the deck's visit id; case = every case field
CREATE TABLE IF NOT EXISTS reading (id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL,  -- read | drive
    visit_ref TEXT, drive_id TEXT, source TEXT, codes TEXT, live TEXT, miles TEXT,
    UNIQUE (t, kind));
CREATE TABLE IF NOT EXISTS reading_value (reading_id INTEGER NOT NULL, key TEXT NOT NULL, value REAL, unit TEXT,
    status TEXT,        -- in / low / high (against low-high from the make DB) / no zone / generic
    low REAL, high REAL, citation TEXT, PRIMARY KEY (reading_id, key));
CREATE INDEX IF NOT EXISTS reading_value_key ON reading_value(key);
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY, t REAL NOT NULL, visit_ref TEXT, sender TEXT,
    message TEXT, UNIQUE (t, sender));
"""


def file_for(key, folder=None):
    folder = folder or DIR
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", key or ""):
        return os.path.join(folder, f"{key}.db")
    slug = re.sub(r"[^a-z0-9]+", "_", (key or "").lower().replace("novin|", "")).strip("_")[:60]
    return os.path.join(folder, f"novin_{slug}_{hashlib.sha1(key.encode()).hexdigest()[:6]}.db")


def values(live, source, car):
    """[{key, value, unit, status, low, high, citation}] from a live line. With a known reader (ssm/obd01) and a
    make DB that has zones, each value is judged by telemetry_ref; otherwise the generic parse, status 'generic'."""
    try:
        import telemetry_ref
        if source and car.get("db") and telemetry_ref.has_tables(car["db"]):
            got = telemetry_ref.normalize(telemetry_ref.from_live_text(live, source), source)
            if got:
                return [{"key": j["key"], "value": j["value"], "unit": j["unit"], "status": j["status"],
                         "low": j["low"], "high": j["high"], "citation": j["citation"]}
                        for j in telemetry_ref.judge(got, car)]
    except Exception:       # a records problem must never stop a read
        pass
    try:
        import live_data
        return [{"key": k, "value": v, "unit": "", "status": "generic", "low": None, "high": None, "citation": None}
                for k, v in live_data.parse(live).items()]
    except Exception:
        return []


class CarRecords:
    _lock = threading.Lock()

    def __init__(self, key, folder=None):
        self.key = key
        self.path = file_for(key, folder)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._lock, self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        return sqlite3.connect(self.path, timeout=10)

    def update_car(self, v):
        now = time.strftime("%Y-%m-%d %H:%M")
        with self._lock, self._db() as db:
            rows = {"key": self.key, "last_seen": now, **{k: str(x) for k, x in (v or {}).items() if x}}
            db.execute("INSERT OR IGNORE INTO car VALUES ('first_seen', ?)", (now,))
            db.executemany("INSERT OR REPLACE INTO car VALUES (?, ?)", rows.items())

    def save_visit(self, visit_ref, vehicle, case, miles=""):
        now = time.strftime("%Y-%m-%d %H:%M")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO visit VALUES (?,?,?,?,?,?) ON CONFLICT(visit_ref) DO UPDATE SET updated=excluded.updated, "
                       "vehicle=excluded.vehicle, miles=excluded.miles, case_json=excluded.case_json",
                       (str(visit_ref), now, now, vehicle, miles, json.dumps(case)))

    def add_reading(self, t, kind, codes, live, source="", visit_ref="", drive_id="", miles="", car=None):
        vals = values(live, source, car or {}) if live else []
        with self._lock, self._db() as db:
            cur = db.execute("INSERT OR IGNORE INTO reading (t, kind, visit_ref, drive_id, source, codes, live, miles) "
                             "VALUES (?,?,?,?,?,?,?,?)", (t, kind, str(visit_ref or ""), str(drive_id or ""),
                                                          source, codes, live, miles))
            if not cur.rowcount:
                return False            # already recorded (a pending reading flushed twice)
            rid = cur.lastrowid
            db.executemany("INSERT OR REPLACE INTO reading_value VALUES (?,?,?,?,?,?,?,?)",
                           [(rid, x["key"], x["value"], x["unit"], x["status"], x["low"], x["high"],
                             x["citation"]) for x in vals])
        return True

    def add_chat(self, t, sender, message, visit_ref=""):
        with self._lock, self._db() as db:
            db.execute("INSERT OR IGNORE INTO chat (t, visit_ref, sender, message) VALUES (?,?,?,?)",
                       (t, str(visit_ref or ""), sender, message))

    def earlier(self, keys, visit_ref, per_key=3):
        """{key: [(date, value, unit, status)]} from READ CAR readings at OTHER visits, newest first."""
        out = {}
        with self._lock, self._db() as db:
            for k in keys:
                rows = db.execute(
                    "SELECT date(r.t,'unixepoch','localtime'), v.value, v.unit, v.status FROM reading r "
                    "JOIN reading_value v ON v.reading_id=r.id WHERE v.key=? AND r.kind='read' "
                    "AND coalesce(r.visit_ref,'') != ? ORDER BY r.t DESC LIMIT ?",
                    (k, str(visit_ref or ""), per_key)).fetchall()
                if rows:
                    out[k] = rows
        return out

    def compare_text(self, live, source, car, visit_ref, budget=500):
        """One line per value read now that was also read at an earlier visit: 'ect now 92 C (in); earlier
        2026-10-02 88 C (in), ...'. '' when there's no history. Python only, for the case the models see."""
        now = values(live, source, car or {}) if live else []
        hist = self.earlier([x["key"] for x in now], visit_ref)
        lines = []
        for x in now:
            if x["key"] in hist:
                was = ", ".join(f"{d} {_n(v)}{' ' + u if u else ''}" + (f" ({s})" if s not in ("no zone", "generic") else "")
                                for d, v, u, s in hist[x["key"]])
                st = f" ({x['status']})" if x["status"] not in ("no zone", "generic") else ""
                lines.append(f"{x['key']} now {_n(x['value'])}{' ' + x['unit'] if x['unit'] else ''}{st}; earlier {was}")
        out = ""
        for ln in lines:
            if len(out) + len(ln) + 1 > budget:
                break
            out += ("\n" if out else "") + ln
        return out


def _n(v):
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)
