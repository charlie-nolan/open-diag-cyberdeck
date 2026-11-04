"""app/live_data.parse() converts a reading to the unit it is judged in, and drops a reading whose unit
belongs to another quantity, so 190 °F coolant is never called overheating.
    python -m pytest kb/tests/test_live_units.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "app"))
import live_data as L  # noqa: E402


class Units(unittest.TestCase):
    def test_canonical_units_unchanged(self):
        v = L.parse("ECT 12 C | STFT1 -3.9 percent | MAP 30 kPa | Speed 40 km/h | Volts 14.1 V | Timing 12 °")
        self.assertEqual(v, {"ect": 12, "stft": -3.9, "map": 30, "speed": 40, "volts": 14.1, "timing": 12})

    def test_no_unit_means_judged_unit(self):
        self.assertEqual(L.parse("LTFT +12%, idle 650, coolant: 90"), {"ltft": 12, "rpm": 650, "ect": 90})

    def test_fahrenheit_converted(self):
        for text in ("ECT 190 F", "Coolant 190 °F", "ECT 190 degF", "ECT 190 fahrenheit"):
            self.assertAlmostEqual(L.parse(text)["ect"], 87.78, places=2, msg=text)

    def test_190F_is_not_overheating(self):
        lines = L.explain("ECT 190 F")
        self.assertTrue(lines[0].startswith("Coolant 88 C: normal operating temperature"), lines)
        self.assertNotIn("overheating", " ".join(lines))

    def test_other_units_converted(self):
        self.assertAlmostEqual(L.parse("MAP 5 psi")["map"], 34.47, places=2)
        self.assertAlmostEqual(L.parse("Speed 30 mph")["speed"], 48.28, places=2)
        self.assertAlmostEqual(L.parse("O2 B1S1 450 mV")["o2"], 0.45)
        self.assertAlmostEqual(L.parse("ECT 88 deg C")["ect"], 88)

    def test_wrong_quantity_dropped(self):
        self.assertEqual(L.parse("ECT 12 V"), {})
        self.assertEqual(L.parse("TPS 0.6 V"), {})          # a sensor voltage is not throttle percent
        self.assertEqual(L.parse("MAP 30 %, RPM 700"), {"rpm": 700})

    def test_words_after_number_are_not_units(self):
        self.assertEqual(L.parse("ECT 85 C (warm)"), {"ect": 85})
        self.assertEqual(L.parse("RPM 650 at idle"), {"rpm": 650})
        self.assertEqual(L.parse("O2 B1S1 lambda 0.98 ratio"), {"lam": 0.98})

    def test_drive_summary_uses_converted_values(self):
        s = L.drive_summary([(0, "", "ECT 150 F | RPM 700 | Speed 0 mph"), (30, "", "ECT 190 F | RPM 700 | Speed 0 mph")])
        self.assertIn("Coolant 66 to 88 C", s)


if __name__ == "__main__":
    unittest.main()
