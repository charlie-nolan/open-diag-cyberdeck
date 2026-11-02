# Remove one doc and everything hanging off its sections (incl. section_vec rows), then rebuild FTS.
import sqlite3, sys, sqlite_vec
db, title = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db); c.enable_load_extension(True); sqlite_vec.load(c)
r = c.execute("SELECT id FROM doc WHERE title=?", (title,)).fetchone()
if not r: sys.exit(f"no doc titled {title!r}")
d = r[0]
c.execute("CREATE TEMP TABLE gone AS SELECT id FROM section WHERE doc_id=?", (d,))
n = c.execute("SELECT count(*) FROM gone").fetchone()[0]
for t, col in (("section_ref", "from_id"), ("dtc_procedure", "section_id"), ("section_vehicle", "section_id"),
               ("spec", "section_id"), ("section", "id")):
    c.execute(f"DELETE FROM {t} WHERE {col} IN (SELECT id FROM gone)")
for (i,) in c.execute("SELECT id FROM gone").fetchall():     # vec0: delete one id at a time
    c.execute("DELETE FROM section_vec WHERE id=?", (i,))
c.execute("UPDATE section_ref SET to_id=NULL WHERE to_id IN (SELECT id FROM gone)")
c.execute("DELETE FROM doc WHERE id=?", (d,))
c.execute("DELETE FROM spec_fts")
c.execute("INSERT INTO spec_fts(rowid, component, context) SELECT p.id, p.component, s.topic || ' ' || s.title "
          "FROM spec p JOIN section s ON s.id=p.section_id")
c.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
c.commit()
print(f"removed doc {d} ({n} sections); left:", c.execute("SELECT count(*) FROM section").fetchone()[0],
      "sections,", c.execute("SELECT count(*) FROM section_vec").fetchone()[0], "vecs,",
      c.execute("PRAGMA integrity_check").fetchone()[0])
