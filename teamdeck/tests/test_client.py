"""teamdeck client: what may leave the deck. venv/bin/python -m unittest discover -s teamdeck/tests"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from teamdeck import client  # noqa: E402

VIN = "1ABCD23EFGH456789"     # made-up, VIN-shaped


class Scrub(unittest.TestCase):
    def test_drops_vin_customer_miles(self):
        out = client.scrub({"vehicle_info": {"year": "2001", "make": "Make", "model": "Model", "vin": VIN,
                                             "customer": "someone", "miles": "1000", "trans": "Manual"},
                            "dtcs": "P0000", "chat_notes": f"car {VIN} again"})
        s = json.dumps(out)
        self.assertNotIn(VIN, s)
        self.assertNotIn("someone", s)
        self.assertNotIn("1000", s)
        self.assertEqual(out["vehicle_info"], {"year": 2001, "make": "Make", "model": "Model",
                                               "transmission": "Manual"})

    def test_earlier_visits_only_when_asked(self):
        c = {"vehicle_info": {"make": "Make"}, "earlier_visits": "x", "past_readings": "y"}
        self.assertNotIn("earlier_visits", client.scrub(c))
        self.assertEqual(client.scrub(c, earlier=True)["past_readings"], "y")

    def test_deck_case_read_only(self):
        p = tempfile.mktemp(suffix=".db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE session_state (key TEXT PRIMARY KEY, value TEXT)")
        c.executemany("INSERT INTO session_state VALUES (?,?)",
                      [("vehicle_info", json.dumps({"make": "Make", "vin": VIN})), ("dtcs", "P0000")])
        c.commit()
        c.close()
        before = os.path.getmtime(p)
        case = client.deck_case(p)
        self.assertEqual(case["dtcs"], "P0000")
        self.assertNotIn("vin", case["vehicle_info"])
        self.assertEqual(os.path.getmtime(p), before)

    def test_board_only(self):
        self.assertEqual(client.board_only("BOARD:\n- a\n\nLOOKUPS (logged by the deck, x):\n- y"), "BOARD:\n- a")

    def test_unreachable_service_is_an_error_not_a_crash(self):
        old = client.SERVICE
        client.SERVICE = "http://127.0.0.1:9"
        try:
            self.assertIn("error", client.health())
        finally:
            client.SERVICE = old


if __name__ == "__main__":
    unittest.main()
