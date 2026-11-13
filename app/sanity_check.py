"""
DATA CHECK: problems in the case's own data, found in Python before any model sees it (no LLM).

    flags = check(case, retriever, ref_db=None)   -> [{kind, item, reason, source}]
    text  = block(flags)                          -> the DATA CHECK block for the prompts ('' when nothing is wrong)

Three checks, each driven by the car's own DB or by plain physics, never by a symptom -> cause list:
  codes      flags: a malformed code, a duplicate with two statuses, a manufacturer-defined code (SAE J2012 ranges)
             that neither the make's code list nor this car's manual has. Notes (not data problems, shown apart):
             a plain duplicate, a valid code with no diagnostic page for this car in the DB.
  live data  a reading outside its sensor's own range (from the DB's telem_source_map conversions, else SAE J1979's
             formulas) or outside a generic physical range (PHYSICAL below); the same signal read twice with
             different values; readings taken together that physics rules out (engine at 0 rpm while the car moves,
             or while air flows, the alternator charges or the fuel trim is working)
  equipment  a component the case names that the make's own data knows as a component (labor operations, spec
             components, code names of kb/<make>.db, when that file is there) but that this car's manual never
             or almost never (max_car pages) mentions. The words come from the case text, not from a list of parts.

case: {"dtcs", "dtcs_raw", "telemetry", "symptoms", "history", "ruled_out", "suspect", "intake_note"}; any may be ''.
Stdlib + sqlite only (runs on the deck).
"""
import os
import re
import sqlite3

# Generic physical limits: what a road vehicle's signal can physically read, not what is healthy, never a cause.
# key: (low, high, unit, why)
PHYSICAL = {
    "rpm": (0, 10000, "rpm", "a road engine's speed"),
    "speed": (0, 300, "km/h", "a road vehicle's speed"),
    "ect": (-40, 150, "°C", "liquid coolant: colder than a start-up ambient or past boiling under cap pressure"),
    "iat": (-40, 120, "°C", "intake air: colder than ambient air gets or hotter than under-hood air gets"),
    "ecu_volts": (6, 16.5, "V", "a 12 V system: under cranking dip or above any charging regulator setting"),
    "load_calc": (0, 100, "%", "calculated load is a percentage of the engine's capacity"),
    "tps": (0, 100, "%", "throttle position is a percentage"),
    "stft1": (-100, 100, "%", "fuel trim is a percentage correction"), "ltft1": (-100, 100, "%", "fuel trim is a percentage correction"),
    "stft2": (-100, 100, "%", "fuel trim is a percentage correction"), "ltft2": (-100, 100, "%", "fuel trim is a percentage correction"),
    "maf": (0, None, "g/s", "air flow can't be negative"),
    "map": (0, None, "kPa", "absolute pressure can't be negative"),
}
# Typed/scanned live-line labels -> canonical keys (telemetry_ref's names)
LABELS = {"rpm": "rpm", "idle": "rpm", "engine speed": "rpm", "speed": "speed", "vss": "speed", "ect": "ect",
          "coolant": "ect", "iat": "iat", "intake air": "iat", "stft1": "stft1", "stft": "stft1", "ltft1": "ltft1",
          "ltft": "ltft1", "stft2": "stft2", "ltft2": "ltft2", "map": "map", "timing": "timing", "tps": "tps",
          "throttle": "tps", "o2 front": "o2_b1s1_v", "o2 b1s1": "o2_b1s1_v", "o2 b1s2": "o2_b1s2_v",
          "o2 b2s1": "o2_b2s1_v", "o2 b2s2": "o2_b2s2_v", "o2 rear": "o2_b1s2_v", "volts": "ecu_volts",
          "battery": "ecu_volts", "ecu volts": "ecu_volts", "load": "load_calc", "maf": "maf", "baro": "baro"}
_UNITS = {"rpm": "rpm", "speed": "km/h", "ect": "°C", "iat": "°C", "stft1": "%", "ltft1": "%", "stft2": "%",
          "ltft2": "%", "map": "kPa", "timing": "°", "tps": "%", "ecu_volts": "V", "load_calc": "%", "maf": "g/s",
          "baro": "kPa", "o2_b1s1_v": "V", "o2_b1s2_v": "V", "o2_b2s1_v": "V", "o2_b2s2_v": "V"}
_NAMES = {"rpm": "engine speed", "speed": "vehicle speed", "ect": "coolant temp", "iat": "intake air temp",
          "ecu_volts": "system voltage", "load_calc": "calculated load", "maf": "mass air flow", "tps": "throttle",
          "map": "manifold pressure", "stft1": "short-term fuel trim", "ltft1": "long-term fuel trim"}
CODE_LIKE = re.compile(r"\b[PBCU](?=[0-9A-Z]*\d)[0-9A-Z]{2,6}\b", re.I)   # needs a digit: not "PENDING"
CODE_OK = re.compile(r"^[PBCU][0-3][0-9A-F]{3}$")
STATUS = re.compile(r"\(\s*(pending|history|permanent|stored|current|confirmed|active|intermittent)\s*\)", re.I)
_NUM = r"[+-]?\d+(?:\.\d+)?"


def _name(key):
    return _NAMES.get(key, key)


# ----------------------------------------------------------------------------------------------- codes
def parse_codes(text):
    """[(code, status or '')] from 'P0420 (pending), P0141' as typed; malformed code-like tokens kept as is."""
    out = []
    for m in CODE_LIKE.finditer(text or ""):
        tail = (text or "")[m.end():m.end() + 24]
        st = STATUS.match(tail.lstrip(" :-"))
        out.append((m.group(0).upper(), st.group(1).lower() if st else ""))
    return out


def _has(db, table):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def generic_code(code):
    """True for a code SAE J2012 reserves for generic (all-makes) use, by its structure: P0, P2, P34-P39, B0, C0,
    U0. The rest (P1, P30-P33, B1/B2, C1/C2, U1/U2, x3) are manufacturer-defined."""
    c = code.upper()
    if c[0] == "P":
        return c[1] in "02" or (c[1] == "3" and c[2] in "456789")
    return c[1] == "0"


def check_codes(text, r):
    """Flags (level 'flag'): malformed, a duplicate with two statuses, or a manufacturer code this make's code
    list doesn't have. Notes (level 'note'): a plain duplicate, or a valid code with no diagnostic page for this
    car in the DB (a coverage gap of the DB, not a data problem)."""
    flags, seen = [], {}
    db = getattr(r, "db", None)
    for code, st in parse_codes(text):
        if not CODE_OK.match(code):
            flags.append({"level": "flag", "kind": "code", "item": code, "source": "OBD-II code format (SAE J2012)",
                          "reason": "not a valid trouble code (a letter P/B/C/U, then 0-3, then three hex digits); "
                                    "re-read it from the scan tool"})
            continue
        if code in seen:
            if seen[code] != st:
                flags.append({"level": "flag", "kind": "code", "item": code, "source": "the case's code list",
                              "reason": f"given twice with different status ({seen[code] or 'no status'} and "
                                        f"{st or 'no status'}); confirm what the scan tool shows"})
            else:
                flags.append({"level": "note", "kind": "code", "item": code, "source": "the case's code list",
                              "reason": "listed twice"})
            continue
        seen[code] = st
        if db is None:
            continue
        desc = _has(db, "dtc") and db.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone()
        vf, args = r._veh_filter("d.section_id") if hasattr(r, "_veh_filter") else ("", [])
        page = _has(db, "dtc_procedure") and db.execute(
            f"SELECT 1 FROM dtc_procedure d WHERE d.code=?{vf} LIMIT 1", [code] + list(args)).fetchone()
        if page:
            continue
        if not desc and not generic_code(code):
            flags.append({"level": "flag", "kind": "code", "item": code,
                          "source": "SAE J2012 (manufacturer-defined range) and this car's DB (dtc, dtc_procedure)",
                          "reason": "a manufacturer-defined code that neither this make's code list nor this car's "
                                    "manual has: check it was read from this car (right car, right module, not a typo)"})
            continue
        name = (desc[0] if desc and desc[0] else "")
        named = _has(db, "car_dtc") and db.execute("SELECT 1 FROM car_dtc WHERE code=? AND named=1",
                                                   (code,)).fetchone()   # kb/car_db.py scope
        flags.append({"level": "note", "kind": "code", "item": code, "source": "this car's DB (dtc_procedure"
                      + (", car_dtc)" if named else ")"),
                      "reason": ("named on this car's manual pages, but " if named else "")
                                + "no manual procedure found for it in this DB; use the "
                                + ("generic definition" if generic_code(code) else "make's definition")
                                + (f" ({name})" if name else "")})
    return flags


# ------------------------------------------------------------------------------------------- live data
def parse_live(text):
    """[(key, value, label as written)] for every reading in the line, ranges ('0.1-0.9 V') as two readings.
    Unknown labels are skipped, never guessed."""
    out = []
    for seg in re.split(r"[|;,]", text or ""):
        s = seg.strip()
        low = s.lower()
        for lab in sorted(LABELS, key=len, reverse=True):
            if low.startswith(lab) and (len(low) == len(lab) or not low[len(lab)].isalpha()):
                rest = s[len(lab):]
                m = re.match(rf"\s*[:=]?\s*({_NUM})(?:\s*(?:-|–|to)\s*({_NUM}))?", rest)
                if m:
                    for g in (m.group(1), m.group(2)):
                        if g is not None:
                            out.append((LABELS[lab], float(g), s))
                break
    return out


def sensor_ranges(db):
    """{key: (low, high)} each signal's own range: every value its definitions can encode (the DB's
    telem_source_map conversions plus SAE J1979's mode 01 formulas)."""
    import telemetry_ref as T
    rows = [(T.canon_key("obd01", pid), expr, n) for pid, (n, expr, _) in T.OBD01.items()]
    if db is not None and _has(db, "telem_source_map"):
        rows += db.execute("SELECT param, conversion, nbytes FROM telem_source_map WHERE conversion IS NOT NULL "
                           "AND nbytes BETWEEN 1 AND 4").fetchall()
    out = {}
    for key, expr, n in rows:
        try:
            vals = [float(T.evaluate(expr, T.bytes_env(data))) for data in (bytes(n), bytes([255] * n))]
        except Exception:
            continue
        lo, hi = min(vals), max(vals)
        if key in out:
            lo, hi = min(lo, out[key][0]), max(hi, out[key][1])
        out[key] = (lo, hi)
    return out


def _fmt(x):
    return f"{x:g}"


def check_live(text, r):
    flags = []
    vals = parse_live(text)
    if not vals:
        return flags
    db = getattr(r, "db", None)
    ranges = sensor_ranges(db)
    src_rng = "the signal's own range (this car's DB telem_source_map conversions)" if db is not None and \
        _has(db, "telem_source_map") else "the signal's own range (SAE J1979 mode 01 formula)"
    by = {}
    for key, v, lab in vals:
        by.setdefault(key, []).append((v, lab))
    for key, items in by.items():
        unit = _UNITS.get(key, "")
        labs = {lab for _, lab in items}
        if len(labs) > 1 and len({v for v, _ in items}) > 1 and not any(re.search(rf"{_NUM}\s*(?:-|–|to)\s*{_NUM}", lab)
                                                                           for lab in labs):
            flags.append({"kind": "live", "item": " and ".join(sorted(labs)),
                          "reason": f"the same signal ({_name(key)}) given twice with different values in one "
                                    "snapshot: at most one is real", "source": "the case's live data"})
        for v, lab in items:
            rng = ranges.get(key)
            if rng and not (rng[0] - 1e-9 <= v <= rng[1] + 1e-9):
                flags.append({"kind": "live", "item": lab, "source": src_rng,
                              "reason": f"{_fmt(v)} {unit} is outside what this signal can report "
                                        f"({_fmt(rng[0])} to {_fmt(rng[1])} {unit})"
                                        + ("; a wideband A/F sensor reports differently, check which sensor this is"
                                           if key.startswith("o2_") else "")})
                continue
            p = PHYSICAL.get(key)
            if p and ((p[0] is not None and v < p[0]) or (p[1] is not None and v > p[1])):
                bound = f"{_fmt(p[0])} to {_fmt(p[1])} {p[2]}" if p[1] is not None else f"at least {_fmt(p[0])} {p[2]}"
                flags.append({"kind": "live", "item": lab, "source": "generic physical limits (sanity_check.PHYSICAL)",
                              "reason": f"{_fmt(v)} {unit} is outside the physically plausible {bound} ({p[3]})"})
    one = {k: items[0][0] for k, items in by.items() if len({v for v, _ in items}) == 1}
    rpm = one.get("rpm")
    if rpm is not None and rpm == 0:
        moving = [(k, v) for k, v in (("speed", one.get("speed")),) if v is not None and v > 5]
        working = []
        if one.get("maf") is not None and one["maf"] > 1:
            working.append(f"mass air flow {_fmt(one['maf'])} g/s (air only flows while the engine turns)")
        if one.get("load_calc") is not None and one["load_calc"] > 0:
            working.append(f"calculated load {_fmt(one['load_calc'])} % (load is computed from a turning engine)")
        if one.get("ecu_volts") is not None and one["ecu_volts"] > 13.6:
            working.append(f"system voltage {_fmt(one['ecu_volts'])} V (above a resting battery: the alternator "
                           "only charges while the engine turns)")
        if any(one.get(k) not in (None, 0) for k in ("stft1", "stft2")):
            working.append("short-term fuel trim not 0 (the fuel trim only works while the engine runs)")
        if moving:
            flags.append({"kind": "live", "item": f"RPM 0 with speed {_fmt(moving[0][1])} km/h",
                          "reason": "the engine reads stopped while the car moves at speed: possible only "
                                    "coasting with the engine off; confirm both readings came from the same moment",
                          "source": "physics (readings taken together)"})
        if working:
            flags.append({"kind": "live", "item": "RPM 0 with " + "; ".join(w.split(" (")[0] for w in working),
                          "reason": "readings that need a running engine while engine speed reads 0: "
                                    + "; ".join(working) + ". At least one reading is wrong",
                          "source": "physics (readings taken together)"})
    if rpm is not None and rpm > 400 and one.get("maf") == 0:
        flags.append({"kind": "live", "item": f"RPM {_fmt(rpm)} with mass air flow 0 g/s",
                      "reason": "a turning engine pulls air, so one of these readings is wrong (a dead air flow "
                                "sensor or its circuit reads like this too)", "source": "physics (readings taken together)"})
    return flags


# ------------------------------------------------------------------------------------------- equipment
_WORD = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")
_VOCAB = {}


def _stop():
    try:
        from kb_retriever import STOP
        return set(STOP)
    except Exception:
        return set()


def find_ref_db(car_db_path, make=None):
    """kb/<make>.db for the make-wide vocabulary: $CYBERDECK_MAKE_DB, else a file of the car DB's name (or
    <make>.db) up to three folders above it, other than the car DB itself. None when there is none (laptop
    builds without the make DB just skip the equipment check)."""
    env = os.environ.get("CYBERDECK_MAKE_DB")
    if env and os.path.isfile(env):
        return env
    if not car_db_path:
        return None
    me = os.path.realpath(car_db_path)
    names = [os.path.basename(car_db_path)] + ([f"{make.lower()}.db"] if make else [])
    d = os.path.dirname(os.path.abspath(car_db_path))
    for _ in range(3):
        d = os.path.dirname(d)
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.path.realpath(p) != me:
                return p
    return None


def _vocab(ref_db):
    """Lower-case words and two-word phrases the make's own data uses to name components: labor operations,
    spec components, code names. Cached per file."""
    if ref_db in _VOCAB:
        return _VOCAB[ref_db]
    words = {}
    try:
        db = sqlite3.connect(f"file:{os.path.abspath(ref_db)}?mode=ro", uri=True)
        texts = []
        for table, col in (("labor", "operation"), ("spec", "component"), ("dtc", "description")):
            if _has(db, table):
                texts += [t for (t,) in db.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")]
        db.close()
    except sqlite3.Error:
        texts = []
    for t in texts:
        ws = [w.lower() for w in _WORD.findall(t)]
        for i, w in enumerate(ws):
            words[w] = words.get(w, 0) + 1
            if i + 1 < len(ws):
                bg = f"{w} {ws[i + 1]}"
                words[bg] = words.get(bg, 0) + 1
    _VOCAB[ref_db] = words
    return words


def _in_vocab(term, vocab):
    """How many times the make's data names term (a longer word it starts, e.g. turbo -> turbocharger, counts)."""
    if term in vocab:
        return vocab[term]
    if " " not in term and len(term) >= 5:
        return sum(n for w, n in vocab.items() if " " not in w and w.startswith(term) and len(w) - len(term) <= 7)
    return 0


def case_terms(text):
    """Words and two-word phrases of the case text, stop words out, in order of first use."""
    stop = _stop()
    out = []
    for sent in re.split(r"[.;:!?()\n]", text or ""):
        ws = [w.lower().strip("-") for w in _WORD.findall(sent)]
        for i, w in enumerate(ws):
            if w in stop or len(w) < 3:
                continue
            out.append(w)
            if i + 1 < len(ws) and ws[i + 1] not in stop and len(ws[i + 1]) >= 3:
                out.append(f"{w} {ws[i + 1]}")
    return list(dict.fromkeys(out))


def check_equipment(text, r, ref_db, min_ref=10, max_car=2):
    if not ref_db or not hasattr(r, "mentions"):
        return []
    vocab = _vocab(ref_db)
    if not vocab:
        return []
    flags, hit = [], []
    for term in case_terms(text):
        n_ref = _in_vocab(term, vocab)
        if n_ref < min_ref:
            continue
        if any(term in h or h in term for h in hit):        # 'timing chain' already says it; 'turbo' ~ 'turbocharger'
            continue
        n_car = r.mentions(term)
        if 0 <= n_car <= max_car:
            hit.append(term)
            flags.append({"kind": "equipment", "item": term, "source": f"this car's manual pages and specs ({n_car} "
                          f"pages name it); the make's DB names it as a part or system {n_ref} times",
                          "reason": "named in the case, but this car's manual " + (
                              "never mentions it" if n_car == 0 else f"names it on only {n_car} page(s)")
                          + ": check whether the car really has it (fitted from another car, wrong car picked, or a "
                            "mistake)"})
    return flags


# ------------------------------------------------------------------------------------------- together
def check(case, r, ref_db=None):
    flags = check_codes(case.get("dtcs_raw") or case.get("dtcs") or "", r)
    flags += check_live(case.get("telemetry") or "", r)
    text = "\n".join(case.get(k) or "" for k in ("symptoms", "history", "ruled_out", "suspect", "intake_note"))
    flags += check_equipment(text, r, ref_db)
    return flags


def block(flags):
    """The DATA CHECK block for the top of a prompt: real data problems under DATA CHECK, coverage notes (level
    'note') apart under a low-key heading. '' when there is neither."""
    real = [f for f in flags if f.get("level", "flag") == "flag"]
    notes = [f for f in flags if f.get("level") == "note"]
    lines = []
    if real:
        lines.append("DATA CHECK (worked out by the deck in Python before any model ran; each line is a data problem. "
                     "Don't build a diagnosis on a flagged item: say how to confirm or correct it first):")
        lines += [f"- {f['kind']}: {f['item']}: {f['reason']} [source: {f['source']}]" for f in real]
    if notes:
        lines.append("MANUAL COVERAGE NOTES (not data problems; the codes are valid):")
        lines += [f"- {f['item']}: {f['reason']}" for f in notes]
    return "\n".join(lines)
