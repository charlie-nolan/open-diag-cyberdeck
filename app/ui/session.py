"""The case and car kept between runs (session_active.db)."""
import json
import live_data
import os
import sqlite3
import telemetry_ref
import threading
import time
from ui.config import CFG
from ui.context import CASE_MAX_CHARS, CHART_READS_MAX, DRIVE_KEY, OPINION_FIELDS


class Session:
    """The open case, one key per field, in a small sqlite file. One connection for the app's lifetime
    (opening one per read meant ~15 opens per chat message); the lock lets worker threads share it."""
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS session_state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT,
                    message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
                -- every case filed under its car's VIN, so the car's past visits come back when it does
                CREATE TABLE IF NOT EXISTS visit (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, started TEXT,
                    updated TEXT, vehicle TEXT, case_json TEXT);
                CREATE INDEX IF NOT EXISTS visit_vin ON visit(vin);
                -- TEST DRIVE: one row per reading (kept even if the app closes mid-drive)
                CREATE TABLE IF NOT EXISTS drive_sample (drive_id TEXT, t REAL, codes TEXT, live TEXT);
                CREATE INDEX IF NOT EXISTS drive_sample_id ON drive_sample(drive_id);
            """)

    def add_drive_sample(self, drive_id, t, codes, live):
        with self._lock, self._db:
            self._db.execute("INSERT INTO drive_sample VALUES (?,?,?,?)", (drive_id, t, codes, live))

    def drive_ids(self):
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT drive_id FROM drive_sample")]

    def drive_samples(self, drive_id):
        with self._lock:
            return self._db.execute("SELECT t, codes, live FROM drive_sample WHERE drive_id=? ORDER BY t",
                                    (drive_id,)).fetchall()

    def save_visit(self, visit_id, vin, vehicle, case):
        """Update the open case's row (same id and VIN), else start a new one. Returns its id."""
        now = time.strftime("%Y-%m-%d %H:%M")
        with self._lock, self._db:
            if visit_id and self._db.execute("SELECT 1 FROM visit WHERE id=? AND vin=?", (visit_id, vin)).fetchone():
                self._db.execute("UPDATE visit SET updated=?, vehicle=?, case_json=? WHERE id=?",
                                 (now, vehicle, json.dumps(case), visit_id))
                return int(visit_id)
            return self._db.execute("INSERT INTO visit (vin, started, updated, vehicle, case_json) VALUES (?,?,?,?,?)",
                                    (vin, now, now, vehicle, json.dumps(case))).lastrowid

    def visits(self, vin, exclude=None):
        """[(id, started, vehicle, case dict)] for this VIN, newest first."""
        with self._lock:
            rows = self._db.execute("SELECT id, started, vehicle, case_json FROM visit WHERE vin=? AND id IS NOT ? "
                                    "ORDER BY id DESC", (vin, int(exclude) if exclude else None)).fetchall()
        out = []
        for i, started, vehicle, cj in rows:
            try:
                out.append((i, started, vehicle, json.loads(cj)))
            except ValueError:
                pass
        return out

    def set(self, k, v):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO session_state VALUES (?,?)", (k, v))

    def get(self, k):
        with self._lock:
            r = self._db.execute("SELECT value FROM session_state WHERE key=?", (k,)).fetchone()
        return r[0] if r else ""

    def log(self, sender, msg):
        with self._lock, self._db:
            self._db.execute("INSERT INTO chat_log (sender, message) VALUES (?,?)", (sender, msg))

    def telemetry_zones(self, budget=900):
        """TELEMETRY (telemetry_ref): each reading of the last READ CAR against this car's healthy zone, out-of-zone
        first with where the manual points. '' when the make DB has no telem_* tables, the live line was typed
        or edited by hand (its reader is unknown), or nothing was read."""
        src, text = self.get("telemetry_source"), self.get("telemetry")
        kb = os.path.expanduser(CFG.get("kb_db") or "")
        if not (src and text and telemetry_ref.has_tables(kb)):
            return ""
        try:
            v = json.loads(self.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        car = {"make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "engine": v.get("engine"),
               "transmission": v.get("trans"), "vin": v.get("vin"), "db": kb}
        readings = telemetry_ref.normalize(telemetry_ref.from_live_text(text, src), src)
        return telemetry_ref.summary(telemetry_ref.judge(readings, car), budget=budget, car=car) if readings else ""

    def opinions(self):
        """The OPINION_FIELDS that are filled in, labelled; '' when there are none."""
        return "\n".join(f"{label}: {self.get(k)}" for k, label in OPINION_FIELDS if (self.get(k) or "").strip())

    def case_summary(self, exclude=()):
        """exclude: keys left out (the blind first pass leaves out OPINION_FIELDS)."""
        drive = self.get(DRIVE_KEY["key"]()) or self.get("drive_log")
        live_label = "Live data (last reading of the test drive below)" if drive else "Live data"
        keys = [("vehicle", "Vehicle"), ("dtcs", "DTCs"), ("telemetry", live_label), ("symptoms", "Symptoms"),
                ("ruled_out", "Verified good / ruled out"), ("suspect", "Suspect / known defects"),
                ("history", "History"), ("earlier_visits", "Earlier visits to this car (deck's records)"),
                ("intake_summary", "Intake note summary"),
                ("maint_selected", "Maintenance the customer approved for this visit")]
        if not self.get("intake_summary"):
            keys.append(("intake_note", "Intake note (raw, as dictated)"))
        if self.get("mode06"):
            keys.insert(3, ("mode06", "Mode 06 on-board tests that failed"))
        if self.get("past_readings"):
            keys.insert(3, ("past_readings", "Same readings at this car's earlier visits (deck's records)"))
        keys = [(k, label) for k, label in keys if k not in exclude]
        out = "\n".join(f"{label}: {(live_data.for_prompt(self.get(k)) if k == 'telemetry' else self.get(k)) or '(none)'}"
                        for k, label in keys)
        meaning = live_data.explain(self.get("telemetry"))
        if meaning:
            out += "\nWhat the live data means (worked out by the deck, generic ranges):\n" + \
                   "\n".join(f"- {m}" for m in meaning)
        zones = self.telemetry_zones()
        if zones:
            out += "\nLive data against this car's manual (zones and status worked out by the deck):\n" + zones
        if self.get("saved_debate") and "saved_debate" not in exclude:
            out += "\nSaved debate (competing explanations weighed, unresolved):\n" + self.get("saved_debate")
        if self.get("saved_wildcard") and "saved_wildcard" not in exclude:
            out += ("\nSaved wildcard ideas (long shots the mechanic wanted kept; unproven, weigh against the "
                    "evidence):\n" + self.get("saved_wildcard"))
        if len(out) > CASE_MAX_CHARS:
            out = out[:CASE_MAX_CHARS] + " [...]"
        if drive:
            out += "\nTEST DRIVE LOG (a series of readings taken while driving, not one snapshot):\n" + drive
        notes = self.get("chat_notes")
        if notes:
            room = 3500 if DRIVE_KEY["key"]() == "drive_log" else 900
            out += ("\nTHE MECHANIC'S OWN NOTES FROM CHAT (their words, newest last; anything they say they replaced, "
                    "checked or tested counts as verified unless the case shows otherwise):\n" + notes[-room:])
        if self.get("chart_reads") and DRIVE_KEY["key"]() == "drive_log":
            out += ("\nCHARM DIAGNOSTIC CHARTS for this case's codes (read from the manual's pictures by a vision "
                    "model; follow their steps, but numbers marked unverified need checking on the picture):\n"
                    + self.get("chart_reads")[:CHART_READS_MAX])
        return out
