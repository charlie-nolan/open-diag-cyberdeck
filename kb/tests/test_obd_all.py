"""app/obd_all.py against simulated ELM adapters on a pty, through the real python-obd.
Mixed cars: ISO 9141 4-cyl, J1850 VPW V8, CAN V6 with wideband O2 + transmission ECU + PIDs past 5F. Values are made up.
    ~/subaru_cyberdeck/venv/bin/python -m unittest kb/tests/test_obd_all.py
"""
import os
import pty
import select
import sys
import tempfile
import threading
import time
import tty
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "app"))
import live_data  # noqa: E402
import obd  # noqa: E402
import obd_all  # noqa: E402
import telemetry_ref as T  # noqa: E402

obd.logger.setLevel(100)


def bitmap(pids, base):
    bits = 0
    for p in pids:
        if base < p <= base + 32:
            bits |= 1 << (base + 32 - p)
    return bits.to_bytes(4, "big")


class Fake:
    """An ELM327 that answers with ATH1 frames. values: {pid: data bytes}; other: a second ECU's values."""

    def __init__(self, proto, values, other=None, raw06=None, can06=None, search_s=0):
        self.search_s, self.searching = search_s, False
        self.auto_fails, self.direct = False, None
        self.proto, self.values, self.other, self.raw06, self.can06 = proto, values, other or {}, raw06 or {}, can06
        self.sent = []
        self.master, slave = pty.openpty()
        tty.setraw(slave)
        self.port = os.ttyname(slave)
        self.stop = False
        threading.Thread(target=self.loop, daemon=True).start()

    def frames(self, pid, data, tx):
        d = bytes((0x41, pid)) + data
        if self.proto == "6":
            if len(d) <= 7:
                f = bytes((len(d),)) + d
                return [f"7E{8 + tx} " + (f + b"\xAA" * (8 - len(f))).hex(" ").upper()]
            out = [f"7E{8 + tx} " + (bytes((0x10, len(d))) + d[:6]).hex(" ").upper()]
            rest, n = d[6:], 1
            while rest:
                c = bytes((0x20 | n,)) + rest[:7]
                out.append(f"7E{8 + tx} " + (c + b"\xAA" * (8 - len(c))).hex(" ").upper())
                rest, n = rest[7:], n + 1
            return out
        return [(bytes((0x48, 0x6B, 0x10 + tx)) + d + b"\x00").hex(" ").upper()]

    def answer(self, c):
        self.sent.append(c)
        u = c.replace(" ", "").upper()
        if u == "ATZ":
            return "ELM327 v1.4b"
        if u == "ATRV":
            return "12.6V"
        if u == "ATDPN":
            return "A" + self.proto if self.direct is None else self.proto
        if u == "ATSP0":
            self.searching, self.direct = True, None
        if u.startswith("ATTP"):
            self.direct = u[4:]
        if u.startswith("AT"):
            return "OK"
        if u.startswith("06"):
            if self.can06 and u == "0600":
                return "7E8 06 46 00 80 00 00 00 AA"
            if self.can06 and u == "0601":
                return self.can06
            return self.raw06.get(u, "NO DATA")
        if u.startswith("03"):
            return "NO DATA" if self.proto != "6" else "7E8 02 43 00 AA AA AA AA AA"
        if not u.startswith("01"):
            return "NO DATA"
        if self.direct not in (None, self.proto):   # what the real truck replied on each wrong protocol
            return {"1": "NO DATA", "3": "BUS INIT: ...ERROR", "4": "BUS INIT: ...ERROR",
                    "5": "BUS INIT: ERROR"}.get(self.direct, "CAN ERROR")
        if self.searching and self.auto_fails:
            self.searching = False
            return "SEARCHING...\rUNABLE TO CONNECT"
        if self.searching:              # a real adapter prints this, then stays silent while it tries each bus
            self.searching = False
            os.write(self.master, b"SEARCHING...")
            if select.select([self.master], [], [], self.search_s)[0]:
                return "\rSTOPPED"     # any byte received interrupts an ELM's search
        pid = int(u[2:4], 16)
        out = []
        for tx, vals in enumerate((self.values, self.other)):
            pids = vals.keys()
            if pid % 32 == 0 and vals:
                more = any(p > pid + 32 for p in pids)
                b = bytearray(bitmap(pids, pid))
                if more:
                    b[3] |= 1
                if pid == 0 or any(p > pid for p in pids):
                    out += self.frames(pid, bytes(b), tx)
            elif pid in vals:
                out += self.frames(pid, vals[pid], tx)
        return "\r".join(out) or "NO DATA"

    def loop(self):
        buf = b""
        while not self.stop:
            if not select.select([self.master], [], [], 0.05)[0]:
                continue
            buf += os.read(self.master, 256)
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                line = line.replace(b"\x7f", b"").replace(b"\n", b"").decode().strip()
                os.write(self.master, (self.answer(line) + "\r\r>").encode() if line else b"?\r\r>")

    def connect(self):
        return obd.OBD(self.port, fast=False, timeout=2, check_voltage=False)


def core(extra):
    v = {0x01: b"\x82\x07\x65\x00", 0x03: b"\x02\x00", 0x04: b"\x40", 0x05: b"\x7D", 0x06: b"\x85", 0x07: b"\x88",
         0x0B: b"\x21", 0x0C: b"\x0B\xB8", 0x0D: b"\x00", 0x0E: b"\x90", 0x0F: b"\x46", 0x11: b"\x20"}
    v.update(extra)
    return v


class Cars(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        obd_all.PROTOCOL_FILE = os.path.join(self.tmp.name, "proto")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, fake):
        c = fake.connect()
        self.assertTrue(c.is_connected())
        got = obd_all.read_all(c)
        m = obd_all.mode06(c)
        c.close()
        fake.stop = True
        for s in fake.sent:   # read-only: only AT, 01, 03, 06, 09
            self.assertTrue(s.replace(" ", "").upper()[:2] in ("AT", "01", "03", "06", "09") or "\x7f" in s, s)
        return got, m

    def test_iso9141_narrowband(self):
        v = core({0x13: b"\x03", 0x14: b"\x90\x85", 0x15: b"\x46\xFF", 0x1C: b"\x06"})
        raw = {"0600": "48 6B 10 46 00 01 80 00 00 00 00", "0601": "48 6B 10 46 01 01 00 10 00 20 00\r48 6B 10 46 01 02 00 05 00 04 00"}
        got, (lines, failed, kind) = self.read(Fake("3", v, raw06=raw))
        line = got["line"]
        self.assertIn("O2 B1S1 0.72 V", line)
        self.assertIn("O2 B1S2 0.35 V", line)
        self.assertIn("O2 B1S1 trim 3.9 %", line)
        self.assertNotIn("O2 B1S2 trim", line)
        self.assertIn("Fuel sys closed loop", line)
        self.assertIn("MIL on", line)
        self.assertIn("Stored DTCs 2", line)
        self.assertIn("Volts 12.6 V", line)
        self.assertLess(line.index("RPM"), line.index("O2 B1S1"))
        self.assertEqual(kind, "raw")
        self.assertEqual(len(lines), 3)
        self.assertEqual(failed, [])
        self.assertAlmostEqual(live_data.parse(line)["o2"], 0.72)
        self.assertAlmostEqual(live_data.parse(line)["o2r"], 0.35)
        keys = {r["key"] for r in T.normalize(T.from_live_text(line, "obd01"), "obd01")}
        self.assertTrue({"rpm", "ect", "o2_b1s1_v", "o2_b1s2_v", "stft1"} <= keys, keys)

    def test_vpw_v8_two_banks_alt_map(self):
        v = core({0x08: b"\x70", 0x09: b"\x80", 0x1D: b"\x33", 0x14: b"\x50\xFF", 0x15: b"\x20\xFF",
                  0x18: b"\x60\xFF", 0x19: b"\x30\xFF", 0x2F: b"\x80"})
        got, _ = self.read(Fake("2", v))
        line = got["line"]
        self.assertIn("O2 B3S1 0.48 V", line)   # PID 18 with PID 1D's layout
        self.assertIn("STFT2 -12.5 %", line)
        self.assertEqual(live_data.parse(line)["stft2"], -12.5)
        self.assertIn("Fuel level", line)
        v[0x13] = b"\x33"
        del v[0x1D]
        got, _ = self.read(Fake("2", v))
        self.assertIn("O2 B2S1 0.48 V", got["line"])   # same PID, PID 13's layout
        self.assertNotIn("PID 13", got["line"])

    def test_can_wideband_tcm_and_high_pids(self):
        v = core({0x13: b"\x03", 0x15: b"\x46\xFF", 0x24: b"\x7F\xFF\x66\x66", 0x34: b"\x80\x00\x80\x80",
                  0x32: b"\xFF\x00", 0x42: b"\x37\x6E", 0x67: b"\x03\x7D\x7E", 0xA6: b"\x00\x01\xE2\x40",
                  0x7A: b"\x01\x02\x03\x04\x05\x06\x07"})
        tcm = {0x0C: b"\x01\x01", 0x5C: b"\x70"}
        got, _ = self.read(Fake("6", v, tcm))
        line = got["line"]
        self.assertIn("RPM 750", line)          # the engine's reply, not the transmission's
        self.assertIn("Oil temp 72 °C", line)  # only the transmission ECU had it
        self.assertIn("O2 B1S1 lambda 1.0", line)
        self.assertIn("O2 B1S1 WR 3.2 V", line)
        self.assertIn("O2 B1S1 current 0.5 mA", line)
        self.assertIn("Evap pressure -64", line)
        self.assertIn("Engine temp 2 86 °C", line)
        self.assertIn("Odometer 12345.6 km", line)
        self.assertIn("PID 7A 01020304", line)  # no formula: raw, no guess
        self.assertIn("ECU volts", line)
        self.assertEqual(got["silent"], [])
        self.assertIn("A6", got["supported"])
        p = live_data.for_prompt(line, 200)
        self.assertLess(len(p), 240)
        self.assertNotIn("PID 7A", p)
        self.assertIn("RPM", p)

    def test_pid4f_scale_blocks_wideband_formula(self):
        v = core({0x4F: b"\x04\x10\x00\x00", 0x24: b"\x7F\xFF\x66\x66"})
        got, _ = self.read(Fake("6", v))
        self.assertNotIn("lambda", got["line"])
        self.assertIn("PID 24 7FFF6666", got["line"])

    def test_silent_pid_reported(self):
        v = core({})
        f = Fake("3", v)
        real = f.frames
        f.frames = lambda pid, data, tx: [] if pid == 0x0F else real(pid, data, tx)
        got, _ = self.read(f)
        self.assertEqual(got["silent"], ["0F"])

    def test_slow_search_then_protocol_reused(self):
        f = Fake("2", core({}), search_s=4)     # longer than python-obd's old 2 s timeout
        c, why = obd_all.connect(f.port)
        self.assertIsNotNone(c, why)
        c.close()
        n = len(f.sent)
        c, _ = obd_all.connect(f.port)
        self.assertIsNotNone(c)
        self.assertIn("ATTP2", [x.replace(" ", "") for x in f.sent[n:]])
        self.assertNotIn("ATSP0", [x.replace(" ", "") for x in f.sent[n:]])
        self.assertIn("RPM 750", obd_all.read_all(c)["line"])
        c.close()
        f.stop = True

    def test_auto_search_fails_direct_protocol_answers(self):
        for proto in ("2", "3", "6"):             # J1850 VPW, ISO 9141, CAN: the search says UNABLE on each
            f = Fake(proto, core({}))
            f.auto_fails = True
            c, why = obd_all.connect(f.port)
            self.assertIsNotNone(c, why)
            self.assertEqual(c.protocol_id(), proto)
            self.assertIn("RPM 750", obd_all.read_all(c)["line"])
            c.close()
            n = len(f.sent)
            c, _ = obd_all.connect(f.port)        # later reads go straight to it
            self.assertEqual([x.replace(" ", "") for x in f.sent[n:]].count("ATSP0"), 0)
            c.close()
            f.stop = True
            os.remove(obd_all.PROTOCOL_FILE)

    def test_wrong_saved_protocol_is_replaced(self):
        obd_all._save_protocol("6")               # 'CAN ERROR' looks like a reply to python-obd
        f = Fake("2", core({}))
        f.auto_fails = True
        c, why = obd_all.connect(f.port)
        self.assertIsNotNone(c, why)
        self.assertEqual(obd_all._last_protocol(), "2")
        self.assertIn("RPM 750", obd_all.read_all(c)["line"])
        c.close()
        f.stop = True

    def test_stale_protocol_falls_back(self):
        obd_all._save_protocol("3")               # the last car was K-line; this one is CAN
        f = Fake("6", core({}))
        c, why = obd_all.connect(f.port)
        self.assertIsNotNone(c, why)
        self.assertEqual(obd_all._last_protocol(), "6")
        c.close()
        f.stop = True

    def test_silent_car_reports_why(self):
        f = Fake("2", {})
        f.answer = lambda c: "OK" if c.upper().startswith("AT") and "RV" not in c.upper() and "DPN" not in c.upper() \
            else "12.6V" if "RV" in c.upper() else "A0" if "DPN" in c.upper() else "ELM327 v1.4b" if c == "ATZ" \
            else "UNABLE TO CONNECT"
        c, why = obd_all.connect(f.port)
        self.assertIsNone(c)
        self.assertEqual(why, "OBD Connected")
        f.stop = True

    def test_ssm_only_where_it_can_exist(self):
        for make, proto, want in (("Subaru", "ISO 9141-2", True), ("", "ISO 9141-2", True), ("", "", True),
                                  ("", "ISO 14230-4 (KWP FAST)", True), ("", "SAE J1850 VPW", False),
                                  ("", "SAE J1850 PWM", False), ("Chevrolet", "SAE J1850 VPW", False),
                                  ("Toyota", "ISO 9141-2", False), ("Ford", "ISO 15765-4 (CAN 11/500)", False)):
            self.assertEqual(obd_all.may_speak_ssm(make, proto), want, (make, proto))

    def test_refuses_writes(self):
        for cmd in ("04", "2E0101", "27", "3B01", "14"):
            with self.assertRaises(ValueError):
                obd_all.ask(None, cmd)


if __name__ == "__main__":
    unittest.main()
