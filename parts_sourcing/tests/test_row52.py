"""parts_sourcing.row52 (search links a person opens, ids learned from pasted links) and interchange.donors."""
import json
import os
import tempfile
import unittest

from parts_sourcing import row52
from parts_sourcing.interchange import Fit, Interchange, donors


class TestRow52(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "ids.json")
        with open(self.path, "w") as f:
            json.dump({"makes": {"Subaru": 226}, "models": {"Subaru": {"Forester": 4149}}}, f)
        self.t = row52.ids(self.path)

    def test_search_url(self):
        u = row52.search_url("Subaru", "98155", [2002, 2005, 2008], "Forester", table=self.t)
        self.assertIn("Year=2002-2008", u)
        self.assertIn("ModelId=4149", u)
        self.assertIn("MakeId=226", u)
        u = row52.search_url("Subaru", "98155", [2003], "Baja", table=self.t)
        self.assertIn("ModelId=&", u)
        self.assertIn("Year=2003&", u)
        self.assertIsNone(row52.search_url("Honda", "98155", [2004], table=self.t))

    def test_learn(self):
        got = row52.learn("https://row52.com/Search/?YMMorVin=YMM&Year=2004&MakeId=12&ModelId=345&V1=J", "Honda",
                          "Civic", path=self.path)
        self.assertEqual(got, {"make": "Honda", "MakeId": 12, "model": "Civic", "ModelId": 345})
        self.assertIn("ModelId=345", row52.search_url("Honda", "1", [2004], "Civic", table=row52.ids(self.path)))
        with self.assertRaises(ValueError):
            row52.learn("https://example.com/?MakeId=1", "X", path=self.path)
        with self.assertRaises(ValueError):
            row52.learn("https://row52.com/Search/?ModelId=1", "X", path=self.path)

    def test_donors(self):
        ic = Interchange("1", "1", fits=[Fit("Forester", 2002, "F4-2.5L SOHC", "1", "exact"),
                                         Fit("Forester", 2003, "F4-2.5L SOHC", "1", "exact"),
                                         Fit("Outback", 2004, "H6-3.0L", "1", "family")])
        d = donors(ic)
        self.assertEqual([x["model"] for x in d], ["Forester", "Outback"])
        self.assertEqual(d[0]["years_text"], "2002-2003")


if __name__ == "__main__":
    unittest.main()
