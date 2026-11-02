"""Which vehicles carry an OEM part number, from the CHARM parts/labor rows in kb/subaru.db.

Read-only. Works offline. The source of truth is CHARM's own parts pages: a vehicle "carries" a part
number when a CHARM parts or labor page filed under that vehicle lists it. On top of that:

- family: Subaru numbers are 5 digits + 2 letters + 3 characters (13028AA21C). Revisions of one part
  usually share the first 9 characters (13028AA21A/B/C are the same timing belt, superseded in no
  particular letter order). Vehicles that list a sibling revision are reported as `family`, not `exact`.
- overrides (interchange_overrides.json): hand-checked extra fits such as the Saab 9-2X (a 2005-06
  Impreza), and model aliases (a 1996-99 Legacy wagon in a yard may be the Outback CHARM files it under).

CHARM's parts pages don't list every part for every car, so "not listed" never means "doesn't fit".
"""
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
SUBARU_PN_RE = re.compile(r"^\d{5}[A-Z]{2}[0-9A-Z]{3}$")
# Subaru engine codes -> (cylinders, litres, cam, turbo), for vehicles stored by code instead of CHARM's name.
ENGINE_CODES = {
    "EJ18": (4, 1.8, "SOHC", False), "EJ22": (4, 2.2, "SOHC", False), "EJ20": (4, 2.0, "DOHC", True),
    "EJ205": (4, 2.0, "DOHC", True), "EJ25D": (4, 2.5, "DOHC", False), "EJ251": (4, 2.5, "SOHC", False),
    "EJ252": (4, 2.5, "SOHC", False), "EJ253": (4, 2.5, "SOHC", False), "EJ255": (4, 2.5, "DOHC", True),
    "EJ257": (4, 2.5, "DOHC", True), "EZ30": (6, 3.0, "DOHC", False), "EZ36": (6, 3.6, "DOHC", False),
    "FB20": (4, 2.0, "DOHC", False), "FB25": (4, 2.5, "DOHC", False),
}
ENGINE_RE = re.compile(r"\b([FHVL])([3-8])-(\d\.\d)L\b(.*)$", re.I)


PN_CELL_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{4,19}$")


def is_part_number(s):
    """An OEM part number in any make's format: Subaru 13028AA21C, Ford F7AZ-9H307-AB, Toyota 90919-01253, GM's
    all-digit 19115080. Letters or a dash, or at least 6 digits (so a quantity or a year never counts).
    Same rule as kb/build_subaru_kb.py step_labor."""
    s = (s or "").replace(" ", "").upper()
    return bool(PN_CELL_RE.match(s) and re.search(r"\d", s)
                and (re.search(r"[A-Z\-]", s) or len(s) >= 6))


def pn_from_raw(raw):
    """The part number in a stored CHARM row ('Fuel Pump Relay | GMO | 19115080 | 16.46'). For DBs built before
    all-digit numbers were kept in labor.part_number (GM and others came out empty)."""
    for c in (raw or "").split(" | ")[1:]:
        if is_part_number(c):
            return c.replace(" ", "").upper()[:40]
    return None


def normalize_pn(pn):
    return re.sub(r"[\s\-]", "", (pn or "").upper())


def family_key(pn):
    """First 9 characters of a Subaru-format number, else None (other makes: exact match only)."""
    pn = normalize_pn(pn)
    return pn[:9] if SUBARU_PN_RE.match(pn) else None


def norm_model(model):
    """One spelling per model line, used for both CHARM vehicles and yard cars."""
    s = (model or "").lower().replace("-", " ").strip()
    if "outback" in s:
        return "outback"
    if "tribeca" in s or s.startswith("b9"):
        return "tribeca"
    if "wrx" in s or "sti" == s or "impreza" in s:
        return "impreza"
    return s.split()[0] if s else ""


@dataclass
class Engine:
    cyl: int = None
    disp: float = None
    cam: str = None        # 'SOHC' | 'DOHC' | None
    turbo: bool = None     # None = not stated

    @classmethod
    def from_charm(cls, text):
        """'F4-2.5L SOHC', 'H6-3.0L', 'F4-2.0L DOHC Turbo' (CHARM's engine names)."""
        if not text:
            return cls()
        code = ENGINE_CODES.get(text.strip().upper()[:5]) or ENGINE_CODES.get(text.strip().upper()[:4])
        if code:                     # FSM-loaded vehicles carry an engine code ('EJ251') instead
            return cls(*code)
        m = ENGINE_RE.search(text)
        if not m:
            return cls()
        rest = m.group(4).upper()
        cam = "DOHC" if "DOHC" in rest else "SOHC" if "SOHC" in rest else None
        # CHARM names turbo engines explicitly, so no 'Turbo' on a named engine means naturally aspirated.
        return cls(int(m.group(2)), float(m.group(3)), cam, "TURBO" in rest)

    def known(self):
        return self.disp is not None

    def compatible(self, other):
        """False only when both sides state something that differs."""
        if self.disp is not None and other.disp is not None and abs(self.disp - other.disp) > 0.06:
            return False
        if self.cyl and other.cyl and self.cyl != other.cyl:
            return False
        if self.cam and other.cam and self.cam != other.cam:
            return False
        if self.turbo is not None and other.turbo is not None and self.turbo != other.turbo:
            return False
        return True

    def label(self):
        if not self.known():
            return "engine unknown"
        return f"{self.disp:.1f}L" + (f" {self.cam}" if self.cam else "") + (" turbo" if self.turbo else "")


@dataclass
class Fit:
    model: str            # CHARM model ('Forester')
    year: int
    engine: str           # CHARM engine text or None
    part_number: str      # the number listed for this vehicle
    match: str            # 'exact' | 'family' | 'override'
    operation: str = ""
    price: str = None
    note: str = ""


@dataclass
class Interchange:
    part_number: str
    family: str
    fits: list = field(default_factory=list)
    numbers: dict = field(default_factory=dict)      # every number seen in the family -> description
    aliases: list = field(default_factory=list)      # override rules that apply (yard-side model aliases)

    def years_by_model(self):
        out = {}
        for f in self.fits:
            out.setdefault(f.model, set()).add(f.year)
        return {m: sorted(y) for m, y in out.items()}

    def summary(self):
        """Per model and engine: 'Forester 2.5L SOHC 2002-2010, 2.5L DOHC 2011-2012; ...'."""
        by = {}
        for f in self.fits:
            by.setdefault(norm_model(f.model).title() if f.model != "XT-6" else f.model, {}) \
              .setdefault(Engine.from_charm(f.engine).label(), set()).add(f.year)
        lines = [f"{m} " + ", ".join(f"{e} {compress_years(ys)}" for e, ys in sorted(engs.items(), key=lambda x: min(x[1])))
                 for m, engs in sorted(by.items())]
        if not lines:
            return "no CHARM fitment found"
        donors = {(f.model, f.year, Engine.from_charm(f.engine).label()) for f in self.fits}
        return (f"{len(donors)} donor model-years across {len(by)} models: " + "; ".join(lines))


def compress_years(years):
    years = sorted(set(years))
    out, start = [], None
    for i, y in enumerate(years):
        if start is None:
            start = y
        if i + 1 == len(years) or years[i + 1] != y + 1:
            out.append(str(start) if start == y else f"{start}-{y}")
            start = None
    return ", ".join(out)


def load_overrides(path=None):
    path = path or os.path.join(HERE, "interchange_overrides.json")
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"extra_fits": [], "model_aliases": []}


def lookup(kb_db, part_number, overrides=None):
    """All CHARM vehicles listing this part number (exact) or a sibling revision (family)."""
    pn = normalize_pn(part_number)
    fam = family_key(pn)
    overrides = overrides if overrides is not None else load_overrides()
    ic = Interchange(pn, fam)
    con = sqlite3.connect(f"file:{os.path.expanduser(kb_db)}?mode=ro", uri=True)
    try:
        if fam:
            where, args = "substr(replace(l.part_number,'-',''),1,9) = ?", [fam]
        else:
            where = "(replace(l.part_number,'-','') = ? OR (l.part_number IS NULL AND replace(l.raw,'-','') LIKE ?))"
            args = [pn, f"%{pn}%"]
        rows = con.execute(
            "SELECT DISTINCT l.part_number, l.raw, l.operation, l.price, v.model, v.year, v.engine "
            "FROM labor l JOIN section_vehicle sv ON sv.section_id = l.section_id "
            f"JOIN vehicle v ON v.id = sv.vehicle_id WHERE {where} ORDER BY v.model, v.year", args).fetchall()
    finally:
        con.close()
    rows = [(normalize_pn(n or pn_from_raw(raw)), op, price, model, year, engine)
            for n, raw, op, price, model, year, engine in rows]
    rows = [r for r in rows if r[0] == pn or (fam and r[0][:9] == fam)]
    best = {}
    for num, op, price, model, year, engine in rows:
        ic.numbers.setdefault(num, op.split(" > ")[-1] if op else "")
        match = "exact" if num == pn else "family"
        key = (model, year, engine)
        if key not in best or (match == "exact" and best[key].match != "exact"):
            best[key] = Fit(model, year, engine, num, match, op, price)
    ic.fits = sorted(best.values(), key=lambda f: (f.model, f.year, f.engine or ""))
    # Hand-checked extra fits keyed by part number or family (e.g. a part also used on another model).
    for x in overrides.get("extra_fits", []):
        if normalize_pn(x.get("part_number")) in (pn,) or (fam and x.get("family") == fam):
            for y in range(x["years"][0], x["years"][1] + 1):
                ic.fits.append(Fit(x["model"], y, x.get("engine"), pn, "override", note=x.get("note", "")))
    ic.aliases = overrides.get("model_aliases", [])
    return ic


def all_engines(kb_db):
    """{(norm_model, year): [Engine, ...]} for every CHARM vehicle: tells whether a model-year
    had one engine (so a yard car without a VIN decode still matches) or several."""
    con = sqlite3.connect(f"file:{os.path.expanduser(kb_db)}?mode=ro", uri=True)
    try:
        out = {}
        for model, year, engine in con.execute("SELECT DISTINCT model, year, engine FROM vehicle"):
            out.setdefault((norm_model(model), year), []).append(Engine.from_charm(engine))
        return out
    finally:
        con.close()


def parts_for_job(kb_db, term, model=None, year=None, limit=12):
    """Part numbers for a job on one car, e.g. parts_for_job(db, 'exhaust manifold gasket', 'Forester', 2002).
    Returns [(part_number, operation, price)] from CHARM parts/labor rows."""
    words = [w for w in re.findall(r"[a-z0-9]{3,}", term.lower())][:6]
    if not words:
        return []
    con = sqlite3.connect(f"file:{os.path.expanduser(kb_db)}?mode=ro", uri=True)
    try:
        sql = ("SELECT DISTINCT coalesce(l.part_number, l.raw), l.operation, l.price FROM labor l "
               "WHERE (l.part_number IS NOT NULL OR l.price IS NOT NULL) AND "
               + " AND ".join("lower(l.operation) LIKE ?" for _ in words))
        args = [f"%{w}%" for w in words]
        if model and year:
            sql += (" AND l.section_id IN (SELECT sv.section_id FROM section_vehicle sv JOIN vehicle v "
                    "ON v.id = sv.vehicle_id WHERE v.model = ? AND v.year = ?)")
            args += [model, year]
        rows = con.execute(sql + " LIMIT 500", args).fetchall()
    finally:
        con.close()
    rows = [(pn if is_part_number(pn) else pn_from_raw(pn), op, price) for pn, op, price in rows]
    rows = [r for r in rows if r[0]]
    # Every job word must be in the last 3 path parts (the part itself, not 'Engine, Cooling and Exhaust'
    # above it), so 'exhaust manifold gasket' doesn't also return the intake manifold gasket.
    def tail(op, n):
        return " ".join(op.lower().split(" > ")[-n:])
    rows = ([r for r in rows if all(w in tail(r[1], 2) for w in words)]
            or [r for r in rows if all(w in tail(r[1], 3) for w in words)])
    rows.sort(key=lambda r: -sum(w in " ".join(r[1].lower().split(" > ")[-2:]) for w in words))
    seen, out = set(), []
    for pn, op, price in rows:
        if pn not in seen:
            seen.add(pn)
            out.append((pn, op, price))
    return out[:limit]
