"""
Live-telemetry reference: what a reading means for THIS car, worked out in Python from the make's manual.

Both apps (cyberdeck_gui and diagent) import this one module, so they give the same answer:
    normalize()   cable/protocol readings -> one canonical form {key, value, unit, source, raw, raw_unit, t}
    condition()   the operating condition the readings show (key on engine off, idle, a stated rpm ...)
    judge()       each reading against the healthy zone from the car's manual: in / low / high / no zone
    pathways()    where the manual sends you when a reading is out of zone (DTC procedures, inspections)
    summary()     a compact text block for model context, out-of-zone first, every line cited
    capabilities() what each cable can read on this car (defined / confirmed / not_supported)

The data lives in the make DB (kb/<make>.db, tables telem_*), built by kb/build_telemetry.py from open
definitions (RomRaider, FreeSSM, PCMHammer, SAE J1979) and the make's own manual pages. Empty beats wrong:
no zone in the data means status 'no zone', never a guess. Stdlib + sqlite only (runs on the Pi).
Read-only towards the car: nothing here talks to an ECU.
"""
import ast
import json
import math
import os
import re
import sqlite3
import time

# ------------------------------------------------------------------------------------------- units
# Canonical units: kPa absolute for pressures, °C, %, rpm, V, g/s, km/h, degrees, ms, A.
UNIT_ALIASES = {
    "kpa": "kPa", "kilopascal": "kPa", "hpa": "hPa", "pa": "Pa", "pascal": "Pa", "bar": "bar", "mpa": "MPa",
    "psi": "psi", "inhg": "inHg", "mmhg": "mmHg",
    "c": "°C", "°c": "°C", "degc": "°C", "degree_celsius": "°C", "celsius": "°C", "deg c": "°C",
    "f": "°F", "°f": "°F", "degf": "°F", "degree_fahrenheit": "°F", "fahrenheit": "°F", "deg f": "°F",
    "v": "V", "volt": "V", "volts": "V", "vdc": "V", "mv": "mV", "millivolt": "mV", "millivolts": "mV",
    "%": "%", "percent": "%", "pct": "%",
    "rpm": "rpm", "revolutions_per_minute": "rpm",
    "g/s": "g/s", "g/sec": "g/s", "gps": "g/s", "gram / second": "g/s", "grams_per_second": "g/s",
    "lb/min": "lb/min", "lb/m": "lb/min", "kg/h": "kg/h",
    "km/h": "km/h", "kph": "km/h", "kilometer_per_hour": "km/h", "mph": "mph", "mile_per_hour": "mph",
    "deg": "°", "degree": "°", "degrees": "°", "°": "°", "° btdc": "°",
    "ms": "ms", "millisecond": "ms", "milliseconds": "ms", "µs": "µs", "us": "µs",
    "a": "A", "amp": "A", "ampere": "A", "ma": "mA", "s": "s", "second": "s", "seconds": "s",
}
# unit -> (dimension, factor, offset): canonical = value * factor + offset
_UNIT = {
    "kPa": ("pressure", 1.0, 0.0), "hPa": ("pressure", 0.1, 0.0), "Pa": ("pressure", 0.001, 0.0),
    "bar": ("pressure", 100.0, 0.0), "MPa": ("pressure", 1000.0, 0.0), "psi": ("pressure", 6.894757, 0.0),
    "inHg": ("pressure", 3.386389, 0.0), "mmHg": ("pressure", 0.1333224, 0.0),
    "°C": ("temp", 1.0, 0.0), "°F": ("temp", 5 / 9, -32 * 5 / 9),
    "V": ("voltage", 1.0, 0.0), "mV": ("voltage", 0.001, 0.0),
    "g/s": ("massflow", 1.0, 0.0), "lb/min": ("massflow", 7.559873, 0.0), "kg/h": ("massflow", 1 / 3.6, 0.0),
    "km/h": ("speed", 1.0, 0.0), "mph": ("speed", 1.609344, 0.0),
    "ms": ("time_ms", 1.0, 0.0), "µs": ("time_ms", 0.001, 0.0),
    "A": ("current", 1.0, 0.0), "mA": ("current", 0.001, 0.0),
    "%": ("percent", 1.0, 0.0), "rpm": ("rpm", 1.0, 0.0), "°": ("angle", 1.0, 0.0), "s": ("seconds", 1.0, 0.0),
}
CANON_UNIT = {"pressure": "kPa", "temp": "°C", "voltage": "V", "massflow": "g/s", "speed": "km/h",
              "time_ms": "ms", "current": "A", "percent": "%", "rpm": "rpm", "angle": "°", "seconds": "s"}


def unit_norm(u):
    """'kilopascal' / 'KPA' / 'Volts' -> 'kPa' / 'V'. Unknown units come back stripped, unchanged."""
    if u is None:
        return ""
    s = str(u).strip()
    k = s.lower()
    return UNIT_ALIASES.get(k, UNIT_ALIASES.get(k.rstrip("s"), s))


def convert(value, unit, to_unit):
    """value in unit -> to_unit, or None when the two aren't the same kind of quantity (no guessing)."""
    a, b = _UNIT.get(unit_norm(unit)), _UNIT.get(unit_norm(to_unit))
    if value is None or a is None or b is None or a[0] != b[0]:
        return value if unit_norm(unit) == unit_norm(to_unit) and value is not None else None
    canon = value * a[1] + a[2]
    return (canon - b[2]) / b[1]


def canonical(value, unit):
    """(value, unit) in the canonical unit of its dimension; unknown units pass through unchanged."""
    u = unit_norm(unit)
    d = _UNIT.get(u)
    if d is None or value is None:
        return value, u
    cu = CANON_UNIT[d[0]]
    return convert(value, u, cu), cu


# ------------------------------------------------------------------------- safe expression evaluator
_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
        ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b, ast.FloorDiv: lambda a, b: a // b,
        ast.BitAnd: lambda a, b: int(a) & int(b), ast.RShift: lambda a, b: int(a) >> int(b),
        ast.LShift: lambda a, b: int(a) << int(b), ast.BitOr: lambda a, b: int(a) | int(b)}


def evaluate(expr, env):
    """Arithmetic on named inputs only (x, A, B, C, D, ...): the conversions in RomRaider / PCMHammer / J1979
    style. No calls, no attributes: a definition file can't run code."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            return env[n.id]
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        raise ValueError(f"unsupported expression: {expr}")
    return ev(ast.parse(str(expr).strip(), mode="eval"))


def bytes_env(data):
    """Reply data bytes -> {'A': b0, 'B': b1, ..., 'x': big-endian integer of all of them}."""
    data = bytes(data)
    env = {chr(65 + i): b for i, b in enumerate(data)}
    env["x"] = int.from_bytes(data, "big") if data else 0
    return env


# ------------------------------------------------------------------------ canonical parameters
# One row per physical quantity that more than one cable/protocol can report. The ids beside each key are
# the definitions that report the same quantity: SAE J1979 mode 01 PID, RomRaider SSM parameter id, and
# PCMHammer mode $22 parameter id. Quantities only one protocol reports get a namespaced key (ssm.P123,
# gm22.<id>, obd01.<pid>) made by the builder from that definition's own name.
CANON = {
    #  key        name                                 unit    J1979  RomRaider SSM  PCMHammer
    "rpm":       ("Engine speed",                      "rpm",  "0C", ["P8"],  ["EngineSpeed"]),
    "speed":     ("Vehicle speed",                     "km/h", "0D", ["P9"],  ["VehicleSpeedSensor"]),
    "ect":       ("Engine coolant temperature",        "°C",   "05", ["P2"],  ["ECTSensor"]),
    "iat":       ("Intake air temperature",            "°C",   "0F", ["P11"], ["IATSensor"]),
    "map":       ("Manifold absolute pressure",        "kPa",  "0B", ["P7"],  ["MAPSensor"]),
    "baro":      ("Barometric (atmospheric) pressure", "kPa",  "33", ["P24"], ["BarometricPressuresampled"]),
    "map_rel":   ("Manifold pressure relative to barometric (negative = vacuum)", "kPa", None, ["P25"], []),
    "stft1":     ("Short-term fuel trim bank 1",       "%",    "06", ["P3"],  ["ShortTermFTLeftBank", "ShortTermFTBank1"]),
    "ltft1":     ("Long-term fuel trim bank 1",        "%",    "07", ["P4"],  ["LongTermFTLeftBank", "LongTermFTBank1"]),
    "stft2":     ("Short-term fuel trim bank 2",       "%",    "08", ["P5"],  ["ShortTermFTRightBank", "ShortTermFTBank2"]),
    "ltft2":     ("Long-term fuel trim bank 2",        "%",    "09", ["P6"],  ["LongTermFTRightBank", "LongTermFTBank2"]),
    "timing":    ("Ignition timing advance",           "°",    "0E", ["P10"], ["IgnitionTiming"]),
    "maf":       ("Mass air flow",                     "g/s",  "10", ["P12"], ["MAFSensor"]),
    "tps":       ("Throttle position",                 "%",    "11", ["P13"], ["ThrottlePosition"]),
    "load_calc": ("Calculated engine load",            "%",    "04", [],      ["EngineLoad"]),
    "o2_b1s1_v": ("Oxygen sensor bank 1 sensor 1 voltage", "V", "14", ["P14"], ["O2LeftUpstream"]),
    "o2_b1s2_v": ("Oxygen sensor bank 1 sensor 2 voltage", "V", "15", ["P15"], ["O2LeftDownstream"]),
    "o2_b2s1_v": ("Oxygen sensor bank 2 sensor 1 voltage", "V", "18", ["P16"], ["O2RightUpstream"]),
    "o2_b2s2_v": ("Oxygen sensor bank 2 sensor 2 voltage", "V", "19", [],      ["O2RightDownstream"]),
    "ecu_volts": ("Control module supply voltage",     "V",    "42", ["P17"], ["Ignition1Signal"]),
    "fuel_level": ("Fuel level input",                 "%",    "2F", [],      ["FuelLevel"]),
    "maf_v":     ("Mass air flow sensor voltage",      "V",    None, ["P18"], []),
    "tps_v":     ("Throttle position sensor voltage",  "V",    None, ["P19"], ["TPSensor"]),
    "map_v":     ("Manifold pressure sensor voltage",  "V",    None, ["P102"], ["MAPSensorVolts"]),
    "accel_pedal": ("Accelerator pedal position",      "%",    "5A", ["P30"], []),
    "ambient":   ("Ambient air temperature",           "°C",   "46", [],      []),
    "runtime":   ("Run time since engine start",       "s",    "1F", [],      ["EngineRunTime"]),
    "inj_pw1":   ("Fuel injector pulse width",         "ms",   None, ["P21"], ["InjectorPWMLeftBankAverage"]),
    "desired_idle": ("Desired idle speed",             "rpm",  None, [],      ["DesiredIdleSpeed"]),
    "adapter_volts": ("Voltage at the OBD connector (measured by the adapter, pin 16)", "V", None, [], []),
}
# Some adapters measure the connector voltage themselves (ELM/STN ATRV); that is a different measuring
# point from the ECU's own supply-voltage reading, so it gets its own key.

# SAE J1979 mode 01 scalar PIDs: pid -> (bytes, expression in A,B,C,D, unit). Every formula here is
# unit-tested against python-obd's decoder (kb/tests/test_telemetry.py).
OBD01 = {
    "04": (1, "A*100/255", "%"), "05": (1, "A-40", "°C"), "06": (1, "(A-128)*100/128", "%"),
    "07": (1, "(A-128)*100/128", "%"), "08": (1, "(A-128)*100/128", "%"), "09": (1, "(A-128)*100/128", "%"),
    "0A": (1, "A*3", "kPa"), "0B": (1, "A", "kPa"), "0C": (2, "(A*256+B)/4", "rpm"), "0D": (1, "A", "km/h"),
    "0E": (1, "A/2-64", "°"), "0F": (1, "A-40", "°C"), "10": (2, "(A*256+B)/100", "g/s"),
    "11": (1, "A*100/255", "%"),
    "14": (2, "A/200", "V"), "15": (2, "A/200", "V"), "16": (2, "A/200", "V"), "17": (2, "A/200", "V"),
    "18": (2, "A/200", "V"), "19": (2, "A/200", "V"), "1A": (2, "A/200", "V"), "1B": (2, "A/200", "V"),
    "1F": (2, "A*256+B", "s"), "21": (2, "A*256+B", "km"), "22": (2, "(A*256+B)*0.079", "kPa"),
    "23": (2, "(A*256+B)*10", "kPa"), "2C": (1, "A*100/255", "%"), "2D": (1, "(A-128)*100/128", "%"),
    "2E": (1, "A*100/255", "%"), "2F": (1, "A*100/255", "%"), "30": (1, "A", "count"),
    "31": (2, "A*256+B", "km"), "33": (1, "A", "kPa"),
    "3C": (2, "(A*256+B)/10-40", "°C"), "3D": (2, "(A*256+B)/10-40", "°C"), "3E": (2, "(A*256+B)/10-40", "°C"),
    "3F": (2, "(A*256+B)/10-40", "°C"), "42": (2, "(A*256+B)/1000", "V"), "43": (2, "(A*256+B)*100/255", "%"),
    "44": (2, "(A*256+B)*2/65536", "ratio"), "45": (1, "A*100/255", "%"), "46": (1, "A-40", "°C"),
    "47": (1, "A*100/255", "%"), "48": (1, "A*100/255", "%"), "49": (1, "A*100/255", "%"),
    "4A": (1, "A*100/255", "%"), "4B": (1, "A*100/255", "%"), "4C": (1, "A*100/255", "%"),
    "4D": (2, "A*256+B", "min"), "4E": (2, "A*256+B", "min"), "52": (1, "A*100/255", "%"),
    "53": (2, "(A*256+B)/200", "kPa"), "55": (1, "(A-128)*100/128", "%"), "56": (1, "(A-128)*100/128", "%"),
    "57": (1, "(A-128)*100/128", "%"), "58": (1, "(A-128)*100/128", "%"), "59": (2, "(A*256+B)*10", "kPa"),
    "5A": (1, "A*100/255", "%"), "5B": (1, "A*100/255", "%"), "5C": (1, "A-40", "°C"),
    "5D": (2, "((A*256+B)-26880)/128", "°"), "5E": (2, "(A*256+B)*0.05", "L/h"),
    "61": (1, "A-125", "%"), "62": (1, "A-125", "%"), "63": (2, "A*256+B", "Nm"),
}

# Labels of the mode 01 PIDs in the live-data text (app/obd_all.py). Core readings come first, in this order.
OBD01_FIRST = [0x0C, 0x05, 0x06, 0x07, 0x0B, 0x10, 0x0E, 0x04, 0x0D, 0x11, 0x0F]
OBD01_LABELS = {
    "0C": "RPM", "05": "ECT", "06": "STFT1", "07": "LTFT1", "0B": "MAP", "10": "MAF", "0E": "Timing", "04": "Load",
    "0D": "Speed", "11": "TPS", "0F": "IAT", "08": "STFT2", "09": "LTFT2", "0A": "Fuel pressure", "1F": "Run time",
    "21": "Dist with MIL", "22": "Fuel rail vac", "23": "Fuel rail gauge", "2C": "Cmd EGR", "2D": "EGR error",
    "2E": "Cmd evap purge", "2F": "Fuel level", "30": "Warmups since clear", "31": "Dist since clear", "33": "Baro",
    "3C": "Cat temp B1S1", "3D": "Cat temp B2S1", "3E": "Cat temp B1S2", "3F": "Cat temp B2S2", "42": "ECU volts",
    "43": "Abs load", "44": "Cmd lambda", "45": "Rel throttle", "46": "Ambient", "47": "Throttle B", "48": "Throttle C",
    "49": "Pedal D", "4A": "Pedal E", "4B": "Pedal F", "4C": "Cmd throttle", "4D": "Time with MIL",
    "4E": "Time since clear", "52": "Ethanol", "53": "Evap abs pressure", "55": "O2 sec STFT B1", "56": "O2 sec LTFT B1",
    "57": "O2 sec STFT B2", "58": "O2 sec LTFT B2", "59": "Fuel rail abs", "5A": "Rel pedal", "5B": "Hybrid battery",
    "5C": "Oil temp", "5D": "Injection timing", "5E": "Fuel rate", "61": "Demand torque", "62": "Actual torque",
    "63": "Ref torque"}
# O2 voltage labels name the sensor's place, so a car whose PID 1D moves B2S1 to PID 16 still gets the right key
OBD01_O2 = {"O2 B1S1": "14", "O2 B1S2": "15", "O2 B1S3": "16", "O2 B1S4": "17", "O2 B2S1": "18", "O2 B2S2": "19",
            "O2 B2S3": "1A", "O2 B2S4": "1B"}

# Labels the app's live-data text uses (ssm_reader.LIVE for SSM; app/obd_all.py for OBD-II) -> the
# definition id they were read with. 'text' readings are already decoded; the unit comes with the text.
TEXT_IDS = {
    "ssm": {"RPM": "P8", "Speed": "P9", "ECT": "P2", "STFT1": "P3", "LTFT1": "P4", "MAP": "P7", "Timing": "P10",
            "Knock corr": "P23", "IAT": "P11", "TPS": "P13", "O2 front": "P14", "Volts": "P17"},
    "obd01": {**{v: k for k, v in OBD01_LABELS.items()}, **OBD01_O2, "Volts": "ATRV"},
}


# Units of those labels when the text carries none (ssm_reader writes 'RPM 812'): the definitions' own units
# (RomRaider P8 rpm; J1979 PID 0C rpm).
TEXT_UNITS = {"ssm": {"P8": "rpm"}, "obd01": {pid: u for pid, (_, _, u) in OBD01.items()}}


def canon_key(source, ident):
    """Canonical key for one definition id of one source ('ssm' P7 -> 'map', 'obd01' '0B' -> 'map')."""
    ident = str(ident)
    if source == "obd01" and ident.upper() == "ATRV":
        return "adapter_volts"
    for key, (_, _, pid, ssm, gm) in CANON.items():
        if source == "obd01" and pid and ident.upper().lstrip("0X").zfill(2) == pid:
            return key
        if source == "ssm" and ident in ssm:
            return key
        if source == "gm22" and ident in gm:
            return key
    return f"{source}.{ident}"


# --------------------------------------------------------------------------------- DB access
def _ro(path):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


_DB_CACHE = {}


def open_db(path):
    """Read-only handle to a make DB (or a standalone kb/telemetry/<make>.telemetry.db), cached per path."""
    if not path:
        return None
    path = os.path.expanduser(path)
    db = _DB_CACHE.get(path)
    if db is None:
        db = _ro(path)
        if db is not None:
            _DB_CACHE[path] = db
    return db


def has_tables(db_or_path):
    db = open_db(db_or_path) if isinstance(db_or_path, str) else db_or_path
    if db is None:
        return False
    try:
        return db.execute("SELECT 1 FROM sqlite_master WHERE name='telem_zone'").fetchone() is not None
    except sqlite3.Error:
        return False


def _db_for(car, db=None):
    if db is not None:
        return open_db(db) if isinstance(db, str) else db
    return open_db((car or {}).get("db") or "")


# ----------------------------------------------------------------------------------- normalize
def _source_def(db, source, ident):
    """(expr, raw_unit, nbytes, signed) for one definition from telem_source_map, or None."""
    if db is None:
        return None
    try:
        r = db.execute("SELECT conversion, raw_unit, nbytes FROM telem_source_map WHERE source=? AND raw_id=? "
                       "ORDER BY canonical DESC LIMIT 1", (source, str(ident))).fetchone()
    except sqlite3.Error:
        return None
    return (r[0], r[1], r[2]) if r else None


def normalize(raw_readings, source, t=None, db=None):
    """Readings from one cable/protocol -> [{key, value, unit, source, raw, raw_unit, t, id}] in canonical units.

    raw_readings: list of dicts, each one of
        {"id": <definition id>, "bytes": b"..."}           raw reply bytes (decoded here)
        {"id": <definition id>, "raw": <int>}               raw integer x (decoded here)
        {"id": <definition id>, "value": v, "unit": "kPa"} already decoded by the reader
    source: 'ssm' (SSM2 on either cable), 'obd01' (SAE J1979 mode 01), 'gm22' (GM mode $22).
    Decoding needs the definition: J1979 formulas are built in; SSM / GM come from the DB's telem_source_map.
    Adds 'map_rel' (MAP minus barometric) when both are present and the ECU didn't report it itself, computed
    the same way for every cable."""
    t = time.time() if t is None else t
    out = []
    for r in raw_readings or []:
        ident = str(r.get("id", ""))
        key = canon_key(source, ident)
        value, unit, raw, raw_unit = r.get("value"), r.get("unit"), r.get("raw"), r.get("unit") or ""
        if value is None and ("bytes" in r or "raw" in r):
            if source == "obd01" and ident.upper().zfill(2) in OBD01:
                n, expr, unit = OBD01[ident.upper().zfill(2)]
                data = bytes(r["bytes"])[:n] if "bytes" in r else int(raw).to_bytes(n, "big")
                raw, raw_unit = data.hex().upper(), "bytes"
                value = evaluate(expr, bytes_env(data))
            else:
                d = _source_def(db, source, ident)
                if d is None:
                    continue                      # no definition: no number (never guess a scaling)
                expr, unit, n = d
                if "bytes" in r:
                    env = bytes_env(r["bytes"])
                    raw, raw_unit = bytes(r["bytes"]).hex().upper(), "bytes"
                else:
                    env = {"x": int(raw)}
                    raw_unit = "x"
                try:
                    value = evaluate(expr, env)
                except (KeyError, ValueError, ZeroDivisionError):
                    continue
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if raw is None:
            raw = value
        cv, cu = canonical(value, unit)
        if key in CANON and CANON[key][1] != cu and convert(cv, cu, CANON[key][1]) is not None:
            cv, cu = convert(cv, cu, CANON[key][1]), CANON[key][1]
        out.append({"key": key, "value": round(cv, 4), "unit": cu, "source": source, "raw": raw,
                    "raw_unit": raw_unit, "t": t, "id": ident})
    keys = {x["key"] for x in out}
    if "map" in keys and "baro" in keys and "map_rel" not in keys:
        m = next(x for x in out if x["key"] == "map")
        b = next(x for x in out if x["key"] == "baro")
        out.append({"key": "map_rel", "value": round(m["value"] - b["value"], 4), "unit": "kPa", "source": "derived",
                    "raw": f"map {m['value']} - baro {b['value']}", "raw_unit": "kPa", "t": t, "id": "map-baro"})
    return out


_SEG = re.compile(r"^\s*(?P<label>[A-Za-z][A-Za-z0-9 ./_-]*?)\s*[:=]?\s*(?P<v>[+-]?\d+(?:\.\d+)?)\s*(?P<u>[^\d|;,]*)$")


def from_live_text(text, source):
    """The app's live line ('RPM 750 | MAP 33 kilopascal | ECT 85 C') -> raw_readings for normalize().
    Only labels the reader for that source writes are used; anything else is skipped, not guessed."""
    ids = TEXT_IDS.get(source, {})
    out = []
    for seg in re.split(r"[|;]", text or ""):
        m = _SEG.match(seg)
        if not m:
            continue
        ident = ids.get(m.group("label").strip())
        if ident:
            unit = m.group("u").strip() or TEXT_UNITS.get(source, {}).get(ident, "")
            out.append({"id": ident, "value": float(m.group("v")), "unit": unit})
    return out


# ----------------------------------------------------------------------------------- conditions
def _val(readings, key):
    for r in readings or []:
        if r.get("key") == key:
            return r.get("value")
    return None


def _cond_rows(db):
    if db is None:
        return []
    try:
        return [dict(r) for r in db.execute("SELECT name, label, definition, source_kind FROM telem_condition")]
    except sqlite3.Error:
        return []


def _check(defn, readings):
    """True / False when the readings can say, None when a needed value wasn't read."""
    need = []
    for k, test in defn.items():
        if k == "engine":
            rpm = _val(readings, "rpm")
            if rpm is None:
                return None
            need.append({"off": rpm < 50, "running": rpm >= 400, "cranking": 50 <= rpm < 400}.get(test, False))
        elif k in ("rpm", "ect", "tps", "speed", "load_calc", "iat"):
            v = _val(readings, k)
            if v is None:
                return None
            lo, hi = test
            need.append((lo is None or v >= lo) and (hi is None or v <= hi))
    return all(need) if need else None


def conditions(readings, db=None, car=None):
    """Every named condition (from telem_condition) the readings satisfy, most specific first."""
    db = _db_for(car, db)
    hits = []
    for c in _cond_rows(db):
        try:
            defn = json.loads(c["definition"])
        except (TypeError, ValueError):
            continue
        if _check(defn, readings):
            hits.append((len(defn), c["name"]))
    return [n for _, n in sorted(hits, key=lambda x: -x[0])]


def condition(readings, db=None, car=None):
    """The most specific condition the readings show, or None when they can't tell."""
    c = conditions(readings, db, car)
    return c[0] if c else None


# ----------------------------------------------------------------------------------------- judge
def _scope_sql(car, alias="z"):
    car = car or {}
    sql, args = [], []
    model = (car.get("model") or "").strip()
    if model:
        sql.append(f"({alias}.model IS NULL OR upper({alias}.model)=upper(?) OR upper(?) LIKE upper({alias}.model)||' %')")
        args += [model, model]
    else:
        sql.append(f"{alias}.model IS NULL")
    year = car.get("year")
    if str(year or "").isdigit():
        sql.append(f"({alias}.year_min IS NULL OR {alias}.year_min<=?) AND ({alias}.year_max IS NULL OR {alias}.year_max>=?)")
        args += [int(year), int(year)]
    eng = (car.get("engine") or "").strip()
    if eng:
        sql.append(f"({alias}.engine IS NULL OR instr(upper(?), upper({alias}.engine))>0 OR instr(upper({alias}.engine), upper(?))>0)")
        args += [eng, eng]
    return " AND ".join(sql), args


def _citation(db, row):
    if row["section_id"] is None:
        return row["source_ref"] or row["source_kind"]
    try:
        s = db.execute("SELECT d.title, s.page, s.path FROM section s JOIN doc d ON d.id=s.doc_id WHERE s.id=?",
                       (row["section_id"],)).fetchone()
    except sqlite3.Error:
        s = None
    if not s:
        return f"section {row['section_id']}"
    return f"{s['title']}" + (f" p.{s['page']}" if s["page"] else "") + f": {s['path']}"


KIND_RANK = {"manual": 0, "charm": 1, "generic": 2}


def zones_for(key, car, conds, db):
    """Candidate zones for one key under the given conditions, best source first."""
    where, args = _scope_sql(car)
    q = ",".join("?" * len(conds)) or "''"
    try:
        rows = db.execute(f"SELECT z.* FROM telem_zone z WHERE z.param=? AND z.condition IN ({q}) AND {where}",
                          [key] + list(conds) + args).fetchall()
    except sqlite3.Error:
        return []
    rank = {c: i for i, c in enumerate(conds)}
    return sorted(rows, key=lambda r: (KIND_RANK.get(r["source_kind"], 9), r["model"] is None,
                                       rank.get(r["condition"], 99), r["id"]))


def judge(readings, car, condition_name=None, db=None):
    """Each reading against its zone. status: 'in' / 'low' / 'high' / 'no zone'. Python decides, never a model.
    condition_name: None = work it out from the readings. A zone only applies under its own condition."""
    db = _db_for(car, db)
    conds = ([condition_name] if isinstance(condition_name, str) else list(condition_name or [])) \
        or conditions(readings, db, car)
    out = []
    for r in readings or []:
        res = {"key": r["key"], "value": r["value"], "unit": r["unit"], "low": None, "high": None,
               "status": "no zone", "source_kind": None, "citation": None, "quote": None, "condition": None,
               "zone_unit": None}
        if db is not None and conds and has_tables(db):
            for z in zones_for(r["key"], car, conds, db):
                lo = convert(z["low"], z["unit"], r["unit"]) if z["low"] is not None else None
                hi = convert(z["high"], z["unit"], r["unit"]) if z["high"] is not None else None
                if (z["low"] is not None and lo is None) or (z["high"] is not None and hi is None):
                    continue                      # zone in a unit this reading can't be compared with
                v = r["value"]
                res.update(low=lo, high=hi, source_kind=z["source_kind"], citation=_citation(db, z),
                           quote=z["quote"], condition=z["condition"], zone_unit=z["unit"],
                           status="low" if lo is not None and v < lo else "high" if hi is not None and v > hi
                           else "in")
                break
        out.append(res)
    return out


# -------------------------------------------------------------------------------------- pathways
def pathways(key, direction, car, k=5, db=None):
    """Manual sections that diagnose this parameter in this direction (each backed by quoted manual text)."""
    db = _db_for(car, db)
    if db is None or not has_tables(db):
        return []
    where, args = _scope_sql(car, "p")
    try:
        rows = db.execute(
            f"SELECT p.*, s.title AS s_title, s.path AS s_path, s.page AS s_page, d.title AS d_title, d.kind AS d_kind "
            f"FROM telem_path p JOIN section s ON s.id=p.section_id JOIN doc d ON d.id=s.doc_id "
            f"WHERE p.param=? AND p.direction IN (?, 'any') AND {where} "
            f"ORDER BY d.kind='charm', p.direction='any', p.rank, p.id LIMIT ?",
            [key, direction] + args + [int(k)]).fetchall()
    except sqlite3.Error:
        return []
    return [{"section_id": r["section_id"], "title": r["s_title"], "step": r["step"], "method": r["method"],
             "kind": r["kind"], "evidence": r["evidence"],
             "citation": f"{r['d_title']}" + (f" p.{r['s_page']}" if r["s_page"] else "") + f": {r['s_path']}"}
            for r in rows]


# --------------------------------------------------------------------------------------- summary
def _name(key, db=None):
    if key in CANON:
        return CANON[key][0]
    if db is not None:
        try:
            r = db.execute("SELECT name FROM telem_param WHERE key=?", (key,)).fetchone()
            if r:
                return r["name"]
        except sqlite3.Error:
            pass
    return key


def _fmt(x):
    return "?" if x is None else (f"{x:.0f}" if abs(x) >= 100 else f"{x:.2f}".rstrip("0").rstrip("."))


def summary(judged, budget=1500, car=None, db=None, paths_per=2):
    """Compact block for model context: out-of-zone first (with where the manual points), then in-zone, then
    readings with no zone. Every zone line names its source; generic ranges say so."""
    db = _db_for(car, db)
    bad = [j for j in judged if j["status"] in ("low", "high")]
    ok = [j for j in judged if j["status"] == "in"]
    none = [j for j in judged if j["status"] == "no zone"]
    lines = []
    for j in bad:
        zone = f"{_fmt(j['low'])} to {_fmt(j['high'])}" if j["low"] is not None and j["high"] is not None else \
            (f"at least {_fmt(j['low'])}" if j["low"] is not None else f"at most {_fmt(j['high'])}")
        src = "generic range, not from this car's manual" if j["source_kind"] == "generic" else j["citation"]
        lines.append(f"- OUT OF ZONE {j['status'].upper()}: {_name(j['key'], db)} {_fmt(j['value'])} {j['unit']} "
                     f"(healthy {zone} {j['unit']} at {j['condition']}; source: {src})")
        for p in pathways(j["key"], j["status"], car, k=paths_per, db=db) if db is not None else []:
            lines.append(f"    manual points to: {p['title']} [{p['citation']}]" +
                         (f" step {p['step']}" if p["step"] else ""))
    for j in ok:
        src = "generic" if j["source_kind"] == "generic" else j["citation"]
        lines.append(f"- in zone: {_name(j['key'], db)} {_fmt(j['value'])} {j['unit']} "
                     f"({_fmt(j['low'])}..{_fmt(j['high'])}; {src})")
    if none:
        lines.append("- no zone in this car's data for: " + ", ".join(
            f"{_name(j['key'], db)} {_fmt(j['value'])} {j['unit']}" for j in none))
    text = "\n".join(lines)
    if len(text) > budget:
        text = text[:budget].rsplit("\n", 1)[0] + "\n[...]"
    return text


# ---------------------------------------------------------------------------------- capabilities
SEEN_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "telemetry_seen.db")


def _seen(path=None, write=False):
    path = os.path.expanduser(path or SEEN_DB)
    if not write and not os.path.isfile(path):
        return None
    if write:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS seen (car_key TEXT NOT NULL, cable TEXT NOT NULL, protocol TEXT, "
               "source TEXT NOT NULL, raw_id TEXT NOT NULL, status TEXT NOT NULL, first_seen REAL, last_seen REAL, "
               "PRIMARY KEY (car_key, cable, source, raw_id))")
    return db


def car_key(car):
    """VIN first, then ECU id (ROM/CAL), then make/model/year: the key a car's confirmed readings are kept under."""
    car = car or {}
    for k in ("vin", "ecu_id"):
        if car.get(k):
            return f"{k}:{car[k]}"
    return "car:" + "/".join(str(car.get(k) or "") for k in ("make", "model", "year", "engine"))


def record_seen(car, cable, protocol, source, supported, unsupported=(), path=None):
    """What a real car answered (its PID bitmap / SSM capability bits): kept per device, never in the make DB.
    supported / unsupported: definition ids (J1979 PIDs, RomRaider ids, PCMHammer ids)."""
    db = _seen(path, write=True)
    now, ck = time.time(), car_key(car)
    with db:
        for ids, st in ((supported, "confirmed"), (unsupported, "not_supported")):
            for i in ids:
                db.execute("INSERT INTO seen VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(car_key, cable, source, raw_id) "
                           "DO UPDATE SET status=excluded.status, last_seen=excluded.last_seen, protocol=excluded.protocol",
                           (ck, cable, protocol, source, str(i), st, now, now))
    db.close()


def ssm_support(init_payload, db):
    """(supported, unsupported) RomRaider SSM ids for one ECU, from its 0xBF init reply's capability bits
    (telem_source_map.gate 'flag byte i, bit b', 0-based after the SYS ID + ROM ID). Parameters with no
    gate (derived ones) are left out of both lists."""
    p = bytes(init_payload or b"")
    if p[:1] == b"\xff":
        p = p[1:]
    flags = p[8:]
    db = open_db(db) if isinstance(db, str) else db
    yes, no = [], []
    if db is None:
        return yes, no
    try:
        rows = db.execute("SELECT raw_id, gate FROM telem_source_map WHERE source IN ('ssm','ssm_switch') "
                          "AND gate LIKE 'flag byte %'").fetchall()
    except sqlite3.Error:
        return yes, no
    for raw_id, gate in rows:
        m = re.match(r"flag byte (\d+)(?:, bit (\d+))?", gate)
        i = int(m.group(1))
        if m.group(2) is None:                     # switches: the byte's presence gates the whole switch byte
            (yes if i < len(flags) and flags[i] else no).append(raw_id)
            continue
        b = int(m.group(2))
        (yes if i < len(flags) and flags[i] & (1 << b) else no).append(raw_id)
    return yes, no


def capabilities(car, cable, db=None, seen_path=None):
    """[{key, name, source, raw_id, status, origin}] for this car and cable: every value an open definition
    says the car's ECU family can report ('defined'), overlaid with what this car really answered."""
    db = _db_for(car, db)
    rows = {}
    if db is not None and has_tables(db):
        where, args = _scope_sql(car, "c")
        ecu = (car or {}).get("ecu_id") or ""
        try:
            for r in db.execute(f"SELECT c.*, p.name FROM telem_capability c LEFT JOIN telem_param p ON p.key=c.param "
                                f"WHERE c.cable=? AND (c.ecu_id IS NULL OR c.ecu_id=?) AND {where}",
                                [cable, ecu.replace("ROM ", "")] + args):
                rows[(r["source"], r["raw_id"])] = {"key": r["param"], "name": r["name"] or r["param"],
                                                    "source": r["source"], "raw_id": r["raw_id"],
                                                    "status": r["status"], "origin": r["origin"]}
        except sqlite3.Error:
            pass
    sdb = _seen(seen_path)
    if sdb is not None:
        for r in sdb.execute("SELECT * FROM seen WHERE car_key=? AND cable=?", (car_key(car), cable)):
            k = (r["source"], r["raw_id"])
            ent = rows.setdefault(k, {"key": canon_key(r["source"], r["raw_id"]), "name": r["raw_id"],
                                      "source": r["source"], "raw_id": r["raw_id"], "origin": "this car"})
            ent["status"], ent["origin"] = r["status"], "this car answered" if r["status"] == "confirmed" else \
                "this car said not supported"
        sdb.close()
    return sorted(rows.values(), key=lambda x: (x["status"] != "confirmed", x["source"], str(x["key"])))


def coverage(protocol, db):
    """Which cable reads which layer on a car with this detected protocol (telem_coverage rows)."""
    db = open_db(db) if isinstance(db, str) else db
    if db is None or not has_tables(db):
        return []
    try:
        return [dict(r) for r in db.execute("SELECT * FROM telem_coverage WHERE ? LIKE '%'||protocol_match||'%' "
                                            "ORDER BY status='built' DESC, cable", (protocol or "",))]
    except sqlite3.Error:
        return []
