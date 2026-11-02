"""app/drive_file.py: drive logs survive a crash and read back.
    ~/subaru_cyberdeck/venv/bin/python -m unittest kb/tests/test_drive_file.py
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "app"))
import drive_file  # noqa: E402


class DriveFile(unittest.TestCase):
    def test_round_trip_and_cut_last_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = drive_file.start("1700000000.25", "2003 Chevrolet Tahoe", d)
            self.assertIn("Chevrolet_Tahoe", p)
            self.assertTrue(drive_file.has("1700000000.25", d))
            self.assertFalse(drive_file.has("1700000100.0", d))
            drive_file.add(p, 2.0, "", "RPM 900 rpm")
            drive_file.add(p, 1.0, "P0300", "RPM 700 rpm")
            with open(p, "a") as f:
                f.write('{"t": 3.0, "co')     # power lost mid-write
            head, rows = drive_file.read(p)
            self.assertEqual(head["car"], "2003 Chevrolet Tahoe")
            self.assertEqual(rows, [(1.0, "P0300", "RPM 700 rpm"), (2.0, "", "RPM 900 rpm")])


if __name__ == "__main__":
    unittest.main()
