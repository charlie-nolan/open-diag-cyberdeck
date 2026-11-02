#!/usr/bin/env python3
"""Redo only the "Specs" and "Book labor / parts" lines of already-baked cards with the current
kb_retriever.py, without asking the card model anything. Causes, steps and sources stay as baked.

  python3 respec.py                      # rewrites kb/cards.db in place (backup: kb/cards.pre_respec.db)
  python3 respec.py --dry-run            # only counts what would change

Then load the cards again: build_subaru_kb.py cards, finish, embed --only dtc_card (respec.sh does all of it).
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_cards import DEFAULT_KB, DEFAULT_OUT, has_table, load_retriever, render, ro  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", default=DEFAULT_KB)
    ap.add_argument("--kb-dir", help="folder with kb_retriever.py (default: next to --kb)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="cards.db to update")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    out_path = os.path.expanduser(a.out)
    if not a.dry_run:
        shutil.copy2(out_path, os.path.join(os.path.dirname(out_path), "cards.pre_respec.db"))
    kb = ro(a.kb)
    with_labor = has_table(kb, "labor")
    Retriever = load_retriever(a.kb, a.kb_dir)
    retrievers = {}
    out = sqlite3.connect(out_path)
    rows = out.execute("SELECT code, model, year_min, year_max, title, text, card_json, sources_json FROM dtc_card").fetchall()
    changed = 0
    for code, model, year_min, year_max, title, old, card_json, sources_json in rows:
        r = retrievers.setdefault((model, year_max), Retriever(a.kb, model=model, year=year_max, engine=None))
        card, sources = json.loads(card_json), json.loads(sources_json)
        specs, labor = [], []
        for c in card["causes"][:3]:          # same lookups as bake_cards.py, with the current retriever
            specs += r.specs(c["cause"], limit=2)
            if with_labor:
                labor += r.labor(c["cause"], limit=2)
        specs, labor = list(dict.fromkeys(specs))[:6], list(dict.fromkeys(labor))[:6]
        desc = title.split(": ", 1)[1] if ": " in title else ""
        text = render(code, desc, card, sources, specs, labor)
        if text != old:
            changed += 1
            if not a.dry_run:
                out.execute("UPDATE dtc_card SET text=? WHERE code=? AND model=? AND year_min=? AND year_max=?",
                            (text, code, model, year_min, year_max))
    if not a.dry_run:
        out.commit()
    print(f"respec: {changed} of {len(rows)} cards got new spec/labor lines" + (" (dry run)" if a.dry_run else ""))


if __name__ == "__main__":
    main()
