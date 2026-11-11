"""faithfulness: specs, torques, part numbers, prices and labor times must be on a page the model was given, and on
the page it cites. Made-up generic pages, no real car or code."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
import faithfulness as F  # noqa: E402

SRC = ("CASE:\nLive: coolant 88 °C, idle 750 rpm, short trim +4%\n"
       "[FSM: Engine > Intake manifold > Installation, p.212]\n"
       "Tighten the bolts to 25 N·m (18.4 ft-lb). Fuel pressure 284 - 314 kPa.\n"
       "[CHARM: Electrical > Sensor > Resistance]\n| Terminals | Ohms |\n| 1-2 | 0.8 - 1.2 |\n"
       "[CHARM labor] Engine > Water Pump > Replace: 2.3 h, part 21111XX460, $54.20\n")


class T(unittest.TestCase):
    def probs(self, a):
        return " | ".join(F.check(a, SRC)[0])

    def test_sourced_items_pass(self):
        a = ("Torque the bolts to 25 N·m (18 ft-lb). Fuel pressure should be 284-314 kPa. The sensor reads "
             "0.8-1.2 Ω across 1-2. Coolant at 88°C is warm. Book time 2.3 h, part 21111XX460, about $54.20.")
        self.assertEqual(self.probs(a), "")
        self.assertEqual(len(F.check(a, SRC)[1]), 8)

    def test_unit_conversion_and_scale(self):
        self.assertEqual(self.probs("That is 2.5 kgf-m or so."), "")          # 25 N·m converted, rounded
        self.assertEqual(self.probs("Pressure about 41-46 psi."), "")          # 284-314 kPa in psi
        self.assertIn("not in any page", self.probs("It should read 0.8 kΩ."))  # 800 ohms is not 0.8 ohms

    def test_invented_items_flagged(self):
        p = self.probs("Gap 1.1 mm. Bolts 30 ft-lb. Order 19115080 for $12. Labor 4.0 h.")
        for bit in ("1.1 mm", "30 ft-lb", "19115080", "$12", "4.0 h"):
            self.assertIn(bit, p)

    def test_same_number_other_unit_flagged(self):
        self.assertIn("25 mm", self.probs("Use a 25 mm socket."))

    def test_wrong_cited_page(self):
        p = self.probs("Labor is 2.3 h (p.212).")
        self.assertIn("not on the page it cites", p)
        self.assertIn("CHARM labor", p)
        self.assertEqual(self.probs("Torque 25 N·m (p.212)."), "")

    def test_not_part_numbers(self):
        self.assertEqual(self.probs("Code P0420 on a 2004 with 123456 miles, engine code AB12."), "")
        self.assertEqual(self.probs("Codes P0300-P0304, steps 10G6-10G9, connector B134-24, codes 2000-2999."), "")
        self.assertIn("90919-01253", self.probs("Plug 90919-01253."))

    def test_report(self):
        self.assertEqual(F.report("No numbers here.", SRC), "")
        self.assertIn("all on the pages given", F.report("Torque 25 N·m.", SRC))
        self.assertIn("not found", F.report("Torque 99 N·m.", SRC))


if __name__ == "__main__":
    unittest.main()
