"""kb/build_general_kb.py loads a source only when its file matches the sha256 in sources.json.
    python -m pytest kb/tests/test_general_kb.py
Synthetic sources in a temp folder; no data files needed.
"""
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "kb"))
import build_general_kb as G  # noqa: E402

PAGE = "<html><head><title>Cooling</title></head><body><h2>Thermostat</h2><p>" + "The thermostat opens. " * 20 + "</p></body></html>"
ARTICLE = "Intro text. " * 20 + "\n== Operation ==\n" + "The pump moves coolant. " * 20


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Sha256Check(unittest.TestCase):
    def build(self, edit=None):
        raw = tempfile.mkdtemp(prefix="genkb-")
        open(os.path.join(raw, "a.html"), "w").write(PAGE)
        json.dump({"title": "Water pump", "extract": ARTICLE}, open(os.path.join(raw, "b.json"), "w"))
        man = {"a.html": {"type": "html", "title": "Cooling", "sha256": sha(PAGE.encode())},
               "b.json": {"type": "wiki", "title": "Wikipedia: Water pump", "sha256": sha(ARTICLE.encode())}}
        if edit:
            edit(raw, man)
        json.dump(man, open(os.path.join(raw, "sources.json"), "w"))
        db = os.path.join(raw, "general.db")
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["build_general_kb.py", "--raw", raw, "--db", db]), redirect_stdout(out):
            G.main()
        conn = sqlite3.connect(db)
        files = {f: h for f, h in conn.execute("SELECT file, sha256 FROM source")}
        conn.close()
        return files, out.getvalue()

    def test_matching_files_load_with_their_digest(self):
        files, report = self.build()
        self.assertEqual(files, {"a.html": sha(PAGE.encode()), "b.json": sha(ARTICLE.encode())})
        self.assertNotIn("[ERROR]", report)

    def test_changed_file_is_an_error_and_skipped(self):
        files, report = self.build(lambda raw, man: open(os.path.join(raw, "a.html"), "a").write("<p>added</p>"))
        self.assertEqual(set(files), {"b.json"})
        self.assertIn("[ERROR] a.html: sha256", report)

    def test_changed_article_text_is_caught(self):
        def edit(raw, man):
            json.dump({"title": "Water pump", "extract": ARTICLE + " Edited."}, open(os.path.join(raw, "b.json"), "w"))
        files, report = self.build(edit)
        self.assertEqual(set(files), {"a.html"})
        self.assertIn("[ERROR] b.json: sha256", report)

    def test_missing_hash_is_an_error(self):
        files, report = self.build(lambda raw, man: man["a.html"].pop("sha256"))
        self.assertEqual(set(files), {"b.json"})
        self.assertIn("[ERROR] a.html: no sha256", report)


if __name__ == "__main__":
    unittest.main()
