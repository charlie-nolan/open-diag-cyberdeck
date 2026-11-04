"""NHTSA flat files are read by field position (build_subaru_diag_db.iter_flat_rows, also used by
kb/add_nhtsa.py and chevrolet/make_pipeline.py). A file whose columns moved must be skipped, not loaded.
    python -m pytest kb/tests/test_nhtsa_layout.py
Synthetic rows in a temp folder; no data files needed.
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, ROOT)
import build_subaru_diag_db as bd  # noqa: E402


def tsb_row(i, year="2004", make="EXAMPLEMAKE", date="20050117"):
    r = [str(i), "", "20050120", f"TSB-{i}", date, "", "TSB", make, "MODELX", year, "ENGINE", "", "", f"summary {i}"]
    return "\t".join(r)


def recall_row(i, year="1999", make="EXAMPLEMAKE"):
    r = [str(i), f"99V{i:03d}000", make, "MODELX", year] + [""] * 14 + ["defect", "consequence", "remedy"]
    return "\t".join(r)


class Layout(unittest.TestCase):
    def folder(self, kind, files):
        d = os.path.join(tempfile.mkdtemp(prefix="nhtsa-"), kind)
        os.makedirs(d)
        for name, lines in files.items():
            with zipfile.ZipFile(os.path.join(d, name), "w") as z:
                z.writestr(name.replace(".zip", ".txt"), "\n".join(lines) + "\n")
        return d

    def read(self, d):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rows = list(bd.iter_flat_rows(d))
        return rows, err.getvalue()

    def test_good_file_streams_every_row(self):
        d = self.folder("tsbs", {"A.zip": [tsb_row(i) for i in range(500)]})
        rows, err = self.read(d)
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[0][9], "2004")
        self.assertEqual(err, "")

    def test_moved_column_skips_that_file_only(self):
        shifted = ["X\t" + tsb_row(i) for i in range(300)]          # one new leading column
        d = self.folder("tsbs", {"A.zip": [tsb_row(i) for i in range(50)], "B.zip": shifted})
        rows, err = self.read(d)
        self.assertEqual(len(rows), 50)
        self.assertIn("SKIPPED B.zip", err)
        self.assertIn("field 9 (model year)", err)

    def test_date_where_year_expected(self):
        d = self.folder("tsbs", {"A.zip": [tsb_row(i, year="20050117") for i in range(10)]})
        rows, err = self.read(d)
        self.assertEqual(rows, [])
        self.assertIn("'20050117'", err)

    def test_bad_date_field(self):
        d = self.folder("tsbs", {"A.zip": [tsb_row(i, date="ENGINE") for i in range(10)]})
        rows, err = self.read(d)
        self.assertEqual(rows, [])
        self.assertIn("communication date", err)

    def test_too_few_fields(self):
        d = self.folder("rcl", {"A.zip": ["a\tb\tc"] * 10})
        rows, err = self.read(d)
        self.assertEqual(rows, [])
        self.assertIn("no row has the 22 fields", err)

    def test_a_few_odd_rows_tolerated(self):
        lines = [recall_row(i) for i in range(200)]
        lines[5] = recall_row(5, year="UNKNOWN")                     # 1 bad row of 200 is under the 5 % limit
        d = self.folder("rcl", {"A.zip": lines})
        rows, err = self.read(d)
        self.assertEqual(len(rows), 200)
        self.assertEqual(err, "")

    def test_unknown_year_9999_is_a_year(self):
        d = self.folder("rcl", {"A.zip": [recall_row(i, year="9999") for i in range(20)]})
        self.assertEqual(len(self.read(d)[0]), 20)


if __name__ == "__main__":
    unittest.main()
