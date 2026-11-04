"""
Tests for app/telemetry_ref.py and kb/build_telemetry.py.
    python -m pytest kb/tests

Readings here are made-up byte patterns that sweep each definition's range; no car, code or case is special.
"""
import os
import random
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "kb"))
import telemetry_ref as T  # noqa: E402

try:
    import obd
    from obd.protocols.protocol import Message
except ImportError:
    obd = None

# Same physical quantity through both cables: (canonical key, SSM RomRaider id, J1979 PID)
PAIRS = [("map", "P7", "0B"), ("baro", "P24", "33"), ("ect", "P2", "05"), ("iat", "P11", "0F"),
         ("stft1", "P3", "06"), ("ltft1", "P4", "07"), ("timing", "P10", "0E"), ("tps", "P13", "11"),
         ("speed", "P9", "0D"), ("rpm", "P8", "0C"), ("maf", "P12", "10"), ("o2_b1s1_v", "P14", "14"),
         ("ecu_volts", "P17", "42")]


def _build_defs(make="subaru"):
    """A throw-away telemetry DB with the real definitions (params step) in a temp folder."""
    import build_telemetry as B
    d = tempfile.mkdtemp()
    B.OUT = d
    B.LOG = None
    B.step_params(make)
    return os.path.join(d, f"{make}.telemetry.db")


class Units(unittest.TestCase):
    def test_pressure(self):
        self.assertAlmostEqual(T.convert(14.6959, "psi", "kPa"), 101.325, places=2)
        self.assertAlmostEqual(T.convert(760, "mmHg", "kPa"), 101.325, places=2)
        self.assertAlmostEqual(T.convert(29.92, "inHg", "kPa"), 101.32, places=1)
        self.assertAlmostEqual(T.convert(1013.25, "hPa", "kPa"), 101.325, places=3)

    def test_temperature_and_others(self):
        self.assertAlmostEqual(T.convert(212, "°F", "°C"), 100, places=6)
        self.assertAlmostEqual(T.convert(-40, "F", "C"), -40, places=6)
        self.assertAlmostEqual(T.convert(450, "mV", "V"), 0.45, places=9)
        self.assertAlmostEqual(T.convert(60, "mph", "km/h"), 96.56, places=2)
        self.assertIsNone(T.convert(1, "kPa", "V"))          # different quantities never convert
        self.assertEqual(T.unit_norm("kilopascal"), "kPa")
        self.assertEqual(T.unit_norm("degree_Celsius"), "°C")

    def test_evaluator_is_arithmetic_only(self):
        self.assertEqual(T.evaluate("(A*256+B)/4", {"A": 0x1F, "B": 0x40}), 2000)
        with self.assertRaises(ValueError):
            T.evaluate("__import__('os')", {})
        with self.assertRaises(ValueError):
            T.evaluate("x.real", {"x": 1})


@unittest.skipIf(obd is None, "python-obd not installed")
class J1979AgainstPythonOBD(unittest.TestCase):
    """Every built-in mode 01 formula gives python-obd's value for the same reply bytes."""

    def test_formulas(self):
        by_pid = {}
        for c in obd.commands[1]:
            if c is not None:
                by_pid[c.command.decode()[2:]] = c
        rnd = random.Random(1)
        checked = 0
        for pid, (n, expr, unit) in T.OBD01.items():
            cmd = by_pid.get(pid)
            if cmd is None:
                continue
            for _ in range(25):
                data = bytes(rnd.randrange(256) for _ in range(max(n, cmd.bytes - 2)))
                m = Message([])
                m.ecu = obd.ECU.ALL
                m.data = bytearray([0x41, int(pid, 16)]) + bytearray(data)
                theirs = cmd([m]).value
                if theirs is None:
                    continue
                mag = theirs.magnitude if hasattr(theirs, "magnitude") else theirs
                tu = str(theirs.units) if hasattr(theirs, "units") else ""
                ours = T.evaluate(expr, T.bytes_env(data[:n]))
                conv = T.convert(ours, unit, tu) if T.unit_norm(tu) in T._UNIT else ours
                self.assertIsNotNone(conv, f"PID {pid}: unit {unit} vs python-obd {tu}")
                self.assertAlmostEqual(conv, float(mag), delta=max(1e-3, abs(float(mag)) * 1e-3),
                                       msg=f"PID {pid} bytes {data.hex()}: ours {ours} {unit}, python-obd {theirs}")
                checked += 1
        self.assertGreater(checked, 500)


def _have_romraider():
    import build_telemetry as B
    return os.path.exists(B.ROMRAIDER_XML)


@unittest.skipUnless(_have_romraider(), "needs the RomRaider logger XML (see kb/build_telemetry.py ROMRAIDER_XML)")
class BothCablesSameCanonical(unittest.TestCase):
    """The same physical reading through SSM2 (KKL or EX) and OBD-II mode 01 (EX) gives the same canonical value."""

    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(_build_defs())

    def _ssm_raw_for(self, ssm_id, canon_value, unit):
        """Search the SSM raw integer that encodes canon_value (definitions are monotone over their range)."""
        expr, raw_unit, n = T._source_def(self.db, "ssm", ssm_id)
        best = None
        for x in range(0, 1 << (8 * (n or 1))):
            v = T.canonical(T.evaluate(expr, {"x": x}), raw_unit)[0]
            if best is None or abs(v - canon_value) < best[1]:
                best = (x, abs(v - canon_value))
        return best[0]

    def test_pairs(self):
        rnd = random.Random(2)
        for key, ssm_id, pid in PAIRS:
            n, expr, unit = T.OBD01[pid]
            for _ in range(10):
                data = bytes(rnd.randrange(256) for _ in range(n))
                obd_r = T.normalize([{"id": pid, "bytes": data}], "obd01", t=0)
                self.assertEqual(obd_r[0]["key"], key)
                e, ru, nb = T._source_def(self.db, "ssm", ssm_id)
                lo, hi = sorted(T.canonical(T.evaluate(e, {"x": v}), ru)[0] for v in (0, (1 << (8 * (nb or 1))) - 1))
                if not lo <= obd_r[0]["value"] <= hi:
                    continue                           # outside what the SSM definition can encode
                x = self._ssm_raw_for(ssm_id, obd_r[0]["value"], unit)
                ssm_r = T.normalize([{"id": ssm_id, "raw": x}], "ssm", t=0, db=self.db)
                self.assertEqual(ssm_r[0]["key"], key, ssm_id)
                self.assertEqual(ssm_r[0]["unit"], obd_r[0]["unit"])
                # equal within the coarser definition's resolution
                step = max(abs(T.canonical(T.evaluate(T._source_def(self.db, "ssm", ssm_id)[0], {"x": x + 1}),
                                           T._source_def(self.db, "ssm", ssm_id)[1])[0] - ssm_r[0]["value"]), 1e-9)
                self.assertLessEqual(abs(ssm_r[0]["value"] - obd_r[0]["value"]), step / 2 + 1e-6,
                                     f"{key}: ssm {ssm_r[0]} obd {obd_r[0]}")

    def test_live_text_both_readers(self):
        """The two readers' live lines (ssm_reader labels vs python-obd values) normalize alike."""
        ssm = T.normalize(T.from_live_text("RPM 812 | ECT 77 C | MAP 41.0 kPa | STFT1 -2.3 % | TPS 1.2 %", "ssm"), "ssm")
        ex = T.normalize(T.from_live_text("RPM 812.0 revolutions_per_minute | ECT 77 degree_Celsius | "
                                          "MAP 41 kilopascal | STFT1 -2.3 percent | TPS 1.2 percent", "obd01"), "obd01")
        a = {r["key"]: (r["value"], r["unit"]) for r in ssm}
        b = {r["key"]: (r["value"], r["unit"]) for r in ex}
        self.assertEqual(a, b)

    def test_relative_pressure_derived_same_way(self):
        s = T.normalize([{"id": "P7", "raw": 37}, {"id": "P24", "raw": 99}], "ssm", db=self.db)
        o = T.normalize([{"id": "0B", "bytes": bytes([37])}, {"id": "33", "bytes": bytes([99])}], "obd01")
        rel = lambda rs: next(r["value"] for r in rs if r["key"] == "map_rel")
        self.assertEqual(rel(s), rel(o))
        self.assertEqual(rel(o), 37 - 99)
        # an ECU that reports relative pressure itself (SSM P25) is used as is, not derived again
        s2 = T.normalize([{"id": "P25", "raw": 128 - 62}, {"id": "P7", "raw": 37}, {"id": "P24", "raw": 99}],
                         "ssm", db=self.db)
        self.assertEqual([r["source"] for r in s2 if r["key"] == "map_rel"], ["ssm"])

    def test_adapter_voltage_is_not_ecu_voltage(self):
        r = T.normalize(T.from_live_text("Volts 14.1 volt", "obd01"), "obd01")
        self.assertEqual(r[0]["key"], "adapter_volts")
        r = T.normalize(T.from_live_text("Volts 14.1 V", "ssm"), "ssm")
        self.assertEqual(r[0]["key"], "ecu_volts")

    def test_no_definition_no_number(self):
        self.assertEqual(T.normalize([{"id": "P99999", "raw": 3}], "ssm", db=self.db), [])

    def test_ssm_capability_bits(self):
        flags = bytearray(48)
        flags[0] = 0b00000010                           # one bit set: the one RomRaider gates at byte 0 bit 1
        yes, no = T.ssm_support(b"\xff" + bytes(8) + bytes(flags), self.db)
        gated = self.db.execute("SELECT raw_id FROM telem_source_map WHERE source='ssm' AND gate='flag byte 0, bit 1'"
                                ).fetchall()
        self.assertEqual(sorted(x for x in yes if not x.startswith("S")), sorted(r[0] for r in gated))
        self.assertTrue(no)


class JudgeAndPaths(unittest.TestCase):
    """judge/pathways against a small hand-built make DB (generic names, no real car)."""

    @classmethod
    def setUpClass(cls):
        import build_telemetry as B
        cls.path = os.path.join(tempfile.mkdtemp(), "x.db")
        db = sqlite3.connect(cls.path)
        db.executescript(B.SCHEMA + """
            CREATE TABLE doc (id INTEGER PRIMARY KEY, kind TEXT, title TEXT);
            CREATE TABLE section (id INTEGER PRIMARY KEY, doc_id INTEGER, title TEXT, path TEXT, page INTEGER, body TEXT);
            INSERT INTO doc VALUES (1, 'fsm', 'Test manual');
            INSERT INTO section VALUES (10, 1, 'Data', 'Engine > Data', 5, 'table');
            INSERT INTO section VALUES (11, 1, 'Sensor inspection', 'Engine > Sensor > Inspection', 9, 'measure');
            INSERT INTO telem_condition VALUES ('idle_warm', 'Idling, warm', '{"engine": "running", "speed": [null, 3], "ect": [80, null]}', 'manual', '');
            INSERT INTO telem_condition VALUES ('koeo', 'Key on engine off', '{"engine": "off"}', 'manual', '');
            INSERT INTO telem_zone(param,model,year_min,year_max,engine,condition,low,high,unit,source_kind,section_id,page,quote,method)
                VALUES ('map', 'Alpha', 2000, 2003, NULL, 'idle_warm', NULL, 300, 'mmHg', 'manual', 10, 5, 'q', 'table');
            INSERT INTO telem_zone(param,model,year_min,year_max,engine,condition,low,high,unit,source_kind,section_id,page,quote,method)
                VALUES ('map', NULL, NULL, NULL, NULL, 'idle_warm', NULL, 55, 'kPa', 'generic', NULL, NULL, 'g', 'generic');
            INSERT INTO telem_zone(param,model,year_min,year_max,engine,condition,low,high,unit,source_kind,section_id,page,quote,method)
                VALUES ('ecu_volts', NULL, NULL, NULL, NULL, 'koeo', 12.4, NULL, 'V', 'generic', NULL, NULL, 'g', 'generic');
            INSERT INTO telem_path(param,direction,model,year_min,year_max,engine,section_id,step,kind,method,evidence,rank)
                VALUES ('map', 'any', 'Alpha', 2000, 2003, NULL, 11, NULL, 'inspection', 'component_title', 'e', 2);
        """)
        db.commit()
        db.close()

    def readings(self, **kv):
        return [{"key": k, "value": v, "unit": T.CANON[k][1]} for k, v in kv.items()]

    def test_manual_zone_wins_and_converts_units(self):
        car = {"model": "Alpha", "year": 2002, "db": self.path}
        r = self.readings(rpm=750, speed=0, ect=88, map=45)
        j = {x["key"]: x for x in T.judge(r, car)}
        self.assertEqual(j["map"]["source_kind"], "manual")
        self.assertAlmostEqual(j["map"]["high"], 300 * 0.1333224, places=3)    # 300 mmHg in kPa
        self.assertEqual(j["map"]["status"], "high")
        self.assertEqual(j["rpm"]["status"], "no zone")

    def test_other_model_gets_generic_only(self):
        car = {"model": "Beta", "year": 2002, "db": self.path}
        j = {x["key"]: x for x in T.judge(self.readings(rpm=750, speed=0, ect=88, map=45), car)}
        self.assertEqual((j["map"]["source_kind"], j["map"]["status"]), ("generic", "in"))

    def test_condition_must_hold(self):
        car = {"model": "Alpha", "year": 2002, "db": self.path}
        cold = {x["key"]: x for x in T.judge(self.readings(rpm=1200, speed=0, ect=30, map=45), car)}
        self.assertEqual(cold["map"]["status"], "no zone")      # warm-idle zone doesn't apply to a cold engine
        off = T.judge(self.readings(rpm=0, ecu_volts=12.0), car)
        self.assertEqual(T.condition(self.readings(rpm=0), car=car), "koeo")
        self.assertEqual({x["key"]: x["status"] for x in off}["ecu_volts"], "low")

    def test_pathways_and_summary(self):
        car = {"model": "Alpha", "year": 2002, "db": self.path}
        p = T.pathways("map", "high", car)
        self.assertEqual([x["section_id"] for x in p], [11])
        self.assertIn("Test manual p.9", p[0]["citation"])
        text = T.summary(T.judge(self.readings(rpm=750, speed=0, ect=88, map=45), car), car=car)
        self.assertTrue(text.startswith("- OUT OF ZONE HIGH: Manifold absolute pressure"))
        self.assertIn("manual points to: Sensor inspection", text)

    def test_no_tables_means_no_zone(self):
        j = T.judge(self.readings(rpm=750), {"model": "Alpha", "db": "/nonexistent.db"})
        self.assertEqual(j[0]["status"], "no zone")

    def test_capabilities_seen_overlay(self):
        seen = os.path.join(tempfile.mkdtemp(), "seen.db")
        car = {"vin": "TESTVIN0000000000", "model": "Alpha", "year": 2002, "db": self.path}
        T.record_seen(car, "EX", "ISO 9141-2", "obd01", ["0C", "05"], ["0B"], path=seen)
        caps = {c["raw_id"]: c["status"] for c in T.capabilities(car, "EX", seen_path=seen)}
        self.assertEqual(caps, {"0C": "confirmed", "05": "confirmed", "0B": "not_supported"})


class Parsers(unittest.TestCase):
    def test_values(self):
        import build_telemetry as B
        got = [(a, b, c, B.T.unit_norm(d)) for a, b, c, d, *_ in B.parse_values("1.6 — 2.9 (%): Idling; 6.4 — 12.8 (%): 2,500 rpm")]
        self.assertEqual(got, [(1.6, 2.9, None, "%"), (6.4, 12.8, None, "%")])
        self.assertEqual([x[:3] for x in B.parse_values("700±100 rpm")], [(600, 800, 700)])
        self.assertEqual([x[:2] for x in B.parse_values("Less than –60.0 kPa (–450 mmHg, –17.72 inHg)")], [(None, -60.0)])
        self.assertEqual(B.parse_values("0 or 5"), [])
        self.assertEqual([x[:2] for x in B.parse_values("85°C - 105°C/190°F - 210°F")], [(85, 105), (190, 210)])

    def test_conditions(self):
        import build_telemetry as B
        self.assertEqual(B.conditions_from("Idling", "After warm-up the engine.")[0][0], "idle_warm")
        self.assertEqual(B.conditions_from("Ignition SW ON (Engine OFF)")[0][0], "koeo")
        self.assertEqual(B.conditions_from("2,500 rpm racing")[0][0], "rpm2500")
        self.assertEqual(B.conditions_from('Idle speed [A/C "ON", no load]'), [])   # A/C state can't be checked
        self.assertFalse(B.checkable("Throttle fully closed."))
        self.assertTrue(B.checkable("Engine idling after warm-up."))


if __name__ == "__main__":
    unittest.main()
