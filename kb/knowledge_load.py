"""
Shared writer for the extra 'knowledge' loaders (add_investigations.py, add_stackexchange.py).
Rows keep their ids on a re-run (upsert, not delete + insert), so vectors made by `embed --missing` stay
attached to the right row; only rows that vanished from the source are removed (with their vectors).
"""
import glob
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
ALT = os.path.join(os.environ.get("CYBERDECK_ARCHIVE", os.path.expanduser("~/cyberdeck_archive")), "kb_build")
# kb file name -> make names as written in NHTSA files and on forums (default: the file name, '_' -> ' ')
MAKES = {"dodge": ["DODGE", "RAM"], "mercedes_benz": ["MERCEDES-BENZ", "MERCEDES BENZ", "MERCEDES", "BENZ"],
         "nissan": ["NISSAN", "DATSUN"], "land_rover": ["LAND ROVER", "LANDROVER"], "chevrolet": ["CHEVROLET", "CHEVY"],
         "volkswagen": ["VOLKSWAGEN", "VW"], "mitsubishi_fuso": ["MITSUBISHI FUSO"], "ud": ["UD", "UD TRUCKS"],
         "alfa_romeo": ["ALFA ROMEO", "ALFA"]}


def make_names(name):
    return MAKES.get(name, [name.replace("_", " ").upper()])


def target_dbs(names, done, marker):
    """DB paths: the given files, plus (with done) every finished make (kb/.<make>.done) and subaru.db,
    unless kb/.<make>.<marker> says this loader already ran."""
    dbs = list(names)
    if done:
        slugs = [os.path.basename(m)[1:-5] for m in sorted(glob.glob(os.path.join(HERE, ".*.done")))] + ["subaru"]
        for s in dict.fromkeys(slugs):
            if os.path.isfile(os.path.join(HERE, f".{s}.{marker}")):
                continue
            for d in (os.path.join(HERE, f"{s}.db"), os.path.join(ALT, f"{s}.db")):
                if os.path.isfile(d):
                    dbs.append(d)
                    break
    return dbs


def nhtsa_models(c):
    """This make's model names as NHTSA writes them (what the app's car model is matched against)."""
    return [r[0] for r in c.execute(
        "SELECT DISTINCT upper(model) FROM knowledge WHERE kind IN ('tsb','nhtsa_complaint','recall',"
        "'nhtsa_investigation') AND model IS NOT NULL AND length(trim(model)) >= 2")]


def write_kind(db, kind, rows, marker):
    """rows: (ref, model, year_min, year_max, title, body, url, confirmed). Returns the row count."""
    c = sqlite3.connect(db, timeout=600)       # waits if another job is writing this DB
    c.execute("CREATE TEMP TABLE new (ref TEXT, model TEXT, year_min INTEGER, year_max INTEGER, title TEXT, "
              "body TEXT, url TEXT, confirmed INTEGER)")
    c.executemany("INSERT INTO new VALUES (?,?,?,?,?,?,?,?)", rows)
    gone = [r[0] for r in c.execute(
        "SELECT id FROM knowledge k WHERE kind=? AND NOT EXISTS (SELECT 1 FROM new n WHERE n.ref IS k.ref AND "
        "n.model IS k.model AND n.year_min IS k.year_min)", (kind,))]
    if gone:
        try:                                   # drop their vectors too, so a reused id can't inherit one
            import sqlite_vec
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_vec'").fetchone():
                c.executemany("DELETE FROM knowledge_vec WHERE id=?", [(i,) for i in gone])
        except Exception:
            pass
        c.executemany("DELETE FROM knowledge WHERE id=?", [(i,) for i in gone])
    # update rows that exist (IS, not =, so a blank model or year still matches), add the rest
    same = "k.kind=? AND k.ref IS n.ref AND k.model IS n.model AND k.year_min IS n.year_min"
    c.execute(f"UPDATE knowledge AS k SET year_max=n.year_max, title=n.title, body=n.body, url=n.url, "
              f"confirmed=n.confirmed FROM new n WHERE {same}", (kind,))
    c.execute("INSERT INTO knowledge(kind,ref,model,year_min,year_max,title,body,url,confirmed) "
              "SELECT ?, ref, model, year_min, year_max, title, body, url, confirmed FROM new n "
              f"WHERE NOT EXISTS (SELECT 1 FROM knowledge k WHERE {same})", (kind, kind))
    c.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES ('rebuild')")
    c.commit()
    n = c.execute("SELECT count(*) FROM knowledge WHERE kind=?", (kind,)).fetchone()[0]
    c.close()
    name = os.path.splitext(os.path.basename(db))[0]
    open(os.path.join(HERE, f".{name}.{marker}"), "w").write(f"{kind} {n}\n")
    return n
