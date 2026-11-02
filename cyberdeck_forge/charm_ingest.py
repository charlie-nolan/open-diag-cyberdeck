#!/usr/bin/env python3
"""
charm_ingest.py -- Operation CHARM per-vehicle ingester.

Built against CHARM's own server.js. Key facts it relies on:

  1. TWO-LEVEL INDIRECTION. The value at a path key is not the page, it is
     another key:
         ptr  = get(b"/Subaru/2002/Forester%20L%20F4-2.5L%20SOHC/...")
         html = get(ptr)
     Values beginning b"SPECIAL_" are navigational index nodes (generated
     child listings), not articles.

  2. KEYS ARE PERCENT-ENCODED. The stored key is b"/Acura%20Truck/1996/",
     never b"/Acura Truck/1996/". Prefixes are quote()d with safe="/".

  3. NO COMPRESSION, NO MSGPACK, NO SUB-DATABASES. Values are raw UTF-8.
     server.js uses openDbi({name: null}) -- the unnamed main DB.

  4. SORT ORDER. b"/" is 0x2F, which sorts before digits (0x30) and before
     "S" (0x53). Hundreds of millions of path pointers precede the article
     keys, so a full cursor scan reads only signposts. We never full-scan:
     we prefix-seek per vehicle (~8,900 keys) using vehicle_catalog.

  5. lmdb-images IS A SEPARATE ENVIRONMENT and is not in lmdb-pages.sqsh.
     /images/ srcs are recorded as text but will not resolve locally.

PROVENANCE RULE: every extracted row carries source_key + source_ptr, both
NOT NULL. A spec that cannot name the CHARM key it came from cannot be
inserted. Nothing in this script seeds, infers, or invents a value.

Usage
-----
  # 1. Prove the deref works and look at real content (writes nothing):
  ./charm_ingest.py --probe --vehicle "Subaru/2002/Forester L F4-2.5L SOHC"

  # 2. Ingest one vehicle:
  ./charm_ingest.py --vehicle "Subaru/2002/Forester L F4-2.5L SOHC"

  # 3. Ingest every vehicle of a make, from vehicle_catalog:
  ./charm_ingest.py --make Subaru

  # 4. See what is available:
  ./charm_ingest.py --list-makes
  ./charm_ingest.py --list-vehicles Subaru
"""

import argparse
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

import lmdb

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("need beautifulsoup4: pip install beautifulsoup4 lxml")

CHARM_MOUNT = Path("/mnt/charm")
FORGE = Path.home() / "cyberdeck_forge"
CATALOG_DB = FORGE / "build_out" / "cyberdeck_universal.db"
OUT_DB = FORGE / "build_out" / "charm_content.db"

SPECIAL = b"SPECIAL_"

# Leaf-section vocabulary, taken verbatim from isProperty() in CHARM's
# server.js. These are the only section names CHARM uses, so they are what
# we route on -- no guessing at folder names.
SECTIONS = {
    "service precautions", "application and id", "description and operation",
    "adjustments", "testing and inspection", "diagrams", "locations",
    "specifications", "service and repair", "parts",
    "technical service bulletins", "tools and equipment", "labor times",
    "exploded diagram", "service intervals", "fundamentals and basics",
    "diagnostic trouble codes", "diagnosis and testing", "overview",
    "general procedures",
}

SPEC_UNIT_RE = re.compile(
    r"(\d[\d.,/\s-]*)\s*"
    r"(ft[\s.\-]?lbs?|lb[\s.\-]?ft|in[\s.\-]?lbs?|N\s*[·.*]?\s*m|Nm|kgf[\s.\-]?m|"
    r"psi|kPa|bar|quarts?|qts?|liters?|lt?rs?|gal(?:lons?)?|pints?|ml|cc|"
    r"ohms?|k[\s]?ohms?|volts?|mm|in(?:ch(?:es)?)?)\b",
    re.I,
)
FLUID_RE = re.compile(r"\b(quart|qt|liter|ltr|gal|pint|ml|cc|capacity|refill|fill)\b", re.I)
HOURS_RE = re.compile(r"^\s*(\d{1,2}\.\d{1,2})\s*$")
PARTNO_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{4,19}$")


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------
def init_out(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;

        -- Every table below REQUIRES provenance. source_key is the exact
        -- percent-encoded CHARM key; source_ptr is the indirection pointer
        -- that key resolved to. Both NOT NULL, by design.

        CREATE TABLE IF NOT EXISTS charm_page (
            id          INTEGER PRIMARY KEY,
            make        TEXT NOT NULL,
            year        INTEGER NOT NULL,
            model_full  TEXT NOT NULL,
            section     TEXT,
            breadcrumb  TEXT NOT NULL,
            title       TEXT,
            body_text   TEXT,
            html_bytes  INTEGER,
            image_srcs  TEXT,
            source_key  TEXT NOT NULL UNIQUE,
            source_ptr  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS charm_spec (
            id          INTEGER PRIMARY KEY,
            page_id     INTEGER NOT NULL REFERENCES charm_page(id),
            make        TEXT NOT NULL,
            year        INTEGER NOT NULL,
            model_full  TEXT NOT NULL,
            section     TEXT,
            component   TEXT NOT NULL,
            spec_kind   TEXT NOT NULL,
            value_raw   TEXT NOT NULL,
            row_raw     TEXT NOT NULL,
            source_key  TEXT NOT NULL,
            source_ptr  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_spec_lookup
            ON charm_spec(make, year, model_full, spec_kind);

        CREATE TABLE IF NOT EXISTS charm_labor (
            id             INTEGER PRIMARY KEY,
            page_id        INTEGER NOT NULL REFERENCES charm_page(id),
            make           TEXT NOT NULL,
            year           INTEGER NOT NULL,
            model_full     TEXT NOT NULL,
            operation      TEXT NOT NULL,
            hours_raw      TEXT,
            part_number    TEXT,
            price_raw      TEXT,
            row_raw        TEXT NOT NULL,
            source_key     TEXT NOT NULL,
            source_ptr     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_labor_lookup
            ON charm_labor(make, year, model_full);

        CREATE VIRTUAL TABLE IF NOT EXISTS fts_charm_page USING fts5(
            breadcrumb, title, body_text,
            content='charm_page', content_rowid='id',
            tokenize='porter unicode61'
        );
    """)
    conn.commit()


# --------------------------------------------------------------------------
# CHARM access
# --------------------------------------------------------------------------
def open_charm(path: Path):
    # subdir=True: data.mdb sits directly inside the mount, which is what
    # python-lmdb's default expects. No map_size needed for readonly.
    return lmdb.open(str(path), readonly=True, lock=False,
                     readahead=False, subdir=True, create=False)


def enc_prefix(vehicle_path: str) -> bytes:
    """'Subaru/2002/Forester L F4-2.5L SOHC' -> b'/Subaru/2002/Forester%20L%20F4-2.5L%20SOHC/'"""
    clean = vehicle_path.strip("/")
    return ("/" + urllib.parse.quote(clean, safe="/") + "/").encode()


def deref(txn, key: bytes):
    """Follow CHARM's two-level lookup. Returns (ptr, payload) or (None, None)."""
    ptr = txn.get(key)
    if ptr is None:
        return None, None
    return ptr, txn.get(ptr)


def is_index_node(ptr: bytes) -> bool:
    return ptr.startswith(SPECIAL)


def walk_vehicle(txn, prefix: bytes, limit: int = 0):
    """Yield (key, ptr, payload) for every node under one vehicle prefix."""
    cur = txn.cursor()
    if not cur.set_range(prefix):
        return
    n = 0
    while True:
        key = cur.key()
        if not key.startswith(prefix):
            return
        ptr = cur.value()
        if ptr:
            yield key, ptr, txn.get(ptr)
        n += 1
        if limit and n >= limit:
            return
        if not cur.next():
            return


def section_of(key_text: str) -> str:
    """Pick the CHARM section name out of a decoded path, if present."""
    for part in reversed([p for p in key_text.strip("/").split("/") if p]):
        if part.lower() in SECTIONS:
            return part
    return ""


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def table_rows(soup):
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True)
                 for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) >= 2:
            yield cells


def extract(conn, veh, key: bytes, ptr: bytes, html: bytes) -> bool:
    """Parse one article page. Returns True if a page row was written."""
    make, year, model_full = veh
    key_text = urllib.parse.unquote(key.decode("utf-8", "replace"))
    ptr_text = ptr.decode("utf-8", "replace")
    text = html.decode("utf-8", "replace")

    soup = BeautifulSoup(text, "lxml")
    title = soup.title.string.strip()[:200] if (soup.title and soup.title.string) \
        else key_text.rstrip("/").split("/")[-1]
    body = soup.get_text("\n", strip=True)
    section = section_of(key_text)
    imgs = ",".join(
        i["src"] for i in soup.find_all("img", src=True)[:20]
    )

    cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO charm_page
          (make, year, model_full, section, breadcrumb, title, body_text,
           html_bytes, image_srcs, source_key, source_ptr)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (make, year, model_full, section, key_text, title, body,
          len(html), imgs, key_text, ptr_text))
    if not cur.lastrowid or cur.rowcount == 0:
        return False
    page_id = cur.lastrowid

    sec_l = section.lower()
    is_labor = sec_l in ("labor times", "parts")

    specs, labors = [], []
    for cells in table_rows(soup):
        row_raw = " | ".join(cells)[:500]
        component = cells[0][:200]

        if is_labor:
            hours = part_no = price = None
            for c in cells[1:]:
                if HOURS_RE.match(c):
                    hours = c.strip()
                elif "$" in c:
                    price = c[:40]
                elif PARTNO_RE.match(c.replace(" ", "")):
                    part_no = c.replace(" ", "")[:40]
            if hours or part_no or price:
                labors.append((page_id, make, year, model_full, component,
                               hours, part_no, price, row_raw,
                               key_text, ptr_text))
            continue

        m = SPEC_UNIT_RE.search(row_raw)
        if m:
            kind = "fluid_capacity" if FLUID_RE.search(row_raw) else "measurement"
            specs.append((page_id, make, year, model_full, section, component,
                          kind, m.group(0).strip()[:120], row_raw,
                          key_text, ptr_text))

    if specs:
        cur.executemany("""
            INSERT INTO charm_spec
              (page_id, make, year, model_full, section, component,
               spec_kind, value_raw, row_raw, source_key, source_ptr)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, specs)
    if labors:
        cur.executemany("""
            INSERT INTO charm_labor
              (page_id, make, year, model_full, operation, hours_raw,
               part_number, price_raw, row_raw, source_key, source_ptr)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, labors)
    return True


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------
def catalog(sql: str, args=()):
    if not CATALOG_DB.exists():
        sys.exit(f"vehicle_catalog not found at {CATALOG_DB}")
    c = sqlite3.connect(f"file:{CATALOG_DB}?mode=ro", uri=True)
    rows = c.execute(sql, args).fetchall()
    c.close()
    return rows


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
def probe(env, vehicle: str, limit: int) -> None:
    prefix = enc_prefix(vehicle)
    print(f"prefix  : {prefix!r}\n")
    shown_article = 0
    with env.begin() as txn:
        root_ptr, root_html = deref(txn, prefix)
        if root_ptr is None:
            print("NO MATCH at vehicle root. Check spelling via --list-vehicles.")
            return
        print(f"root ptr: {root_ptr[:60]!r}  (index node: {is_index_node(root_ptr)})")
        print(f"root html bytes: {len(root_html) if root_html else 0}\n")

        n_idx = n_art = 0
        for key, ptr, payload in walk_vehicle(txn, prefix, limit=limit):
            if is_index_node(ptr):
                n_idx += 1
                continue
            n_art += 1
            if shown_article < 3 and payload:
                kt = urllib.parse.unquote(key.decode("utf-8", "replace"))
                print("=" * 72)
                print("KEY :", kt[:160])
                print("PTR :", ptr[:60].decode("utf-8", "replace"))
                print("SIZE:", len(payload), "bytes")
                print("-" * 72)
                print(payload[:700].decode("utf-8", "replace"))
                print()
                shown_article += 1
        print("=" * 72)
        print(f"scanned {n_idx + n_art} nodes under this vehicle: "
              f"{n_idx} index, {n_art} article")
        if n_art == 0:
            print("No article nodes -- every value was SPECIAL_. Widen --limit.")


def ingest(env, conn, vehicles, limit: int) -> None:
    pages = 0
    for make, year, model_full in vehicles:
        prefix = enc_prefix(f"{make}/{year}/{model_full}")
        with env.begin() as txn:
            if txn.get(prefix) is None:
                print(f"  [skip] no CHARM node: {prefix.decode()}")
                continue
            local = 0
            for key, ptr, payload in walk_vehicle(txn, prefix, limit=limit):
                if is_index_node(ptr) or not payload:
                    continue
                if extract(conn, (make, year, model_full), key, ptr, payload):
                    local += 1
            conn.commit()
            pages += local
            print(f"  [ok] {make} {year} {model_full}: {local:,} article pages")
    conn.execute("INSERT INTO fts_charm_page(fts_charm_page) VALUES('rebuild')")
    conn.commit()

    cur = conn.cursor()
    print("\n" + "=" * 72)
    for t in ("charm_page", "charm_spec", "charm_labor"):
        print(f"  {t:14s} {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:>10,}")
    print(f"  {'total pages':14s} {pages:>10,}")
    print(f"  output: {OUT_DB}")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--charm", default=str(CHARM_MOUNT))
    ap.add_argument("--vehicle", help="'Make/Year/Model full' (unencoded)")
    ap.add_argument("--make", help="ingest every vehicle of this make")
    ap.add_argument("--probe", action="store_true", help="read-only demo, writes nothing")
    ap.add_argument("--limit", type=int, default=0, help="max nodes per vehicle (0 = all)")
    ap.add_argument("--list-makes", action="store_true")
    ap.add_argument("--list-vehicles", metavar="MAKE")
    args = ap.parse_args()

    if args.list_makes:
        for mk, n in catalog("SELECT make, COUNT(*) FROM vehicle_catalog "
                             "GROUP BY make ORDER BY make"):
            print(f"  {mk:28s} {n:>6,}")
        return

    if args.list_vehicles:
        for mk, yr, mf in catalog("SELECT make, year, model_full FROM vehicle_catalog "
                                  "WHERE make=? ORDER BY year, model_full",
                                  (args.list_vehicles,)):
            print(f"  {yr}  {mf}")
        return

    env = open_charm(Path(args.charm))

    if args.probe:
        if not args.vehicle:
            sys.exit("--probe needs --vehicle")
        probe(env, args.vehicle, args.limit or 20000)
        return

    if args.vehicle:
        parts = [p for p in args.vehicle.strip("/").split("/") if p]
        if len(parts) != 3 or not parts[1].isdigit():
            sys.exit("--vehicle must be 'Make/Year/Model full'")
        vehicles = [(parts[0], int(parts[1]), parts[2])]
    elif args.make:
        vehicles = catalog("SELECT make, year, model_full FROM vehicle_catalog "
                           "WHERE make=? ORDER BY year, model_full", (args.make,))
        print(f"[*] {len(vehicles):,} vehicles for {args.make}")
    else:
        sys.exit("need --vehicle, --make, --probe, --list-makes or --list-vehicles")

    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(OUT_DB))
    init_out(conn)
    ingest(env, conn, vehicles, args.limit)
    conn.close()


if __name__ == "__main__":
    main()
