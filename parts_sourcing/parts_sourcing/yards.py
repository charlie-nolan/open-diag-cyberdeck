"""Cached self-service yard inventory (yards.db) and "how many cars at each yard might carry this part".

yards.db is written by yard_sync.py (needs internet: Row52 + NHTSA vPIC) and only read here, so the
deck answers offline from the last sync and says how old it is.

Match levels for a car in a yard, against the part's CHARM fitment (interchange.lookup):
  likely    same model + year, and the engine matches a CHARM vehicle listing this exact number
            (engine from the VIN decode, or the model-year only had one engine)
  family    as likely, but CHARM lists a sibling revision of the number (13028AA21B vs ...21C)
  possible  same model + year but the engine can't be told apart (no VIN decode and the year had
            several engines), or the car only matches through a model alias (Saab 9-2X -> Impreza)
A car whose decoded engine differs from every fitting engine is not counted.
"""
import datetime as dt
import os
import sqlite3
from dataclasses import dataclass, field

from .interchange import Engine, all_engines, norm_model

SCHEMA = """
CREATE TABLE IF NOT EXISTS yard (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,          -- 'PICK-n-PULL Lynnwood'
  city TEXT, state TEXT, zip TEXT,
  source TEXT NOT NULL,               -- 'row52'
  location_id INTEGER,                -- Row52 LocationId when known
  url TEXT,
  last_full TEXT                      -- when this yard's whole list was last entered (paste --full)
);
CREATE TABLE IF NOT EXISTS yard_vehicle (
  vin TEXT NOT NULL,
  yard_id INTEGER NOT NULL REFERENCES yard(id),
  year INTEGER, make TEXT, model TEXT,
  row TEXT,                            -- yard row as published
  added TEXT,                          -- date the yard says it was set (ISO)
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,   -- ISO timestamps of our syncs
  url TEXT,
  PRIMARY KEY (vin, yard_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS yard_vehicle_mm ON yard_vehicle(make, model, year);
CREATE TABLE IF NOT EXISTS vin_decode (   -- NHTSA vPIC, Subaru/Saab only
  vin TEXT PRIMARY KEY,
  model TEXT, series TEXT, trim TEXT, body TEXT, drive TEXT,
  displacement_l REAL, cylinders INTEGER, valve_train TEXT, turbo TEXT, engine_model TEXT,
  error TEXT, decoded_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_log (
  run_at TEXT NOT NULL, source TEXT NOT NULL, pages INTEGER, vehicles INTEGER, matched_make INTEGER,
  gone INTEGER, error TEXT
);
"""

DEFAULT_DB = os.path.expanduser("~/subaru_cyberdeck/kb/yards.db")
MAX_AGE_DAYS = 45   # a car only known from an alert or a partial paste stops counting after this (yards crush)


def connect(path=DEFAULT_DB, write=False):
    path = os.path.expanduser(path)
    if write:
        con = sqlite3.connect(path)
        con.executescript(SCHEMA)
        if "last_full" not in [r[1] for r in con.execute("PRAGMA table_info(yard)")]:
            con.execute("ALTER TABLE yard ADD COLUMN last_full TEXT")
        return con
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def engine_from_vpic(disp, cyl, valve, turbo):
    cam = None
    v = (valve or "").upper()
    if "DOHC" in v or "DUAL" in v:
        cam = "DOHC"
    elif "SOHC" in v or "SINGLE" in v:
        cam = "SOHC"
    t = (turbo or "").strip().lower()
    return Engine(cyl or None, round(disp, 1) if disp else None, cam,
                  True if t == "yes" else False if t == "no" else None)


@dataclass
class YardCar:
    vin: str
    year: int
    make: str
    model: str
    row: str
    added: str
    last_seen: str
    url: str
    engine: Engine
    series: str = ""
    level: str = ""
    why: str = ""

    def days_in_yard(self, today=None):
        if not self.added:
            return None
        today = today or dt.date.today()
        try:
            return (today - dt.date.fromisoformat(self.added[:10])).days
        except ValueError:
            return None


@dataclass
class YardResult:
    yard: str
    city: str
    location_id: int
    url: str
    cars: list = field(default_factory=list)

    def count(self, level):
        return sum(c.level == level for c in self.cars)


@dataclass
class YardReport:
    synced_at: str          # last successful sync (ISO) or None
    yards: list
    outside_years: int = 0  # matching-model cars outside CHARM's 1990-2012 coverage

    def age_hours(self):
        if not self.synced_at:
            return None
        t = dt.datetime.fromisoformat(self.synced_at)
        now = dt.datetime.now(t.tzinfo) if t.tzinfo else dt.datetime.now()
        return (now - t).total_seconds() / 3600


LEVEL_ORDER = {"likely": 0, "family": 1, "possible": 2}


def classify(car, fits, engines_by_my, alias=False):
    """Best level for one car against the fits for its (normalized model, year); None if no fit."""
    if not fits:
        return None, ""
    best = None
    for f in fits:
        fe = Engine.from_charm(f.engine)
        base = "family" if f.match == "family" else "likely"
        if car.engine.known() and fe.known():
            if not car.engine.compatible(fe):
                continue
            level, why = base, f"VIN engine {car.engine.label()} = CHARM {f.engine}"
        else:
            year_engines = engines_by_my.get((norm_model(f.model), f.year), [])
            uniq = {e.label() for e in year_engines}
            if len(uniq) <= 1:
                level, why = base, f"only engine that year: {f.engine or '?'}"
            else:
                level, why = "possible", f"engine not decoded; {len(uniq)} engines that year"
        if f.match == "override":
            level, why = "possible", f.note or "hand-added fit"
        if alias:
            level = "possible"
        if best is None or LEVEL_ORDER[level] < LEVEL_ORDER[best[0]]:
            best = (level, why)
    return best if best else (None, "")


def report(yards_db, kb_db, interchange, make="Subaru", today=None):
    """Per-yard matching cars for one Interchange (from interchange.lookup)."""
    con = connect(yards_db)
    try:
        synced = con.execute("SELECT max(run_at) FROM sync_log WHERE error IS NULL").fetchone()[0]
        last_run = synced
        engines_by_my = all_engines(kb_db)
        fits_by_my = {}
        for f in interchange.fits:
            fits_by_my.setdefault((norm_model(f.model), f.year), []).append(f)
        alias_rules = interchange.aliases
        makes = {make.lower()} | {a["make"].lower() for a in alias_rules}
        rows = con.execute(
            "SELECT v.vin, v.year, v.make, v.model, v.row, v.added, v.last_seen, v.url, "
            "y.name, y.city, y.location_id, y.url, d.displacement_l, d.cylinders, d.valve_train, d.turbo, "
            "d.series, d.trim, d.model "
            "FROM yard_vehicle v JOIN yard y ON y.id = v.yard_id LEFT JOIN vin_decode d ON d.vin = v.vin "
            f"WHERE lower(v.make) IN ({','.join('?' * len(makes))}) "
            # Still there: on the yard's latest full list, or added since then and not older than MAX_AGE_DAYS.
            "AND ((y.last_full IS NOT NULL AND v.last_seen >= y.last_full) OR "
            "     (v.first_seen > coalesce(y.last_full, '') AND "
            "      julianday('now') - julianday(substr(coalesce(v.added, v.first_seen), 1, 10)) <= ?))",
            sorted(makes) + [MAX_AGE_DAYS]).fetchall()
        yards = {}
        for (y_name, y_city, y_loc, y_url) in con.execute("SELECT name, city, location_id, url FROM yard ORDER BY name"):
            yards[y_name] = YardResult(y_name, y_city, y_loc, y_url)
    finally:
        con.close()
    outside = 0
    for (vin, year, mk, model, row, added, last_seen, url, y_name, y_city, y_loc, y_url,
         disp, cyl, valve, turbo, series, trim, d_model) in rows:
        car = YardCar(vin, year, mk, model, row, added, last_seen, url,
                      engine_from_vpic(disp, cyl, valve, turbo), " ".join(x for x in (series, trim) if x))
        nm = norm_model(model)
        # vPIC's series/trim catches Outbacks a yard lists as Legacy.
        if nm == "legacy" and "outback" in f"{series} {trim} {d_model}".lower():
            nm = "outback"
        level, why = (None, "")
        if mk.lower() == make.lower():
            level, why = classify(car, fits_by_my.get((nm, year)), engines_by_my)
        for a in alias_rules:
            if (a["make"].lower() == mk.lower() and norm_model(a["model"]) == nm
                    and a["years"][0] <= (year or 0) <= a["years"][1]):
                lv, w = classify(car, fits_by_my.get((norm_model(a["alias_model"]), year)), engines_by_my, alias=True)
                if lv and (level is None or LEVEL_ORDER[lv] < LEVEL_ORDER[level]):
                    level, why = lv, f"{a['note']} ({w})"
        if level is None:
            if year and (year < 1990 or year > 2012) and any(norm_model(f.model) == nm for f in interchange.fits):
                outside += 1
            continue
        car.level, car.why = level, why
        yards.setdefault(y_name, YardResult(y_name, y_city, y_loc, y_url)).cars.append(car)
    for yr in yards.values():
        yr.cars.sort(key=lambda c: (LEVEL_ORDER[c.level], -(c.year or 0)))
    order = sorted(yards.values(), key=lambda r: (-r.count("likely"), -r.count("family"), -r.count("possible"), r.yard))
    return YardReport(last_run, order, outside)


def format_report(rep, max_cars=4, today=None):
    lines = []
    age = rep.age_hours()
    if age is None:
        lines.append("Yard inventory: empty (paste a yard list with yard_paste).")
    else:
        lines.append(f"Yard list last updated {rep.synced_at[:16].replace('T', ' ')} ({age:.0f} h ago)"
                     + ("; cars turn over in 30-45 days, so re-check Row52 before driving out" if age > 72 else ""))
    for y in rep.yards:
        n_l, n_f, n_p = y.count("likely"), y.count("family"), y.count("possible")
        lines.append(f"  {y.yard} ({y.city or '?'}): {n_l} likely, {n_f} same-family, {n_p} possible")
        for c in y.cars[:max_cars]:
            days = c.days_in_yard(today)
            lines.append(f"    - {c.year} {c.make} {c.model} {c.engine.label()}, row {c.row or '?'}"
                         f"{f', in yard {days} d' if days is not None else ''} [{c.level}: {c.why}]")
    if rep.outside_years:
        lines.append(f"  ({rep.outside_years} same-model cars are outside CHARM's 1990-2012 years and weren't rated)")
    return "\n".join(lines)
