"""
Tests for kb/build_community.py (quote checks, tables, counts) and app/community_ref.py.
    ~/subaru_cyberdeck/venv/bin/python -m unittest discover -s ~/subaru_cyberdeck/kb/tests -v

The thread is synthetic and its codes are drawn at random, so no car, code or cause is special.
"""
import json
import os
import random
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "kb"))
import build_community as B  # noqa: E402
import community_ref as R  # noqa: E402

RND = random.Random(7)
CODE = f"{RND.choice('PCBU')}{RND.randint(0, 3)}{RND.randint(100, 999)}"
PART = RND.choice(["widget sensor", "flux valve", "sprocket relay", "gizmo harness"])


def thread():
    segs = [{"k": "op", "s": None, "t": f"Light came on, code {CODE}\n\nMy 2011 Examplecar with 120k miles stalls "
                                        f"when warm at idle and throws {CODE} after a few minutes."},
            {"k": "reply", "s": 40, "t": f"Check the {PART} first. Measure it with a multimeter, it should read "
                                         f"about 4.5 volts with the key on."},
            {"k": "op_reply", "s": 3, "t": f"Measured the {PART} at 1.2 volts key on. Replaced the {PART} and that "
                                           f"fixed it, no more stalling after two weeks."}]
    return segs


def model_reply(segs):
    return {
        "relevant": True,
        "vehicle": {"model": "Examplecar", "year": "2011", "engine": "", "transmission": "", "mileage": "120k",
                    "seg": 0, "quote": "My 2011 Examplecar with 120k miles"},
        "codes": [{"code": CODE, "seg": 0, "quote": f"throws {CODE} after a few minutes"},
                  {"code": "P0000", "seg": 0, "quote": "invented words that are not in the thread"}],
        "symptoms": [{"symptom": "stalls when warm", "condition": "warm, idle", "seg": 0,
                      "quote": "stalls when warm at idle"}],
        "tests": [{"test": "voltage check", "tool": "multimeter", "component": PART, "result": "1.2 volts key on",
                   "status": "ran", "seg": 1, "quote": f"Measured the {PART} at 1.2 volts key on"},   # wrong seg
                  {"test": "voltage check", "tool": "multimeter", "component": PART, "result": "9.9 volts",
                   "status": "ran", "seg": 2, "quote": f"Measured the {PART} at 1.2 volts key on"}],  # bad number
        "ruled_out": [],
        "suggestions": [{"component": PART, "reason": "first thing to check", "seg": 1,
                         "quote": f"Check the {PART} first"}],
        "outcome": {"fix": f"replaced {PART}", "component": PART, "status": "confirmed", "came_back": "no",
                    "seg": 2, "quote": f"Replaced the {PART} and that fixed it"},
        "chains": [{"steps": [{"role": "test", "seg": 2, "quote": f"Measured the {PART} at 1.2 volts"},
                              {"role": "result", "seg": 2, "quote": f"Replaced the {PART} and that fixed it"}]}],
        "refs": [],
    }


class TestCommunity(unittest.TestCase):
    def test_validate(self):
        segs = thread()
        rec, ck = B.validate(model_reply(segs), segs, p90=10)
        self.assertEqual([c["code"] for c in rec["codes"]], [CODE])          # invented quote dropped
        self.assertEqual(rec["codes"][0]["op_had"], 1)
        self.assertEqual(rec["tests"][0]["seg"], 2)                          # Python moved it to the right segment
        self.assertEqual(rec["tests"][0]["who"], "op")
        self.assertEqual(rec["tests"][1]["result"], "")                      # 9.9 isn't in the quote
        self.assertEqual(rec["suggestions"][0]["who"], "reply")
        self.assertEqual(rec["suggestions"][0]["high_score"], 1)
        self.assertEqual(rec["outcome"]["op_confirmed"], 1)
        self.assertEqual(rec["vehicle"]["year"], "2011")
        self.assertEqual(len(rec["chains"]), 1)
        self.assertGreaterEqual(ck["quote_fail"], 1)
        self.assertGreaterEqual(ck["seg_fixed"], 1)

    def test_confirmed_needs_op(self):
        segs = thread()
        raw = model_reply(segs)
        raw["outcome"].update(seg=1, quote=f"Check the {PART} first")
        rec, _ = B.validate(raw, segs, p90=10)
        self.assertEqual(rec["outcome"]["status"], "claimed")
        self.assertEqual(rec["outcome"]["op_confirmed"], 0)

    def test_tables_and_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            B.KB, B.OUT, B.WORK = d, os.path.join(d, "community"), os.path.join(d, "community", "work")
            B.LOG = os.path.join(d, "log.txt")
            w = B.work_db("testmake")
            for i in range(3):
                segs = thread()
                rec, ck = B.validate(model_reply(segs), segs, p90=10)
                w.execute("INSERT INTO thread(src, tid, sub, url, title, n_replies, segs, tags) VALUES (?,?,?,?,?,?,?,?)",
                          ("r", str(i), "s", f"https://example.invalid/{i}", "t", 2, json.dumps(segs), "{}"))
                w.execute("INSERT INTO extract(src, tid, status, rec, checks, llm_model) VALUES (?,?,?,?,?,?)",
                          ("r", str(i), "ok", json.dumps(rec), json.dumps(ck), "fake"))
            w.commit()
            B.step_tables("testmake", types.SimpleNamespace(pilot=False, vec=False))
            c = sqlite3.connect(B.cdb_path("testmake"))
            n = c.execute("SELECT threads, op_confirmed FROM comm_count WHERE code=? AND model IS NULL", (CODE,)).fetchall()
            self.assertIn((3, 3), n)
            r = R.community(B.cdb_path("testmake"), code=CODE, model="Examplecar", year=2012)
            self.assertEqual(r["threads"], 3)
            self.assertEqual(r["causes"][0]["op_confirmed"], 3)
            self.assertEqual(r["causes"][0]["car_match"], "same model and years")
            r2 = R.community(B.cdb_path("testmake"), text="stalls when warm", model="Othercar")
            self.assertEqual(r2["threads"], 3)
            self.assertEqual(r2["causes"][0]["car_match"], "other or unknown model")


if __name__ == "__main__":
    unittest.main()


class TestLite(unittest.TestCase):
    def test_code_fill_and_trim(self):
        segs = thread()
        segs.append({"k": "reply", "s": 1, "t": "> quoted earlier text\nok"})
        codes, refs, veh = B.code_fill(segs, "no_such_make")      # no make DB: no model/engine list, nothing guessed
        self.assertEqual([c["code"] for c in codes], [CODE])
        self.assertTrue(all(B.quote_in(c["quote"], segs[c["seg"]]["t"]) for c in codes))
        self.assertNotIn("model", veh)
        self.assertEqual(veh.get("mileage"), "120k miles")
        self.assertEqual(B.trim(segs).keys(), {0, 1, 2})          # the quote-only reply is left out
        self.assertNotIn("S3", B.thread_text(segs, B.trim(segs)))
