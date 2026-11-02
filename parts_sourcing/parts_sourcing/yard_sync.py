"""Refresh yards.db from Row52 (Pick-n-Pull's public yard inventory) and decode Subaru VINs with NHTSA vPIC.

Needs internet; run it on the PC (or on the deck over a phone hotspot), then the deck reads yards.db
offline. Polite by default: one page every 3 s, about 90 pages for a 100-mile radius around Seattle.

    python3 -m parts_sourcing.yard_sync                      # ZIP + radius from sourcing.json
    python3 -m parts_sourcing.yard_sync --zip 98101 --miles 100 --db ~/subaru_cyberdeck/kb/yards.db
    python3 -m parts_sourcing.yard_sync --probe              # fetch page 1 only, print what was parsed

Row52 search results carry year/make/model, VIN, yard, row and the date the car was set. The parser
works on the page HTML by anchoring on the /Vehicle/Index/<VIN> links; --save-html keeps the raw pages
so a layout change can be fixed without guessing.
"""
import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

from . import yards as Y

UA = "subaru-cyberdeck-yard-sync/1.0 (personal use; one request every few seconds)"
ROW52_SEARCH = "https://row52.com/search/"
VPIC_BATCH = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/"
VIN_LINK_RE = re.compile(r"/Vehicle/Index/([A-HJ-NPR-Z0-9]{17})\b", re.I)
TITLE_RE = re.compile(r"/Vehicle/Index/[A-Z0-9]{17}[^>]*>\s*(?:<[^>]+>\s*)*((?:19|20)\d\d)\s+([^<]{2,80}?)\s*<", re.I)
YARD_LINK_RE = re.compile(r"/Yards?/Detail/(\d+)[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,80}?)\s*<", re.I)
YARD_TEXT_RE = re.compile(r"\b(PICK-n-PULL\s+[A-Z][A-Za-z .'-]{2,40}?)(?=\s{2,}|\s*\||\s+[A-Z][a-z]+,\s|$)")
CITY_RE = re.compile(r"\b([A-Z][A-Za-z .'-]{2,30}),\s+(Washington|Oregon|Idaho|WA|OR|ID)\s*(\d{5})?")
ROW_RE = re.compile(r"\bRow\b\s*:?\s*([A-Za-z0-9-]{1,6})\b")
ADDED_RE = re.compile(r"Added(?:\s+to\s+yard)?\s*:?\s*([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")
ANY_DATE_RE = re.compile(r"\b([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\b")
PAGES_RE = re.compile(r"\bof\s+(\d{1,4})\b")
MULTIWORD_MAKES = ("Land Rover", "Mercedes-Benz", "Alfa Romeo", "Aston Martin", "Rolls-Royce", "Mercedes Benz")


def http(url, data=None, timeout=40, tries=4):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # network blips: back off 2, 4, 8 s
            if i + 1 == tries:
                raise
            print(f"  retry after {e}", file=sys.stderr)
            time.sleep(2 ** (i + 1))


def strip_tags(s):
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "  ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", htmllib.unescape(s)).replace("\n", "  ")


def split_make_model(text):
    text = " ".join(htmllib.unescape(text).split())
    for mk in MULTIWORD_MAKES:
        if text.lower().startswith(mk.lower() + " "):
            return mk, text[len(mk) + 1:]
    parts = text.split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def parse_date(s):
    if not s:
        return None
    s = s.replace(".", "")
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_row52(page_html):
    """[{vin, year, make, model, yard, location_id, city, state, zip, row, added, url}] from one search page."""
    hits = [(m.start(), m.group(1).upper()) for m in VIN_LINK_RE.finditer(page_html)]
    starts, seen = [], set()
    for pos, vin in hits:
        if vin not in seen:
            seen.add(vin)
            starts.append((pos, vin))
    titles = {m.group(0).split("/Vehicle/Index/")[1][:17].upper(): (m.group(1), m.group(2))
              for m in TITLE_RE.finditer(page_html)}
    out = []
    for i, (pos, vin) in enumerate(starts):
        # A card can open just before its first VIN link (image block), so look back a little too.
        lo = max(0, pos - 400) if i == 0 else max(starts[i - 1][0] + 1, pos - 400)
        hi = starts[i + 1][0] if i + 1 < len(starts) else len(page_html)
        chunk = page_html[pos:hi]
        text = strip_tags(chunk)
        if vin in titles:
            year, mm = titles[vin]
        else:
            m = re.search(r"\b((?:19|20)\d\d)\s+([A-Z][\w-]+(?:\s[\w.-]+){0,4})", text)
            if not m:
                continue
            year, mm = m.group(1), m.group(2)
        make, model = split_make_model(mm)
        loc_id, yard = None, None
        ym = YARD_LINK_RE.search(chunk) or YARD_LINK_RE.search(page_html[lo:pos])
        if ym:
            loc_id, yard = int(ym.group(1)), " ".join(ym.group(2).split())
        if not yard:
            ytm = YARD_TEXT_RE.search(text)
            yard = " ".join(ytm.group(1).split()) if ytm else None
        cm = CITY_RE.search(text)
        rm = ROW_RE.search(text)
        am = ADDED_RE.search(text)
        added = parse_date(am.group(1) if am else (ANY_DATE_RE.search(text).group(1) if ANY_DATE_RE.search(text) else None))
        out.append(dict(vin=vin, year=int(year), make=make, model=model, yard=yard, location_id=loc_id,
                        city=cm.group(1).strip() if cm else None, state=cm.group(2) if cm else None,
                        zip=cm.group(3) if cm else None, row=rm.group(1) if rm else None, added=added,
                        url=f"https://row52.com/Vehicle/Index/{vin}"))
    return out


def crawl_row52(zip_code, miles, delay=3.0, max_pages=250, save_html=None, probe=False):
    cars, pages, total = {}, 0, None
    for page in range(1, max_pages + 1):
        q = urllib.parse.urlencode({"ZipCode": zip_code, "Distance": miles, "Sort": "DateAdded",
                                    "SortDirection": "desc", "Page": page})
        body = http(f"{ROW52_SEARCH}?{q}")
        pages += 1
        if save_html:
            os.makedirs(save_html, exist_ok=True)
            with open(os.path.join(save_html, f"row52_p{page:03d}.html"), "w") as f:
                f.write(body)
        rows = parse_row52(body)
        if total is None:
            pm = PAGES_RE.search(strip_tags(body))
            total = int(pm.group(1)) if pm else None
        if VIN_LINK_RE.search(body) and not rows:
            raise RuntimeError(f"page {page}: VIN links found but nothing parsed; Row52 layout changed "
                               "(re-run with --save-html and fix parse_row52)")
        new = [r for r in rows if r["vin"] not in cars]
        for r in rows:
            cars.setdefault(r["vin"], r)
        print(f"  page {page}{'/' + str(total) if total else ''}: {len(rows)} cars, {len(new)} new", flush=True)
        if probe or not new or (total and page >= total):
            break
        time.sleep(delay)
    return list(cars.values()), pages


def vpic_decode(vins, delay=1.0):
    """{vin: dict} from NHTSA vPIC, 50 VINs per request."""
    out = {}
    for i in range(0, len(vins), 50):
        batch = vins[i:i + 50]
        data = urllib.parse.urlencode({"format": "json", "data": ";".join(batch)}).encode()
        res = json.loads(http(VPIC_BATCH, data=data, timeout=90))
        for r in res.get("Results", []):
            out[r.get("VIN", "").upper()] = r
        time.sleep(delay)
    return out


def _num(x, cast=float):
    try:
        return cast(float(x)) if x not in (None, "") else None
    except ValueError:
        return None


def store(con, cars, run_at, decode_makes=("subaru", "saab"), full_yards=()):
    """Upsert cars; for yards named in full_yards this list is the yard's whole stock (older cars there drop off)."""
    yard_ids = {}
    for r in cars:
        name = r["yard"] or "unknown yard"
        if name not in yard_ids:
            con.execute("INSERT INTO yard(name, city, state, zip, source, location_id, url) VALUES (?,?,?,?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET city=coalesce(excluded.city, city), "
                        "state=coalesce(excluded.state, state), zip=coalesce(excluded.zip, zip), "
                        "location_id=coalesce(excluded.location_id, location_id), url=coalesce(excluded.url, url)",
                        (name, r["city"], r["state"], r["zip"], "row52", r["location_id"],
                         f"https://row52.com/search/?LocationId={r['location_id']}" if r["location_id"] else None))
            yard_ids[name] = con.execute("SELECT id FROM yard WHERE name=?", (name,)).fetchone()[0]
        con.execute("INSERT INTO yard_vehicle(vin, yard_id, year, make, model, row, added, first_seen, last_seen, url) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(vin, yard_id) DO UPDATE SET row=excluded.row, "
                    "added=coalesce(excluded.added, added), last_seen=excluded.last_seen",
                    (r["vin"], yard_ids[name], r["year"], r["make"], r["model"], r["row"], r["added"],
                     run_at, run_at, r["url"]))
    for name in full_yards:
        con.execute("UPDATE yard SET last_full = ? WHERE name = ?", (run_at, name))
    gone = con.execute("SELECT count(*) FROM yard_vehicle v JOIN yard y ON y.id = v.yard_id "
                       "WHERE y.last_full = ? AND v.last_seen < ?", (run_at, run_at)).fetchone()[0]
    todo = [v for (v,) in con.execute(
        f"SELECT vin FROM yard_vehicle WHERE lower(make) IN ({','.join('?' * len(decode_makes))}) "
        "AND vin NOT IN (SELECT vin FROM vin_decode)", decode_makes)]
    return gone, todo


def store_decodes(con, decoded):
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for vin, r in decoded.items():
        con.execute("INSERT OR REPLACE INTO vin_decode VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (vin, r.get("Model"), r.get("Series"), r.get("Trim"), r.get("BodyClass"), r.get("DriveType"),
                     _num(r.get("DisplacementL")), _num(r.get("EngineCylinders"), int), r.get("ValveTrainDesign"),
                     r.get("Turbo"), r.get("EngineModel"),
                     r.get("ErrorText") if r.get("ErrorCode") not in ("0", None) else None, now))


def load_settings(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(here, "sourcing.json"))
    ap.add_argument("--zip")
    ap.add_argument("--miles", type=int)
    ap.add_argument("--db")
    ap.add_argument("--delay", type=float, default=3.0)
    ap.add_argument("--save-html", help="keep raw pages in this folder")
    ap.add_argument("--probe", action="store_true", help="fetch page 1 only and print the parsed rows; writes nothing")
    ap.add_argument("--no-decode", action="store_true", help="skip the NHTSA VIN decode")
    a = ap.parse_args(argv)
    cfg = load_settings(a.config)
    if not cfg.get("row52_crawl_allowed"):
        # Row52's Terms (row52.com/About/Terms) forbid robots/scrapers. Only run this with Row52's written
        # permission, then set "row52_crawl_allowed": true in sourcing.json.
        print("Row52 crawl is off: Row52's terms forbid automated access. Use the Row52 QR link on the sheet, "
              "or set row52_crawl_allowed in sourcing.json once Row52 has said yes.")
        return 2
    zip_code = a.zip or cfg.get("home_zip", "98101")
    miles = a.miles or cfg.get("yard_radius_miles", 100)
    db = os.path.expanduser(a.db or cfg.get("yards_db", Y.DEFAULT_DB))
    run_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"Row52: yards within {miles} mi of {zip_code}")
    if a.probe:
        cars, _ = crawl_row52(zip_code, miles, a.delay, save_html=a.save_html, probe=True)
        for r in cars:
            print(f"  {r['year']} {r['make']} | {r['model']} | {r['yard']} (loc {r['location_id']}, {r['city']}) "
                  f"row {r['row']} added {r['added']} {r['vin']}")
        print(f"{len(cars)} cars parsed; fields missing: "
              + ", ".join(f"{k}={sum(1 for r in cars if not r[k])}" for k in ("yard", "row", "added", "city")))
        return 0
    con = Y.connect(db, write=True)
    try:
        cars, pages = crawl_row52(zip_code, miles, a.delay, save_html=a.save_html)
        gone, todo = store(con, cars, run_at, full_yards={c["yard"] or "unknown yard" for c in cars})
        con.commit()
        print(f"{len(cars)} cars in {len({c['yard'] for c in cars})} yards; {gone} earlier cars no longer listed")
        if todo and not a.no_decode:
            print(f"vPIC: decoding {len(todo)} Subaru/Saab VINs")
            store_decodes(con, vpic_decode(todo))
        subaru = sum(1 for c in cars if c["make"].lower() in ("subaru", "saab"))
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,NULL)", (run_at, "row52", pages, len(cars), subaru, gone))
        con.commit()
        for name, n, s in con.execute(
                "SELECT y.name, count(*), sum(lower(v.make)='subaru') FROM yard_vehicle v JOIN yard y ON y.id=v.yard_id "
                "WHERE v.last_seen = ? GROUP BY y.name ORDER BY y.name", (run_at,)):
            print(f"  {name}: {n} cars, {s} Subarus")
    except Exception as e:
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,?)", (run_at, "row52", None, None, None, None, str(e)[:500]))
        con.commit()
        raise
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
