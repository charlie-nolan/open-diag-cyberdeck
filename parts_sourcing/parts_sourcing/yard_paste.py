"""Add yard cars you copied by hand (from Row52 or the Pick-n-Pull site in your browser) to yards.db.

Select the results on the page, copy, and paste into a text file (or straight into stdin). The parser
splits the text at each "YEAR MAKE MODEL" title and picks up VIN, yard, row and the date set when present.
Hand-typed lines work too: "2004 Subaru Forester row 12".

    python3 -m parts_sourcing.yard_paste lynnwood.txt --yard "PICK-n-PULL Lynnwood" --full
    xclip -o | python3 -m parts_sourcing.yard_paste - --yard "PICK-n-PULL Tacoma"

--full  this paste is the yard's whole current list (for the makes you pasted): cars from that yard that
        aren't in it stop counting. Without it, cars are only added, and drop off after 45 days.
VIN decodes for the engine go to NHTSA vPIC (a public government API) unless --no-decode.
"""
import argparse
import datetime as dt
import hashlib
import os
import re
import sys

from . import yards as Y
from .yard_sync import (ADDED_RE, ANY_DATE_RE, CITY_RE, ROW_RE, YARD_TEXT_RE, load_settings, parse_date,
                        split_make_model, store, store_decodes, vpic_decode)

TITLE_LINE_RE = re.compile(r"^\s*((?:19|20)\d\d)\s+([A-Z][A-Za-z-]+(?:\s+[A-Za-z0-9.\-/]+){0,4}?)"
                           r"(?=\s*$|\s+(?:row|vin|added|pick-n-pull)\b|\s{2,}|\s*[,|])", re.I | re.M)
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")


def parse_text(text, default_yard=None):
    titles = list(TITLE_LINE_RE.finditer(text))
    out, seen = [], set()
    for i, m in enumerate(titles):
        block = text[m.start():titles[i + 1].start() if i + 1 < len(titles) else len(text)]
        rest = block[m.end() - m.start():]
        make, model = split_make_model(m.group(2))
        model = re.split(r"\s+(?:row|vin|added)\b", model, flags=re.I)[0].strip()
        vm = next((v for v in VIN_RE.findall(block.upper()) if re.search(r"\d", v) and re.search(r"[A-Z]", v)), None)
        ym = YARD_TEXT_RE.search(rest)
        yard = " ".join(ym.group(1).split()) if ym else default_yard
        rm = ROW_RE.search(rest) or re.search(r"\brow\s*:?\s*([A-Za-z0-9-]{1,6})\b", rest, re.I)
        am = ADDED_RE.search(rest) or ANY_DATE_RE.search(rest)
        cm = CITY_RE.search(rest)
        row = rm.group(1) if rm else None
        vin = vm or "MANUAL-" + hashlib.sha1(f"{yard}|{m.group(1)}|{make}|{model}|{row}".lower().encode()).hexdigest()[:10]
        if vin in seen:
            continue
        seen.add(vin)
        out.append(dict(vin=vin, year=int(m.group(1)), make=make, model=model, yard=yard, location_id=None,
                        city=cm.group(1).strip() if cm else None, state=cm.group(2) if cm else None,
                        zip=cm.group(3) if cm else None, row=row, added=parse_date(am.group(1)) if am else None,
                        url=f"https://row52.com/Vehicle/Index/{vin}" if vm else None))
    return out


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="text file, or - for stdin")
    ap.add_argument("--yard", help="yard name for cars whose block doesn't name one, e.g. 'PICK-n-PULL Lynnwood'")
    ap.add_argument("--full", action="store_true", help="this is the yard's whole current list")
    ap.add_argument("--no-decode", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print what was parsed, write nothing")
    ap.add_argument("--config", default=os.path.join(here, "sourcing.json"))
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8", errors="replace").read()
    cars = parse_text(text, a.yard)
    for c in cars:
        print(f"  {c['year']} {c['make']} {c['model']} | {c['yard'] or '?'} | row {c['row'] or '?'} | "
              f"set {c['added'] or '?'} | {c['vin']}")
    missing = [c for c in cars if not c["yard"]]
    print(f"{len(cars)} cars parsed" + (f"; {len(missing)} have no yard (use --yard)" if missing else ""))
    if a.dry_run or not cars:
        return 0
    if missing:
        print("Not saved: give --yard so every car has a yard.")
        return 2
    cfg = load_settings(a.config)
    con = Y.connect(os.path.expanduser(cfg.get("yards_db", Y.DEFAULT_DB)), write=True)
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        gone, todo = store(con, cars, run_at, full_yards={c["yard"] for c in cars} if a.full else ())
        todo = [v for v in todo if not v.startswith("MANUAL-")]
        if todo and not a.no_decode:
            print(f"vPIC: decoding {len(todo)} VINs")
            store_decodes(con, vpic_decode(todo))
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,NULL)",
                    (run_at, "paste-full" if a.full else "paste", None, len(cars),
                     sum(c["make"].lower() in ("subaru", "saab") for c in cars), gone))
        con.commit()
    finally:
        con.close()
    print(f"saved{f'; {gone} earlier cars at that yard no longer listed' if a.full else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
