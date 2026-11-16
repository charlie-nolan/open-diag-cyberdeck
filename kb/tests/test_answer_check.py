"""app/answer_check.py: ISSUE 1 vs the case's verified-good / replaced parts and the car's manual."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app"))
import answer_check as A  # noqa: E402


class R:
    def mentions(self, phrase, min_words=1):
        return 0 if "turbocharger" in phrase.lower() else 5


class TestAnswerCheck(unittest.TestCase):
    def test_flags(self):
        case = {"ruled_out": "Thermostat is new (6 months). Fans run.", "history": "Radiator replaced last year."}
        self.assertTrue(A.check([{"name": "Thermostat stuck", "PARTS": "thermostat"}], case, R()))
        self.assertTrue(A.check([{"name": "Radiator clogged", "PARTS": "radiator"}], case, R()))
        self.assertTrue(A.check([{"name": "Turbo", "PARTS": "turbocharger"}], case, R()))

    def test_clean(self):
        case = {"ruled_out": "Fans run.", "history": "Coolant flushed."}
        self.assertEqual(A.check([{"name": "Head gasket", "PARTS": "head gasket, coolant"}], case, R()), [])
        self.assertEqual(A.check([], case, R()), [])


if __name__ == "__main__":
    unittest.main()
