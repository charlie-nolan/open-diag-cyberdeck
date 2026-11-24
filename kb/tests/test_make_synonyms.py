"""Make-specific manual names come from that make's DB; the shared table stays make-neutral."""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import kb_retriever as kr  # noqa: E402
import kb_synonyms  # noqa: E402


def _db(path, pairs):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE vehicle (id INTEGER PRIMARY KEY, model TEXT, year INTEGER)")
    c.commit()
    c.close()
    for shop, manual in pairs:
        kb_synonyms.main([path, "add", shop, manual])


def test_shared_table_has_no_make_specific_part_names():
    _, alts = kr._expand("exhaust manifold leak")
    assert "front exhaust pipe" not in alts
    _, alts = kr._expand("o2 sensor")
    assert "oxygen sensor" in alts


def test_each_make_gets_only_its_own_names(capsys):
    with tempfile.TemporaryDirectory() as d:
        a, b, plain = (os.path.join(d, n) for n in ("make_a.db", "make_b.db", "plain.db"))
        _db(a, [("exhaust manifold", "front exhaust pipe")])
        _db(b, [("exhaust manifold", "exhaust manifold|y-pipe")])
        _db(plain, [])
        ra, rb, rp = kr.Retriever(a), kr.Retriever(b), kr.Retriever(plain)
        assert "front exhaust pipe" in kr._expand("exhaust manifold", ra.syn_re)[1]
        assert "front exhaust pipe" not in kr._expand("exhaust manifold", rb.syn_re)[1]
        assert "y-pipe" in kr._expand("Exhaust Manifold crack", rb.syn_re)[1]
        assert rp.syn_re == []
        assert '"front exhaust pipe"' in kr._fts_query("exhaust manifold leak", extra=ra.syn_re)
        assert '"front exhaust pipe"' not in kr._fts_query("exhaust manifold leak")


def test_db_without_synonym_table():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "old.db")
        sqlite3.connect(p).execute("CREATE TABLE vehicle (id INTEGER PRIMARY KEY)").connection.commit()
        assert kr.Retriever(p).syn_re == []
