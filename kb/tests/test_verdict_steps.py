"""app/verdict_steps.py: hard stop, parsing, ranking (no model)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
import verdict_steps as V  # noqa: E402


class TestSteps(unittest.TestCase):
    def test_hard_stop(self):
        self.assertEqual(V.hard_stop([{"level": "note", "kind": "code", "item": "P0141", "reason": "x", "source": "s"}]), "")
        self.assertEqual(V.hard_stop([{"level": "flag", "kind": "equipment", "item": "cvt", "reason": "x", "source": "s"}]), "")
        t = V.hard_stop([{"level": "flag", "kind": "live", "item": "RPM 0 with speed 140 km/h", "reason": "r", "source": "s"}])
        self.assertTrue(t.startswith("NEEDS MORE DATA"))
        self.assertIn("TEST:", t)

    def test_parse_and_rank(self):
        c = V.parse_candidates("1. CAUSE: Vacuum leak | FROM: LTFT +18 %\nCAUSE: MAF sensor | FROM: step 4\nnoise")
        self.assertEqual([x["name"] for x in c], ["Vacuum leak", "MAF sensor"])
        c[0].update(V.parse_check("FITS: yes - trims\nAGAINST: no - none\nMANUAL: yes - step 2\nTEST: quick - smoke"))
        c[1].update(V.parse_check("FITS: partly - lean\nAGAINST: no\nMANUAL: yes - step 4\nTEST: moderate - scope"))
        g = V.opinion_candidates("Suspect / known defects (the owner's idea): MAF sensor")
        g[0].update(V.parse_check("FITS: partly - x\nAGAINST: no\nMANUAL: no - none\nTEST: quick - clean"))
        r = V.rank(c + g)
        self.assertEqual(r[0]["name"], "Vacuum leak")
        self.assertTrue(r[-1]["guess"])
        self.assertTrue(c[0]["parsed"])

    def test_tie_break_cheaper_first(self):
        a = {"name": "A", "fits": "yes", "manual": "no", "against": "no", "cost": "major"}
        b = {"name": "B", "fits": "yes", "manual": "no", "against": "no", "cost": "quick"}
        self.assertEqual(V.rank([a, b])[0]["name"], "B")

    def test_against_drops(self):
        a = {"name": "A", "fits": "yes", "manual": "yes", "against": "yes", "cost": "quick"}
        b = {"name": "B", "fits": "partly", "manual": "no", "against": "no", "cost": "quick"}
        self.assertEqual(V.rank([a, b])[0]["name"], "B")


if __name__ == "__main__":
    unittest.main()
