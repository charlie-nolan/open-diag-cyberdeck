"""Faithfulness check for model answers (stdlib only, runs on the deck).

Rule 4 (accuracy, cite sources): every spec, torque, part number, price and labor time in an answer must appear in a
page the model was given, and in the page it cites when it cites one. This marks, never deletes or rewrites.

    probs, found = check(answer, prompt)

`prompt` is the text the model saw: the case summary plus the manual/card/owner blocks, each opened by a tag line
such as "[FSM: Engine > Fuel Injector, p.212]", "[CHARM: ...]", "[MANUAL DIAGNOSTIC PROCEDURE: ...", "[DTC] ...".
Text before the first tag is the case (readings the deck took), which backs a number but is not a manual page.

Wording and unit rules only: no parts, causes, cars or codes are listed here.
"""
import os
import re

# unit -> (group, factor to the group's base unit). Groups keep "25 N·m" from matching "25 mm" on the same page.
# (?i:...) where the manuals vary the case ('29 ft. lbs.', '22 Ft. LBS.'); single letters (V, A, L, h) stay exact.
_UNITS = [
    (r"(?i:N·m|N\.m|Nm|N-m)", "torque", 1.0), (r"(?i:kgf-?m|kg-?m)", "torque", 9.80665),
    (r"(?i:ft\.?[- ]?lbs?\.?|ft\.?[- ]?lbf|lbs?\.?[- ]?ft\.?|foot[- ]pounds?)", "torque", 1.35582),
    (r"(?i:in\.?[- ]?lbs?\.?|lbs?\.?[- ]?in\.?|inch[- ]pounds?)", "torque", 0.112985),
    (r"(?i:kPa)", "pressure", 1.0), (r"(?i:MPa)", "pressure", 1000.0), (r"(?i:psi)", "pressure", 6.89476),
    (r"bar", "pressure", 100.0), (r"(?i:kgf/cm2|kgf/cm²|kg/cm2|kg/cm²)", "pressure", 98.0665),
    (r"(?i:in\.? ?Hg)", "vacuum", 3.38639), (r"(?i:mm ?Hg)", "vacuum", 0.133322),
    (r"mV", "volt", 0.001), (r"kV", "volt", 1000.0), (r"V|(?i:volts?)", "volt", 1.0),
    (r"mA", "amp", 0.001), (r"A|(?i:amps?)", "amp", 1.0),
    (r"MΩ|M ?(?i:ohms?)", "ohm", 1e6), (r"kΩ|(?i:k ?ohms?)", "ohm", 1000.0), (r"Ω|(?i:ohms?)", "ohm", 1.0),
    (r"(?i:mm)", "length", 1.0), (r"cm", "length", 10.0), (r"in\.|(?i:inch(?:es)?)", "length", 25.4),
    (r"°\s?[Cc]|(?i:deg ?c)", "degC", 1.0), (r"°\s?[Ff]|(?i:deg ?f)", "degF", 1.0),
    (r"(?i:rpm|r/min)", "rpm", 1.0), (r"Hz", "hz", 1.0), (r"kHz", "hz", 1000.0),
    (r"ms", "time", 0.001), (r"g/s", "flow", 1.0), (r"%", "pct", 1.0),
    (r"(?i:liters?|litres?)|L", "volume", 1000.0), (r"ml|mL|cc", "volume", 1.0),
    (r"(?i:US ?qts?|qts?|quarts?)", "volume", 946.353), (r"(?i:US ?gal|gal(?:lons?)?)", "volume", 3785.41),
    (r"h|hrs?", "hours", 1.0),
]
UNIT_RE = "|".join(f"(?:{u})" for u, _, _ in sorted(_UNITS, key=lambda x: -len(x[0])))
_UNIT_ONE = [(re.compile(rf"^(?:{u})$"), g, f) for u, g, f in _UNITS]
NUM = r"[+-]?\d+(?:[.,]\d+)?"
# a range (284-314, 0.8 — 1.2) or a nominal with tolerance (10±2: the nominal is what an answer quotes)
RANGE = rf"{NUM}(?:\s*(?:-|–|—|~)\s*{NUM}|\s*±\s*{NUM})?"
# a value with its unit: 25 N·m, 284-314 kPa, 0.8 - 1.2 kΩ, 14.2V, 1.3 h
VALUE_RE = re.compile(rf"(?<![\w.$]){RANGE}\s*(?:{UNIT_RE})(?![A-Za-z0-9])")
PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
# Part numbers in any maker's style: 6+ characters of letters/digits/dashes with 3+ digits ('13028AA21C',
# 'F7AZ-9H307-AB', '90919-01253', '19115080'). Codes and years are not part numbers.
PN_RE = re.compile(r"(?<![\w-])(?=[A-Z0-9-]*\d[A-Z0-9-]*\d[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{4,}[A-Z0-9](?![\w-])")
DTC_RE = re.compile(r"^[PBCU][0-3][0-9A-F]{3}$")
TAG_RE = re.compile(r"(?m)^\s*\[(?!\d)([^\]\n]{2,200})\]?.*$")
PAGE_RE = re.compile(r"\bp(?:age|\.)\s*(\d{1,4})\b", re.I)
CITE_RE = re.compile(r"\[(?:FSM|CHARM|DTC|MANUAL|CARD)[^\]\n]*\]|\bp(?:age|\.)\s*\d{1,4}\b", re.I)


def _f(s):
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _unit(u):
    u = u.strip()
    for rx, g, f in _UNIT_ONE:
        if rx.match(u):
            return g, f
    return None, 1.0


def values(text):
    """[(group, factor, ((number, decimals)...), matched text)] for each value with a unit in text."""
    out = []
    for m in VALUE_RE.finditer(text or ""):
        s = m.group(0)
        um = re.search(rf"(?:{UNIT_RE})$", s)
        g, f = _unit(um.group(0)) if um else (None, 1.0)
        body = (s[:um.start()] if um else s).split("±")[0]
        nums = [(abs(_f(x)), len(x.replace(",", ".").partition(".")[2]))
                for x in re.findall(r"\d+(?:[.,]\d+)?", body) if _f(x) is not None]
        if g and nums:
            out.append((g, f, tuple(nums), s.strip()))
    return out


def _close(a, b):
    return abs(a - b) <= max(0.011, 0.01 * max(abs(a), abs(b)))


def _same(n, dec, f, page_base):
    """The answer's number n (written with dec decimals, unit factor f) is the page's value page_base (in the base
    unit), as printed or converted and rounded to the answer's precision: 18 ft-lb for a page's 25 N·m (18.4 ft-lb)."""
    conv = page_base / f
    return _close(n, conv) or round(conv, dec) == round(n, dec)


def _bare_numbers(text):
    """Numbers in table cells and ranges, for tables that print the unit once in the header."""
    keep = set()
    for cell in re.findall(r"\|\s*([^|\n]{1,40}?)\s*(?=\|)", text or ""):
        for x in re.findall(NUM, cell):
            v = _f(x)
            if v is not None:
                keep.add(abs(v))
    return keep


def pages(prompt):
    """[(label, text)]: the case first, then one entry per tagged block of the prompt."""
    prompt = prompt or ""
    marks = [(m.start(), m.group(1).strip()) for m in TAG_RE.finditer(prompt)]
    out = [("case", prompt[:marks[0][0]] if marks else prompt)]
    for i, (pos, label) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(prompt)
        out.append((label, prompt[pos:end]))
    return out


class _Page:
    def __init__(self, label, text):
        self.label, self.text = label, text
        self.vals = values(text)
        self.bare = _bare_numbers(text)
        self.cell_units = {}        # a table that prints its unit once, in a header cell: 'Ohms', 'Torque (N·m)'
        for cell in re.findall(r"\|\s*([^|\n]{1,40}?)\s*(?=\|)", text):
            um = re.search(rf"(?:^|[\s(])((?:{UNIT_RE}))\)?$", cell, re.I)
            if um and not re.search(r"\d", cell):
                g, f = _unit(um.group(1))
                if g:
                    self.cell_units.setdefault(g, f)
        self.flat = re.sub(r"[\s\-]", "", text.upper())
        self.prices = {_f(p.replace("$", "").replace(",", "").strip()) for p in PRICE_RE.findall(text)}
        self.page_nos = set(PAGE_RE.findall(label)) | set(PAGE_RE.findall(text[:300]))

    def has_value(self, group, f, nums):
        """Every number of the answer's value (both ends of a range) is a value of the same kind on this page."""
        base = [pf * n for g, pf, ns, _ in self.vals if g == group for n, _ in ns]
        return bool(base) and all(any(_same(n, d, f, b) for b in base) for n, d in nums)

    def has_cells(self, group, f, nums):
        """The numbers are table cells under a header that names a unit of the same kind (scaled to it)."""
        hf = self.cell_units.get(group)
        return hf is not None and all(any(_same(n, d, f, v * hf) for v in self.bare) for n, d in nums)


_SENT_END = re.compile(r"\n|(?<=[.!?])\s+(?=[A-Z(\[])")


def _not_pn(s):
    """Shapes that look like a part number but name something else: a code range (P0300-P0304), a step or chart
    range (10G6-10G9, 2000-2999: two pieces of one shape with a shared start), a connector and pin (B134-24)."""
    bits = s.split("-")
    if len(bits) > 1 and all(DTC_RE.match(b) for b in bits):
        return True
    if len(bits) == 2:
        a, b = bits
        shape = lambda x: re.sub(r"\d", "9", re.sub(r"[A-Z]", "A", x))  # noqa: E731
        if shape(a) == shape(b) and os.path.commonprefix([a, b]) and len(os.path.commonprefix([a, b])) >= len(a) // 2:
            return True
        if a.isdigit() and b.isdigit() and len(a) == len(b) and int(a) < int(b):    # a number range
            return True
        if re.fullmatch(r"[A-Z]{1,2}\d{1,3}", a) and re.fullmatch(r"\d{1,3}", b):
            return True
    return False


def _sentence_at(answer, pos):
    """The sentence of the answer around pos (a cite applies to the sentence it sits in)."""
    a = max([m.end() for m in _SENT_END.finditer(answer, 0, pos)] or [0])
    m = _SENT_END.search(answer, pos)
    return answer[a: m.start() if m else len(answer)]


def _cited(line, pgs):
    """The pages a line of the answer cites (by page number or by the tag's words), or None for no cite."""
    cites = CITE_RE.findall(line)
    if not cites:
        return None
    hit = []
    for c in cites:
        nos = PAGE_RE.findall(c)
        words = set(re.findall(r"[a-z]{4,}", c.lower())) - {"page", "charm", "manual", "card"}
        for p in pgs[1:]:
            if (nos and set(nos) & p.page_nos) or (words and len(words & set(re.findall(r"[a-z]{4,}", p.label.lower())))
                                                   >= max(1, len(words) // 2)):
                hit.append(p)
    return hit


def check(answer, prompt):
    """(problems, found): problems are short lines for items the answer states that no page holds (or that the
    page it cites doesn't hold); found maps each checked item to the page label that backs it."""
    if not answer:
        return [], {}
    pgs = [_Page(lbl, txt) for lbl, txt in pages(prompt)]
    probs, found = [], {}

    def place(kind, item, pos, test):
        line = _sentence_at(answer, pos)
        cited = _cited(line, pgs)
        where = [p for p in pgs if test(p)]
        if cited and not any(p in where for p in cited):
            if where:
                probs.append(f"{kind} '{item}' is not on the page it cites; it is on: {where[0].label[:70]}")
            else:
                probs.append(f"{kind} '{item}' is not on the page it cites or any page it was given")
        elif not where:
            probs.append(f"{kind} '{item}' is not in any page it was given")
        else:
            found[item] = where[0].label
    for m in VALUE_RE.finditer(answer):
        v = values(m.group(0))
        if not v:
            continue
        g, f, nums, s = v[0]
        kind = {"torque": "torque", "hours": "labor time"}.get(g, "spec")
        place(kind, s, m.start(), lambda p, g=g, f=f, nums=nums: p.has_value(g, f, nums) or p.has_cells(g, f, nums))
    for m in PRICE_RE.finditer(answer):
        v = _f(m.group(0).replace("$", "").replace(",", "").strip())
        place("price", m.group(0).strip(), m.start(), lambda p, v=v: any(x is not None and _close(v, x) for x in p.prices))
    for m in PN_RE.finditer(answer):
        s = m.group(0)
        if DTC_RE.match(s) or re.fullmatch(r"(19|20)\d\d", s) or (s.isdigit() and len(s) < 6) or _not_pn(s):
            continue
        if re.match(rf"\s*(?:{UNIT_RE}|miles|mi|km)(?![A-Za-z])", answer[m.end():m.end() + 7]):   # 123456 miles
            continue
        key = re.sub(r"[\s\-]", "", s)
        place("part number", s, m.start(), lambda p, key=key: key in p.flat)
    return list(dict.fromkeys(probs)), found


def report(answer, prompt):
    """One block for the answer box: '' when nothing fact-shaped was stated, else the count and each problem."""
    probs, found = check(answer, prompt)
    if not probs and not found:
        return ""
    if not probs:
        return f"[source check] {len(found)} spec/part/price item(s), all on the pages given."
    return ("[source check] not found in the pages given (check the manual before using these):\n"
            + "\n".join("  • " + p for p in probs))
