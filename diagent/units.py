"""The user's replies and the manual's checks, worked out in Python so the model never does arithmetic.

parse_reply: 'done' / "can't" / 'skip' / 'back' / 'stop', yes/no, or a number with a unit (unit-checked
against what was asked; 'OL' on a resistance range = open circuit).
judge: the manual's check ('Is the voltage more than 10 V?', 'Is the resistance between 1 and 4 kΩ?')
applied to a reading. None when the check isn't a single plain comparison: then the user answers it.
"""
import math
import re

# unit -> (dimension, factor to the base unit)
UNITS = {
    "v": ("V", 1), "volt": ("V", 1), "volts": ("V", 1), "mv": ("V", 1e-3),
    "ω": ("Ω", 1), "ohm": ("Ω", 1), "ohms": ("Ω", 1), "kω": ("Ω", 1e3), "kohm": ("Ω", 1e3), "kohms": ("Ω", 1e3),
    "k": ("Ω", 1e3), "mω": ("Ω", 1e6), "mohm": ("Ω", 1e6), "megohm": ("Ω", 1e6), "megohms": ("Ω", 1e6),
    "a": ("A", 1), "amp": ("A", 1), "amps": ("A", 1), "ma": ("A", 1e-3),
    "kpa": ("kPa", 1), "psi": ("kPa", 6.894757), "mmhg": ("kPa", 0.1333224), "inhg": ("kPa", 3.386389),
    "bar": ("kPa", 100),
    "%": ("%", 1), "percent": ("%", 1), "rpm": ("rpm", 1),
    "°c": ("°C", 1), "c": ("°C", 1), "degc": ("°C", 1),
    "hz": ("Hz", 1), "khz": ("Hz", 1e3), "ms": ("s", 1e-3), "s": ("s", 1), "sec": ("s", 1),
    "mm": ("mm", 1), "in": ("mm", 25.4), "g/s": ("g/s", 1),
    "n·m": ("N·m", 1), "nm": ("N·m", 1), "ft-lb": ("N·m", 1.355818), "ft-lbs": ("N·m", 1.355818),
}
UNIT_ALT = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
NUM = r"[+-]?\d+(?:[.,]\d+)?"
VALUE_RE = re.compile(rf"({NUM})\s*({UNIT_ALT})?(?![a-z])", re.I)
COMMANDS = {"done": "done", "ok": "done", "did it": "done", "can't": "cant", "cant": "cant", "cannot": "cant",
            "can not": "cant", "unable": "cant", "skip": "skip", "back": "back", "go back": "back",
            "stop": "stop", "quit": "stop", "pause": "stop"}
YES = re.compile(r"^(y|yes|yep|yeah|it does|it is|there is|true|affirmative)\b", re.I)
NO = re.compile(r"^(n|no|nope|it doesn't|it does not|it isn't|it is not|there isn't|there is no|none|false)\b", re.I)
OPEN = re.compile(r"^(ol|o\.l\.|open|infinite|inf|over ?limit|no continuity)\b", re.I)


def norm_unit(u):
    """(dimension, factor) for a unit as typed or printed; 'MΩ' and 'mΩ' both mean megohm (the manuals never
    ask for milliohms)."""
    return UNITS.get(re.sub(r"\s+", "", u or "").lower())


def _f(x):
    return float(x.replace(",", "."))


def fmt(value, dim):
    if value == math.inf:
        return "open (OL)"
    for big, pre in ((1e6, "M"), (1e3, "k")):
        if dim == "Ω" and abs(value) >= big:
            return f"{value / big:g} {pre}Ω"
    return f"{value:g} {dim}"


def parse_reply(text, expect=None, unit=None):
    """{'kind': 'command'|'yesno'|'number'|'choice'|'text', ...} or {'kind': 'error', 'why': ...}.
    expect: 'number' (with unit), 'yesno', 'choice', 'text', 'done' or None."""
    t = (text or "").strip()
    low = t.lower().rstrip(".!")
    if not t:
        return {"kind": "error", "why": "empty reply"}
    if low in COMMANDS:
        return {"kind": "command", "command": COMMANDS[low], "raw": t}
    for k, v in COMMANDS.items():
        if v in ("cant", "back", "stop") and low.startswith(k + " "):   # "can't reach the connector"
            return {"kind": "command", "command": v, "raw": t, "note": t[len(k):].strip()}
    if expect == "yesno":
        if YES.match(low):
            return {"kind": "yesno", "answer": "Yes", "raw": t}
        if NO.match(low):
            return {"kind": "yesno", "answer": "No", "raw": t}
        return {"kind": "error", "why": "expected yes or no"}
    if expect == "number":
        want = norm_unit(unit) if unit else None
        if OPEN.match(low):
            if want and want[0] != "Ω":
                return {"kind": "error", "why": f"'open' is a resistance reading; {unit} was asked for"}
            return {"kind": "number", "value": math.inf, "unit": "Ω", "raw": t, "assumed_unit": False}
        m = VALUE_RE.search(t)
        if not m:
            return {"kind": "error", "why": f"no number found; expected a value in {unit or 'a unit'}"}
        val, u = _f(m.group(1)), m.group(2)
        if re.search(rf"{NUM}\s*(°\s*f|deg ?f|f)\b", low):
            return {"kind": "number", "value": (val - 32) * 5 / 9, "unit": "°C", "raw": t, "assumed_unit": False}
        got = norm_unit(u) if u else None
        if got is None:
            if not want:
                return {"kind": "error", "why": "give the unit too (V, Ω, A, kPa ...)"}
            return {"kind": "number", "value": val * want[1], "unit": want[0], "raw": t, "assumed_unit": True}
        if want and got[0] != want[0]:
            return {"kind": "error", "why": f"that's {got[0]}, but the step asks for {want[0]}"}
        return {"kind": "number", "value": val * got[1], "unit": got[0], "raw": t, "assumed_unit": False}
    if expect == "done":
        return {"kind": "text", "text": t, "raw": t}
    if YES.match(low) and expect in (None, "text"):
        return {"kind": "yesno", "answer": "Yes", "raw": t}
    if NO.match(low) and expect in (None, "text"):
        return {"kind": "yesno", "answer": "No", "raw": t}
    return {"kind": "text", "text": t, "raw": t}


# ---------------------------------------------------------------------------- the manual's checks
_Q = rf"({NUM})\s*({UNIT_ALT})"
_RANGE = re.compile(rf"(?:between\s+)?({NUM})\s*({UNIT_ALT})?\s*(?:and|—|–|-|to)\s*({NUM})\s*({UNIT_ALT})", re.I)
_CMP = [(re.compile(rf"(?:more|greater|higher) than\s+{_Q}", re.I), ">"),
        (re.compile(rf"(?:less|lower|smaller) than\s+{_Q}", re.I), "<"),
        (re.compile(rf"{_Q}\s+or (?:more|higher|greater|above)", re.I), ">="),
        (re.compile(rf"{_Q}\s+or (?:less|lower|below)", re.I), "<=")]
# checks that need more than one reading or a change over time: the user answers those
_NOT_SIMPLE = re.compile(r"\bchange|\bshak|\bwhile\b|\bfluctuat|\bwaveform|\bboth\b|\bor more than|\band less than", re.I)


def _unit(u):
    u = re.sub(r"\s+", "", u or "")
    return norm_unit(u)


def comparison(check):
    """(op, lo, hi, dim, text) for a plain one-value check, else None. Parenthesised conversions are ignored."""
    c = re.sub(r"\([^)]*\)", " ", check or "")
    c = re.sub(r"(\d)\s+([kM])\s*Ω", r"\1 \2Ω", c)     # '1 M Ω' -> '1 MΩ'
    if _NOT_SIMPLE.search(c):
        return None
    m = _RANGE.search(c)
    if m and m.group(4):
        hi_u = _unit(m.group(4))
        lo_u = _unit(m.group(2)) if m.group(2) else hi_u
        if hi_u and lo_u and hi_u[0] == lo_u[0]:
            return ("range", _f(m.group(1)) * lo_u[1], _f(m.group(3)) * hi_u[1], hi_u[0], m.group(0).strip())
    hits = []
    for rx, op in _CMP:
        for m in rx.finditer(c):
            u = _unit(m.group(2))
            if u:
                hits.append((op, _f(m.group(1)) * u[1], None, u[0], m.group(0).strip()))
    return hits[0] if len(hits) == 1 else None


def expected_unit(check):
    cmp_ = comparison(check)
    return cmp_[3] if cmp_ else None


def judge(check, value, dim):
    """('Yes'|'No', explanation) or None when Python can't decide this check from one reading."""
    cmp_ = comparison(check)
    if not cmp_ or cmp_[3] != dim:
        return None
    op, lo, hi, d, text = cmp_
    ok = {"range": lambda: lo <= value <= hi, ">": lambda: value > lo, "<": lambda: value < lo,
          ">=": lambda: value >= lo, "<=": lambda: value <= lo}[op]()
    rel = (f"between {fmt(lo, d)} and {fmt(hi, d)}" if op == "range" else
           {">": "more than {}", "<": "less than {}", ">=": "{} or more", "<=": "{} or less"}[op].format(fmt(lo, d)))
    return ("Yes" if ok else "No", f"{fmt(value, d)} is {'' if ok else 'not '}{rel} (manual: \"{text}\")")
