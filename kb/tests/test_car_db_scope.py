"""kb/car_db.py scope and threads on tiny fake DBs (no data files needed)."""
import argparse
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import car_db  # noqa: E402


def car(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE vehicle (id INTEGER PRIMARY KEY, model TEXT, year INTEGER);
        CREATE TABLE section (id INTEGER PRIMARY KEY, body TEXT);
        CREATE TABLE section_vehicle (vehicle_id INTEGER, section_id INTEGER);
        CREATE TABLE dtc_procedure (code TEXT, vehicle_id INTEGER, section_id INTEGER);
        CREATE TABLE dtc (code TEXT PRIMARY KEY, module TEXT, description TEXT);
        CREATE TABLE dtc_ssm (code TEXT, tmp_addr TEXT, mem_addr TEXT, bit INTEGER);
        CREATE TABLE telem_source_map (id INTEGER PRIMARY KEY, source TEXT, raw_id TEXT, param TEXT, origin TEXT);
        INSERT INTO vehicle VALUES (1, 'Civic', 2004);
        INSERT INTO section VALUES (1, 'DTC P0301 cylinder 1 misfire'), (2, 'Codes P0340 - P0505. See also P0116.');
        INSERT INTO section_vehicle VALUES (1, 1), (1, 2);
        INSERT INTO dtc_procedure VALUES ('P0301', 1, 1);
        INSERT INTO dtc VALUES ('P0301', 'engine', 'Cylinder 1 misfire'), ('P0116', 'engine', 'ECT range');
        INSERT INTO telem_source_map(source, raw_id, param, origin) VALUES
          ('obdb', 'a', 'x', 'OBDb Honda-Civic@1 (CC-BY-SA-4.0)'), ('obdb', 'b', 'y', 'OBDb Honda-Accord@1 (CC-BY-SA-4.0)'),
          ('obdb', 'c', 'z', 'OBDb Honda@1 (CC-BY-SA-4.0)'), ('obd01', '0C', 'rpm', 'SAE J1979');
    """)
    c.commit()
    c.close()


class TestScope(unittest.TestCase):
    def test_scope(self):
        d = tempfile.mkdtemp()
        db = os.path.join(d, "honda.db")
        car(db)
        car_db.cmd_scope(argparse.Namespace(db=db, model="Civic", year=2004, ecu_ids="",
                                            rom_db=os.path.join(d, "none.db")))
        c = sqlite3.connect(db)
        rows = dict(c.execute("SELECT code, source FROM car_dtc"))
        self.assertEqual(rows["P0301"], "diagnostic page for this car")
        self.assertEqual(rows["P0116"], "named on this car's manual pages")
        self.assertNotIn("P0505", rows)                 # a range heading's bound, not a code the manual names
        self.assertNotIn("P0340", rows)
        origins = {o for (o,) in c.execute("SELECT origin FROM telem_source_map")}
        self.assertNotIn("OBDb Honda-Accord@1 (CC-BY-SA-4.0)", origins)   # another model's repo
        self.assertIn("OBDb Honda@1 (CC-BY-SA-4.0)", origins)             # the make-wide one stays
        side = sqlite3.connect(os.path.join(d, "honda.make_tables.db"))
        self.assertEqual(side.execute("SELECT count(*) FROM telem_source_map").fetchone()[0], 1)


class TestThreads(unittest.TestCase):
    def test_threads(self):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "honda.threads.db")
        c = sqlite3.connect(src)
        c.executescript("""
            CREATE TABLE raw_meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE raw_thread (id INTEGER PRIMARY KEY, src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT,
              title TEXT, created INTEGER, score INTEGER, n_replies INTEGER, tag_models TEXT, tag_years TEXT,
              tag_dtcs TEXT, card_keep INTEGER, text TEXT NOT NULL, UNIQUE (src, tid));
            CREATE VIRTUAL TABLE raw_thread_fts USING fts5(title, text, content='raw_thread', content_rowid='id');
            INSERT INTO raw_meta VALUES ('make', 'honda');
            INSERT INTO raw_thread(id, src, tid, title, tag_models, tag_years, text) VALUES
              (1, 'r', '1', 'misfire', 'Civic', '2004', 'P0301 fixed by a coil'),
              (2, 'r', '2', 'old', 'Civic', '1992', 'carb'),
              (3, 'r', '3', 'other', 'Accord', '2004', 'x'),
              (4, 'r', '4', 'no year', 'Civic', '', 'y');
        """)
        c.commit()
        c.close()
        out = os.path.join(d, "car.threads.db")
        car_db.cmd_threads(argparse.Namespace(db=os.path.join(d, "car.db"), src=src, out=out, model="Civic",
                                              year=2004, window=2, years=None))
        ids = [r[0] for r in sqlite3.connect(out).execute("SELECT id FROM raw_thread ORDER BY id")]
        self.assertEqual(ids, [1, 4])


if __name__ == "__main__":
    unittest.main()
