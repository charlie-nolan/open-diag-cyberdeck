"""A make DB's own manual wording for shop words, read by kb_retriever on top of its make-neutral table.

  python kb/kb_synonyms.py kb/<make>.db list
  python kb/kb_synonyms.py kb/<make>.db add "exhaust manifold" "front exhaust pipe"
  python kb/kb_synonyms.py kb/<make>.db remove "exhaust manifold"

'manual' may hold several names separated by '|'. Add a pair only after checking the make's manual uses that name."""
import argparse
import sqlite3

SYNONYM_SQL = "CREATE TABLE IF NOT EXISTS synonym (shop TEXT PRIMARY KEY, manual TEXT NOT NULL)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    add = sub.add_parser("add")
    add.add_argument("shop")
    add.add_argument("manual")
    rm = sub.add_parser("remove")
    rm.add_argument("shop")
    a = ap.parse_args(argv)
    c = sqlite3.connect(a.db)
    c.execute(SYNONYM_SQL)
    if a.cmd == "add":
        c.execute("INSERT OR REPLACE INTO synonym VALUES (?, ?)", (a.shop.lower().strip(), a.manual.lower().strip()))
    elif a.cmd == "remove":
        c.execute("DELETE FROM synonym WHERE shop=?", (a.shop.lower().strip(),))
    c.commit()
    for shop, manual in c.execute("SELECT shop, manual FROM synonym ORDER BY shop"):
        print(f"{shop} -> {manual}")


if __name__ == "__main__":
    main()
