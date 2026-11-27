import json
import os
import sqlite3
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import ingest_youtube as yt

class TestYoutubeIngest(unittest.TestCase):
    def test_spoken_dtc_normalization(self):
        self.assertEqual(yt.normalize_spoken_dtcs("We pulled code P 0 3 0 3 and PO420"), "We pulled code P0303 and P0420")
        self.assertEqual(yt.normalize_spoken_dtcs("Normal P0171 stays intact"), "Normal P0171 stays intact")

    def test_make_detection(self):
        self.assertEqual(yt.detect_make("2003 Chevy Tahoe P0300 Misfire Diagnosis"), "chevrolet")
        self.assertEqual(yt.detect_make("Toyota Camry P0420 Cat Efficiency Test"), "toyota")
        self.assertEqual(yt.detect_make("2008 F-150 5.4L Cam Phaser Timing"), "ford")
        self.assertEqual(yt.detect_make("Generic Oscilloscope Compression Test"), "general")

    def test_quality_gate_accepts_good_video(self):
        meta = {
            "id": "abc123",
            "title": "2005 Honda Accord P0171 Lean Condition Diagnosis & Fix",
            "duration": 920,
            "view_count": 50000,
            "like_count": 1800,
            "comments": [
                {"text": "Thank you! Saved me $400, fixed my cracked intake boot!", "like_count": 42},
                {"text": "Exact same problem on my 06 Accord, spot on diagnosis.", "like_count": 18},
            ],
        }
        acc, reason, metrics = yt.evaluate_video(meta)
        self.assertTrue(acc, reason)
        self.assertEqual(metrics["pos_comments"], 2)
        self.assertEqual(metrics["fix_confirmations"], 2)
        self.assertEqual(metrics["neg_comments"], 0)
        self.assertGreater(metrics["quality_score"], 10.0)

    def test_strong_fix_comments_rescue_viral_low_like_ratio(self):
        viral = {
            "id": "viral1",
            "title": "GM 5.3L P0171 P0174 Intake Gasket Fix",
            "duration": 620,
            "view_count": 500000,
            "like_count": 3200,
            "comments": [
                {"text": "Thank you!! Finally fixed my 2004 Tahoe, code is gone!", "like_count": 90},
                {"text": "This fixed it! Passed smog today after 3 months of chasing this.", "like_count": 35},
            ],
        }
        acc, reason, metrics = yt.evaluate_video(viral)
        self.assertTrue(acc, reason)
        self.assertEqual(metrics["fix_confirmations"], 2)

    def test_quality_gate_rejects_clickbait_and_shorts(self):
        bad_short = {"title": "P0300 fix!", "duration": 45, "view_count": 10000, "like_count": 500}
        acc, reason, _ = yt.evaluate_video(bad_short)
        self.assertFalse(acc)
        self.assertIn("too_short", reason)

        bad_ratio_neg = {
            "title": "Top 5 P0420 Fixes",
            "duration": 400,
            "view_count": 100000,
            "like_count": 400,
            "comments": [
                {"text": "Total clickbait AI slop, waste of time", "like_count": 50},
                {"text": "Didn't work at all, terrible advice", "like_count": 20},
            ],
        }
        acc2, reason2, _ = yt.evaluate_video(bad_ratio_neg)
        self.assertFalse(acc2)
        self.assertIn("low_like_ratio", reason2)
        self.assertIn("neg_comments", reason2)

    def test_json3_chunking_and_work_export(self):
        with tempfile.TemporaryDirectory() as td:
            cap = os.path.join(td, "vid1.en.json3")
            with open(cap, "w") as f:
                json.dump({
                    "events": [
                        {"tStartMs": 1000, "segs": [{"utf8": "Today we have a 2003 Tahoe with P 0 1 7 1."}]},
                        {"tStartMs": 80000, "segs": [{"utf8": "Smoke test shows a huge intake gasket vacuum leak."}]},
                    ]
                }, f)
            segs = yt.parse_json3_segments(cap, chunk_sec=60)
            self.assertEqual(len(segs), 2)
            self.assertEqual(segs[0]["t_sec"], 1)
            self.assertIn("P0171", segs[0]["t"])
            self.assertEqual(segs[1]["t_sec"], 80)

            raw_db = os.path.join(td, "raw.db")
            con = yt.open_raw_db(raw_db)
            meta = {"id": "vid1", "title": "2003 Tahoe P0171 Fix", "channel": "South Main Auto",
                    "upload_date": "20240501", "duration": 600, "view_count": 20000, "like_count": 800}
            _, _, metrics = yt.evaluate_video(meta)
            metrics["top_comments"] = [{"text": "Worked on my 04 Silverado!", "likes": 12, "pos": True, "neg": False}]
            yt.save_video_row(con, meta, True, "", metrics, "chevrolet", "chevrolet:P0171", cap, "en")
            con.close()

            out_dir = os.path.join(td, "work")
            class Args:
                pass
            a = Args()
            a.raw_db = raw_db
            a.out_dir = out_dir
            yt.cmd_export_work(a)

            wdb = os.path.join(out_dir, "chevrolet.work.db")
            self.assertTrue(os.path.isfile(wdb))
            w = sqlite3.connect(wdb)
            row = w.execute("SELECT src, tid, segs FROM thread").fetchone()
            w.close()
            self.assertEqual(row[0], "yt_south_main_auto")
            self.assertEqual(row[1], "vid1")
            loaded = json.loads(row[2])
            self.assertEqual(len(loaded), 3)
            self.assertEqual(loaded[2]["k"], "reply")

if __name__ == "__main__":
    unittest.main()
