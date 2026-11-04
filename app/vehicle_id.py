"""
Which car is this, and which knowledge database goes with it. Offline, no network.

One knowledge DB per make: kb/subaru.db, later kb/chevrolet.db, kb/toyota.db ...
The make comes from (in this order):
  1. a VIN read over OBD-II (mode 09) or typed into Intake,
  2. the SSM init reply on the KKL cable (only Subaru ECUs speak SSM),
  3. the Make dropdown.

VIN decoding: if kb/vin.db exists (NHTSA vPIC converted on the PC, see VIN_DECODING.md) it is used for
the make; otherwise a built-in table of common WMIs. Year comes from VIN position 10 for every make.
Model is filled in only where it is unambiguous (Subaru chassis code, positions 4-5).
"""
import json
import os
import re
import sqlite3

MAKES = ["Subaru", "Acura", "Audi", "BMW", "Buick", "Cadillac", "Chevrolet", "Chrysler", "Dodge", "Ford", "GMC",
         "Honda", "Hyundai", "Infiniti", "Jeep", "Kia", "Lexus", "Lincoln", "Mazda", "Mercedes-Benz", "Mercury",
         "Mini", "Mitsubishi", "Nissan", "Pontiac", "Porsche", "Ram", "Saab", "Saturn", "Scion", "Suzuki",
         "Toyota", "Volkswagen", "Volvo", "Alfa Romeo", "Dacia", "Fiat", "Jaguar", "Lada", "Land Rover", "Renault",
         "Seat", "Skoda", "Smart"]

# Other file names a make's DB may have been saved under (first existing file wins).
ALIASES = {"chevrolet": ["chevy"], "volkswagen": ["vw"], "mercedes-benz": ["mercedes", "mercedes_benz"],
           "mini": ["mini_cooper"], "ram": ["dodge_ram", "dodge"]}

# Built-in World Manufacturer Identifiers (first 3 VIN chars). kb/vin.db, when present, covers every make.
WMI = {
    "JF1": "Subaru", "JF2": "Subaru", "JF3": "Subaru", "4S3": "Subaru", "4S4": "Subaru", "4S6": "Subaru",
    "1G1": "Chevrolet", "1GC": "Chevrolet", "1GN": "Chevrolet", "2G1": "Chevrolet", "3GN": "Chevrolet",
    "1GT": "GMC", "1GK": "GMC", "2GT": "GMC", "1G4": "Buick", "1G6": "Cadillac", "1G2": "Pontiac",
    "1G8": "Saturn", "1FA": "Ford", "1FB": "Ford", "1FC": "Ford", "1FD": "Ford", "1FM": "Ford", "1FT": "Ford",
    "2FA": "Ford", "2FM": "Ford", "3FA": "Ford", "1LN": "Lincoln", "1ME": "Mercury",
    "1C3": "Chrysler", "2C3": "Chrysler", "1B3": "Dodge", "1D7": "Dodge", "2B3": "Dodge", "2D4": "Dodge",
    "3D7": "Dodge", "1C4": "Jeep", "1J4": "Jeep", "1J8": "Jeep", "1C6": "Ram", "3C6": "Ram",
    "1HG": "Honda", "2HG": "Honda", "5FN": "Honda", "JHM": "Honda", "SHH": "Honda", "19U": "Acura",
    "JH4": "Acura", "JT2": "Toyota", "JT3": "Toyota", "JTD": "Toyota", "JTE": "Toyota", "JTM": "Toyota",
    "JTN": "Toyota", "4T1": "Toyota", "4T3": "Toyota", "5TD": "Toyota", "5TF": "Toyota", "2T1": "Toyota",
    "JTH": "Lexus", "JTJ": "Lexus", "2T2": "Lexus", "JN1": "Nissan", "JN8": "Nissan", "1N4": "Nissan",
    "1N6": "Nissan", "5N1": "Nissan", "3N1": "Nissan", "JNK": "Infiniti", "JNR": "Infiniti",
    "JM1": "Mazda", "JM3": "Mazda", "1YV": "Mazda", "JA3": "Mitsubishi", "JA4": "Mitsubishi",
    "4A3": "Mitsubishi", "JS2": "Suzuki", "JS3": "Suzuki", "KMH": "Hyundai", "5NP": "Hyundai",
    "5NM": "Hyundai", "KNA": "Kia", "KND": "Kia", "5XY": "Kia", "WVW": "Volkswagen", "WVG": "Volkswagen",
    "3VW": "Volkswagen", "1VW": "Volkswagen", "WAU": "Audi", "WA1": "Audi", "WBA": "BMW", "WBS": "BMW",
    "WBX": "BMW", "5UX": "BMW", "4US": "BMW", "WMW": "Mini", "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz",
    "WDC": "Mercedes-Benz", "4JG": "Mercedes-Benz", "WP0": "Porsche", "WP1": "Porsche", "YV1": "Volvo",
    "YV4": "Volvo", "YS3": "Saab", "JTK": "Scion",
}

# Subaru chassis code (VIN positions 4-5) -> model. BE/BH/BL/BP/BR/BS are Legacy or Outback: not guessed.
SUBARU_LINES = {"SF": "Forester", "SG": "Forester", "SH": "Forester", "SJ": "Forester", "SK": "Forester",
                "GC": "Impreza", "GF": "Impreza", "GD": "Impreza", "GG": "Impreza", "GE": "Impreza",
                "GH": "Impreza", "GJ": "Impreza", "GP": "Impreza", "BT": "Baja", "WX": "Tribeca",
                "CX": "SVX", "GV": "WRX", "VA": "WRX"}

YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # position 10, 1980..2009 (+30 when position 7 is a letter)
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def slug(make):
    return re.sub(r"[^a-z0-9-]+", "_", (make or "").strip().lower()).strip("_")


def kb_path_for(make, kb_dir):
    """(path, exists) of a make's knowledge DB. path is the preferred name even when it doesn't exist yet."""
    s = slug(make)
    if not s:
        return "", False
    names = [s] + ALIASES.get(s, []) + ([s.replace("-", "_")] if "-" in s else [])
    for n in names:
        p = os.path.join(os.path.expanduser(kb_dir), f"{n}.db")
        if os.path.isfile(p):
            return p, True
    return os.path.join(os.path.expanduser(kb_dir), f"{s}.db"), False


def clean_vin(text):
    return re.sub(r"[\s-]", "", (text or "")).upper()


def check_digit_ok(vin):
    """North American check digit (position 9). Advisory only: some imports don't use it."""
    trans = {**{str(d): d for d in range(10)},
             **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMNPR", (1, 2, 3, 4, 5, 7, 9))),
             **dict(zip("STUVWXYZ", range(2, 10)))}
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    total = sum(trans[c] * w for c, w in zip(vin, weights))
    want = "X" if total % 11 == 10 else str(total % 11)
    return vin[8] == want


def vin_year(vin):
    i = YEAR_CODES.find(vin[9])
    if i < 0:
        return None
    year = 1980 + i
    if vin[6].isalpha():  # from 2010 on, makers put a letter in position 7 for light vehicles
        year += 30
    return year


def make_from_vin_db(wmi, vin_db):
    """Make from kb/vin.db (table wmi(wmi, make), built from NHTSA vPIC on the PC). None if absent."""
    if not vin_db or not os.path.isfile(os.path.expanduser(vin_db)):
        return None
    try:
        with sqlite3.connect(f"file:{os.path.expanduser(vin_db)}?mode=ro", uri=True) as c:
            row = c.execute("SELECT make FROM wmi WHERE wmi = ?", (wmi,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def decoded_from_vin_db(vin, vin_db):
    """A VIN the PC already decoded through NHTSA vPIC (kb/vin.db table vin_decoded), else None."""
    if not vin_db or not os.path.isfile(os.path.expanduser(vin_db)):
        return None
    try:
        with sqlite3.connect(f"file:{os.path.expanduser(vin_db)}?mode=ro", uri=True) as c:
            return c.execute("SELECT make, model, year, engine FROM vin_decoded WHERE vin = ?", (vin,)).fetchone()
    except sqlite3.Error:
        return None


NHTSA_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{}?format=json"


def _nhtsa_cache(cache_path):
    try:
        with open(os.path.expanduser(cache_path)) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def nhtsa_cached(vin, cache_path):
    """A VIN decoded earlier through NHTSA (kept on this deck), else None. Works offline."""
    return _nhtsa_cache(cache_path).get(vin) if cache_path else None


def nhtsa_decode(vin, cache_path, timeout=6):
    """NHTSA vPIC's free decoder (needs internet once per car): {'make', 'model', 'year', 'trim', 'engine',
    'trans', 'drive', 'body'} with only what NHTSA filled in, saved to cache_path for offline use.
    None when there's no connection or NHTSA doesn't know the VIN."""
    hit = nhtsa_cached(vin, cache_path)
    if hit:
        return hit
    try:  # NHTSA's standalone database restored on this machine (tools/vpic_setup.sh): offline, same answer
        from vpic_local import vpic_decode
        out = vpic_decode(vin)
    except ImportError:
        out = None
    if out:
        return _nhtsa_save(vin, out, cache_path)
    import urllib.request
    try:
        with urllib.request.urlopen(NHTSA_URL.format(vin), timeout=timeout) as r:
            res = (json.load(r).get("Results") or [{}])[0]
    except Exception:
        return None
    g = lambda k: (res.get(k) or "").strip()   # noqa: E731
    if not g("Make"):
        return None
    disp = g("DisplacementL")
    disp = f"{float(disp):.1f}L" if re.fullmatch(r"\d+(\.\d+)?", disp) else ""
    layout = {"V-Shaped": "V", "In-Line": "I", "Horizontally opposed (boxer)": "H"}.get(g("EngineConfiguration"), "")
    cyl = f"{layout}{g('EngineCylinders')}" if g("EngineCylinders") else ""
    engine = " ".join(x for x in (disp, cyl, g("EngineModel"), "turbo" if g("Turbo") == "Yes" else "") if x)
    speeds = f"{g('TransmissionSpeeds')}-speed " if g("TransmissionSpeeds") else ""
    out = {"make": canon_make(g("Make")), "model": g("Model"), "year": int(g("ModelYear")) if g("ModelYear").isdigit()
           else None, "trim": g("Trim"), "engine": engine,
           "trans": (speeds + g("TransmissionStyle")).strip(), "drive": g("DriveType"), "body": g("BodyClass")}
    return _nhtsa_save(vin, out, cache_path)


def _nhtsa_save(vin, out, cache_path):
    cache = _nhtsa_cache(cache_path)
    cache[vin] = out
    try:
        os.makedirs(os.path.dirname(os.path.expanduser(cache_path)), exist_ok=True)
        with open(os.path.expanduser(cache_path), "w") as f:
            json.dump(cache, f, indent=1)
    except OSError:
        pass
    return out


def canon_make(make):
    """'CHEVROLET' / 'chevrolet' -> 'Chevrolet', the spelling the Make list uses."""
    for m in MAKES:
        if m.lower() == (make or "").strip().lower():
            return m
    return (make or "").strip().title()


def decode_vin(text, vin_db=None, nhtsa_cache=None):
    """{'vin', 'make', 'year', 'model', 'note'}; make/year/model are '' / None when unknown."""
    vin = clean_vin(text)
    out = {"vin": vin, "make": "", "year": None, "model": "", "engine": "", "note": ""}
    if not VIN_RE.match(vin):
        out["note"] = "A VIN is 17 letters/digits, without I, O or Q."
        return out
    out["make"] = canon_make(make_from_vin_db(vin[:3], vin_db) or WMI.get(vin[:3], ""))
    out["year"] = vin_year(vin)
    if out["make"] == "Subaru":
        out["model"] = SUBARU_LINES.get(vin[3:5], "")
    full = decoded_from_vin_db(vin, vin_db)
    if full:  # NHTSA's own decode wins: make, model, year, engine
        out["make"] = canon_make(full[0]) or out["make"]
        out["model"] = full[1] or out["model"]
        out["year"] = full[2] or out["year"]
        out["engine"] = full[3] or ""
    saved = nhtsa_cached(vin, nhtsa_cache) if nhtsa_cache else None
    if saved:  # decoded online once before: everything NHTSA gave, offline
        for k in ("make", "model", "year", "engine", "trans", "trim", "drive"):
            if saved.get(k):
                out[k] = saved[k]
    notes = []
    if not out["make"]:
        notes.append(f"maker code {vin[:3]} not known here; pick the Make by hand")
    if not check_digit_ok(vin):
        notes.append("check digit doesn't match (typo? non-US cars may not use it)")
    out["note"] = "; ".join(notes)
    return out


def make_from_ssm(ssm_id):
    """Any ECU that answers the SSM init (0xBF) on the K-line is a Subaru."""
    return "Subaru" if ssm_id else ""


# ------------------------------------------------------------------ Subaru ECU id -> year and model
# Cars before ~2005 rarely give a VIN over any cable, but every Subaru ECU sends its 5-byte ECU id in the SSM
# init reply. RomRaider's community ECU definitions list, per ECU id, the car it was built for (year, model,
# market, transmission). build_rom_db() turns those XML files into kb/romid.db once on the PC; the deck
# only reads that small table.
def build_rom_db(xml_paths, out_db):
    """RomRaider ECU definition XML file(s) -> sqlite table rom(ecuid, xmlid, year, make, model, submodel,
    market, transmission). Returns the row count."""
    import xml.etree.ElementTree as ET
    rows = {}
    for path in xml_paths:
        for rom in ET.parse(path).getroot().iter("romid"):
            get = lambda tag: (rom.findtext(tag) or "").strip()   # noqa: E731
            ecuid = get("ecuid").upper()
            if not ecuid:
                continue
            year = get("year")
            if year.isdigit() and len(year) == 2:
                year = str(1900 + int(year) if int(year) > 80 else 2000 + int(year))
            rows[ecuid] = (ecuid, get("xmlid").upper(), year if year.isdigit() else "", get("make"),
                           get("model"), get("submodel"), get("market"), get("transmission"))
    tmp = out_db + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    with sqlite3.connect(tmp) as c:
        c.execute("CREATE TABLE rom (ecuid TEXT PRIMARY KEY, xmlid TEXT, year TEXT, make TEXT, model TEXT, "
                  "submodel TEXT, market TEXT, transmission TEXT)")
        c.executemany("INSERT INTO rom VALUES (?,?,?,?,?,?,?,?)", rows.values())
    os.replace(tmp, out_db)
    return len(rows)


def car_from_rom(rom_id, rom_db):
    """{'year', 'model', 'submodel', 'market', 'transmission'} for an SSM ECU id, or None when the table
    doesn't have it (or isn't on this deck). Year and model are only what the definition says."""
    path = os.path.expanduser(rom_db or "")
    if not rom_id or not os.path.isfile(path):
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
            row = c.execute("SELECT year, model, submodel, market, transmission FROM rom WHERE ecuid = ?",
                            (rom_id.strip().upper(),)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    year, model, sub, market, trans = row
    return {"year": int(year) if year and year.isdigit() else None, "model": model or "", "submodel": sub or "",
            "market": market or "", "transmission": trans or ""}


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--build-romdb":
        # python3 vehicle_id.py --build-romdb kb/romid.db defs1.xml [defs2.xml ...]
        print(build_rom_db(sys.argv[3:], sys.argv[2]), "ECU ids")
    else:
        print("usage: vehicle_id.py --build-romdb <out.db> <RomRaider ECU definition .xml> ...")
