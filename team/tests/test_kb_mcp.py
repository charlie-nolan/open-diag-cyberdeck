"""teamkb MCP server: read-only DBs, profiles, evidence ids, refused fetches.
venv/bin/python -m unittest discover -s team/tests"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import kb_mcp  # noqa: E402

MAKE = next((m for m in ("subaru", "toyota", "chevrolet") if os.path.isfile(os.path.join(kb_mcp.ROOT, "kb", f"{m}.db"))),
            None)


@unittest.skipIf(MAKE is None, "no make DB on this machine")
class TeamKB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kb = kb_mcp.KB(MAKE, embed=False)
        cls.srv = kb_mcp.Server(kb, kb_mcp.Store(tempfile.mkdtemp(prefix="teamkb_test_")), cloud_manual=False,
                                cloud_manual_chars=500)
        cls.code = kb.r.db.execute("SELECT code FROM dtc_procedure LIMIT 1").fetchone()[0]

    def test_every_write_fails(self):
        con = self.srv.kb.r.db
        for sql in ("CREATE TABLE t_probe(x)", "DELETE FROM section WHERE id=-1",
                    "UPDATE section SET body=body WHERE id=-1", "INSERT INTO dtc(code) VALUES ('X0000')"):
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                con.execute(sql)

    def test_community_db_read_only(self):
        import community_ref
        with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
            community_ref._open(self.srv.kb.path).execute("CREATE TABLE t_probe(x)")

    def test_only_run_folder_written(self):
        before = os.path.getmtime(self.srv.kb.path)
        self.srv.call("dtc_procedure", {"code": self.code}, "full", "test")
        self.srv.call("search_manual", {"query": "removal and installation"}, "full", "test")
        self.assertEqual(os.path.getmtime(self.srv.kb.path), before)
        self.assertLessEqual(set(os.listdir(self.srv.store.dir)), {"evidence.jsonl", "lookups.jsonl"})

    def test_cloud_profile_hides_manual_when_off(self):
        names = {t["name"] for t in self.srv.tools("cloud")}
        self.assertFalse(names & {"search_manual", "dtc_procedure", "specs"})
        _, err = self.srv.call("search_manual", {"query": "x"}, "cloud", "gemini")
        self.assertTrue(err)

    def test_cap_per_member(self):
        self.srv.cloud_manual = True
        try:
            self.srv.store.used = {}
            for _ in range(3):
                self.srv.call("dtc_procedure", {"code": self.code}, "cloud", "gemini")
                self.srv.call("owner_reports", {"code": self.code}, "cloud", "gemini")
            self.assertLessEqual(self.srv.store.used.get("gemini", 0), self.srv.cloud_cap)
            self.assertGreater(self.srv.store.used.get("gemini", 0), 0)
            text, _ = self.srv.call("dtc_procedure", {"code": self.code}, "cloud", "gptoss")   # its own allowance
            self.assertNotIn("used up", text)
            self.srv.call("dtc_procedure", {"code": self.code}, "full", "claude")              # the lead isn't capped
            self.assertNotIn("claude", self.srv.store.used)
        finally:
            self.srv.cloud_manual = False

    def test_evidence_ids_resolve(self):
        text, err = self.srv.call("dtc_procedure", {"code": self.code}, "full", "claude")
        self.assertFalse(err)
        self.assertTrue(text.startswith("[M"))
        eid = text[1:text.index("]")]
        back, err = self.srv.call("read_evidence", {"id": eid}, "full", "gemini")
        self.assertFalse(err)
        self.assertTrue(back.startswith(f"[{eid}]"))
        rows = [json.loads(ln) for ln in open(os.path.join(self.srv.store.dir, "evidence.jsonl"))]
        self.assertIn(eid, [r["id"] for r in rows])

    def test_jsonrpc_shape(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "cloud", "x")
        self.assertEqual(r["result"]["serverInfo"]["name"], "teamkb")
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "cloud", "x"))
        r = self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}}, "full", "x")
        self.assertTrue(r["result"]["isError"])


class Fetch(unittest.TestCase):
    def test_refused_fetches(self):
        for url in ("https://www.row52.com/", "http://127.0.0.1:11434/", "http://100.64.0.1:8765/",
                    "file:///etc/passwd"):
            text, why = kb_mcp.fetch_page(url)
            self.assertIsNone(text)
            self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
