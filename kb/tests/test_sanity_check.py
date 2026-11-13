"""app/sanity_check.py on fake cases and a tiny fake car DB: a valid case gets no flags, a contradictory one does."""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))
sys.path.insert(0, os.path.join(HERE, "..", "..", "kb"))
import sanity_check as S  # noqa: E402


class FakeRetriever:
    """What sanity_check uses of kb_retriever.Retriever: db, _veh_filter, mentions."""

    def __init__(self, db, pages):
        self.db, self.pages = db, [p.lower() for p in pages]

    def _veh_filter(self, col="s.id"):
        return "", []

    def mentions(self, phrase):
        words = phrase.lower().split()
        return sum(all(w in p.split() for w in words) for p in self.pages)


def car_db():
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE dtc (code TEXT PRIMARY KEY, module TEXT, description TEXT);
        CREATE TABLE dtc_procedure (code TEXT, vehicle_id INTEGER, section_id INTEGER);
        INSERT INTO dtc VALUES ('P0301','engine','Cylinder 1 misfire'), ('P0420','engine','Catalyst efficiency'),
                               ('P0299','engine','Boost pressure low');
        INSERT INTO dtc_procedure VALUES ('P0301',1,10), ('P0420',1,11);
    """)
    return db


def ref_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE labor (id INTEGER PRIMARY KEY, section_id INTEGER, operation TEXT, hours REAL,
                            part_number TEXT, price TEXT, raw TEXT);
    """)
    rows = [(f"Engine > Turbocharger > Replace {i}",) for i in range(12)] + \
        [(f"Ignition > Spark Plug > Replace {i}",) for i in range(12)]
    db.executemany("INSERT INTO labor(operation, raw) VALUES (?, '')", rows)
    db.commit()
    db.close()


PAGES = ["spark plug removal installation", "spark plug inspection", "spark plug specifications",
         "ignition coil inspection", "catalytic converter"]


class TestSanity(unittest.TestCase):
    def setUp(self):
        self.r = FakeRetriever(car_db(), PAGES)
        self.tmp = tempfile.mkdtemp()
        self.ref = os.path.join(self.tmp, "make.db")
        ref_db(self.ref)
        S._VOCAB.clear()

    def test_valid_case_no_flags(self):
        case = {"dtcs": "P0301 (pending), P0420", "telemetry": "ECT 90 C | RPM 750 | SPEED 0 km/h | LOAD 22 % | "
                "STFT1 1.6 % | LTFT1 -3.1 % | VOLTS 14.1 V | MAF 3.2 g/s | IAT 30 C | O2 B1S1 0.1-0.9 V switching",
                "symptoms": "Rough idle when cold.", "history": "Spark plug replaced last year.", "suspect": "",
                "ruled_out": "", "intake_note": ""}
        flags = S.check(case, self.r, self.ref)
        self.assertEqual(flags, [], flags)
        self.assertEqual(S.block(flags), "")

    def test_contradictory_case_flags(self):
        case = {"dtcs": "P0301, P0301 (history), P03O1, P0299, P9999, P1999",
                "telemetry": "ECT -20 C | ECT 128 C | RPM 0 | SPEED 140 km/h | LOAD 105 % | VOLTS 18.9 V | "
                             "MAF 12 g/s | O2 B1S1 1.8 V | STFT1 -12 %",
                "symptoms": "Turbocharger whistle.", "history": "", "suspect": "", "ruled_out": "", "intake_note": ""}
        flags = S.check(case, self.r, self.ref)
        items = {(f["kind"], f["item"]) for f in flags}
        self.assertIn(("code", "P0301"), items)              # twice, two statuses
        self.assertIn(("code", "P03O1"), items)              # malformed
        level = {f["item"]: f.get("level", "flag") for f in flags}
        self.assertEqual(level["P0299"], "note")             # valid generic code, no page: coverage note only
        self.assertEqual(level["P9999"], "flag")             # malformed
        self.assertEqual(level["P1999"], "flag")             # manufacturer code nothing in the DB knows
        self.assertEqual(level["P0301"], "flag")             # two statuses
        self.assertNotIn(("code", "P0420"), items)
        self.assertTrue(any(f["kind"] == "live" and "ECT -20" in f["item"] for f in flags))     # same sensor twice
        self.assertTrue(any(f["item"] == "LOAD 105 %" for f in flags))                         # outside its range
        self.assertTrue(any(f["item"] == "VOLTS 18.9 V" for f in flags))                       # physical limit
        self.assertTrue(any(f["item"] == "O2 B1S1 1.8 V" for f in flags))                      # J1979 0-1.275 V
        self.assertTrue(any("RPM 0 with speed" in f["item"] for f in flags))                   # moving, engine stopped
        self.assertTrue(any("mass air flow" in f["item"] for f in flags))                      # air flow at 0 rpm
        self.assertIn(("equipment", "turbocharger"), items)
        self.assertNotIn(("equipment", "spark plug"), items)
        self.assertTrue(S.block(flags).startswith("DATA CHECK"))
        self.assertIn("MANUAL COVERAGE NOTES", S.block(flags))

    def test_valid_code_without_page_is_only_a_note(self):
        flags = S.check({"dtcs": "P0141"}, self.r, None)
        self.assertEqual([f.get("level") for f in flags], ["note"])
        self.assertFalse(S.block(flags).startswith("DATA CHECK"))

    def test_no_make_db_skips_equipment(self):
        case = {"dtcs": "", "telemetry": "", "symptoms": "Turbocharger whistle."}
        self.assertEqual(S.check(case, self.r, None), [])

    def test_parse_codes_status(self):
        self.assertEqual(S.parse_codes("P0420 (pending), P0141"), [("P0420", "pending"), ("P0141", "")])
        self.assertEqual(S.parse_codes("pending PENDING"), [])

    def test_running_engine_without_air(self):
        flags = S.check_live("RPM 800 | MAF 0 g/s", self.r)
        self.assertEqual(len(flags), 1)
        self.assertIn("mass air flow 0", flags[0]["item"])


if __name__ == "__main__":
    unittest.main()
