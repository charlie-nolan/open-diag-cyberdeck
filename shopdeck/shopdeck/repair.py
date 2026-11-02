"""From a finding ("Replace the throttle position sensor.") to a job: the manual's procedure for it, the
torque values and must-be-new parts that procedure names, the CHARM book hours and part numbers, and a
quote. Python does every number; anything it can't tie to a source is marked as the mechanic's entry.
"""
import re
from dataclasses import dataclass, field, asdict

from .chart import REF_RE, clean
from .kb import STOP

TORQUE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*N[·.\s]?m\s*\(([^)]*)\)")
NEW_RE = re.compile(r"\b(new (?:one|ones|part|parts|\w+(?: \w+)?)|with new|do not reuse|don't reuse|cannot be reused|"
                    r"can not be reused|must not be reused|replace[^.]{0,40}\bnew\b|always replace)", re.I)
FASTENERS = r"gasket|seal|o-ring|o ring|packing|bolt|nut|washer|clip|clamp|pin|circlip|snap ring|spring|lock|cotter|plug"


# ---------------------------------------------------------------------------------------------- manual text
def refs_in(text):
    return [r.strip().rstrip(".").strip() for r in REF_RE.findall(text)]


def resolve_refs(kb, from_sections, text):
    """Sections named by '<Ref. to FU-33, Throttle Position Sensor.>' in text, via the kb's link table."""
    out = []
    for ref in refs_in(text):
        sec = None
        for s in from_sections:
            sec = kb.resolve_ref(s.id, ref)
            if sec:
                break
        if sec is None and "section_ref" in kb.tables and from_sections:
            r = kb.db.execute("SELECT to_id FROM section_ref r JOIN section s ON s.id=r.from_id "
                              "WHERE r.ref_text=? AND r.to_id IS NOT NULL AND s.doc_id="
                              "(SELECT doc_id FROM section WHERE id=?) LIMIT 1", (ref, from_sections[0].id)).fetchone()
            sec = kb.section(r[0]) if r else None
        if sec and all(sec.id != o.id for o in out):
            out.append(sec)
    return out


def procedure_candidates(kb, veh, name, limit=10):
    """Manual topics (removal / installation / inspection / replacement) that match a part or job name.
    The mechanic picks; the deck doesn't guess which one is meant."""
    hits = kb.search(veh, name, kind="fsm", limit=60)
    if len(hits) < 5:   # the manual may call it something else ("front exhaust pipe" for a manifold): any word
        hits += kb.search(veh, name, kind="fsm", limit=60, any_word=True)
    seen, out = set(), []
    for s in hits:
        if "DIAGNOSTIC" in s.path.split(">")[0].upper():
            continue         # charts and tables, not repair procedures
        key = " > ".join(x.strip() for x in s.path.split(">")[:2])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) == limit:
            break
    return out


def topic_name(section):
    return " > ".join(x.strip() for x in section.path.split(">")[:2])


def torque_lines(section):
    """Every torque value in a manual section, with the line it sits under (the manual's own wording)."""
    out, last = [], section.title
    for line in section.body.splitlines():
        t = clean(line.lstrip("#- "))
        if not t or t == "[fig]":
            continue
        if re.match(r"^\d+\)|^[A-Z0-9 .&/()-]{6,}$", t) and not TORQUE_RE.search(t):
            last = t[:140]            # the numbered step (or sub-heading) a torque value belongs to
        for m in TORQUE_RE.finditer(t):
            what = re.sub(r"^Tightening torque\s*", "", t[:m.start()], flags=re.I).strip(" :()")
            out.append((what or last, f"{m.group(1)} N·m ({m.group(2)})"))
    return out


def must_be_new(section):
    """Lines in a procedure that say a part must be new ('Replace gaskets with new ones.')."""
    out = []
    for line in section.body.splitlines():
        t = clean(line.lstrip("#- "))
        for sent in re.split(r"(?<=\.)\s+", t):
            if NEW_RE.search(sent) and (re.search(FASTENERS, sent, re.I) or re.search(r"new ones|new one", sent, re.I)):
                if sent not in out:
                    out.append(sent)
    return out


# ---------------------------------------------------------------------------------------------- CHARM labor
def words(text):
    return [w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in STOP and w not in
            {"the", "faulty", "parts", "part", "following", "items", "harness", "connector"}]


def match_labor(rows, name, limit=12):
    """CHARM rows (book hours or part numbers) whose operation names the job's words, best first.
    Score: words found in the last parts of the operation path count double."""
    ws = words(name)
    if not ws:
        return []
    scored = []
    for r in rows:
        segs = [x.strip().lower() for x in r["operation"].split(">")]
        tail, full = " ".join(segs[-3:]), " ".join(segs)
        hit_tail = sum(w in tail for w in ws)
        if hit_tail < min(2, len(ws)):
            continue
        sc = 2 * hit_tail + sum(w in full for w in ws) - len(r["operation"]) / 1000
        scored.append((sc, r))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _, r in scored:
        key = (r["operation"], r["hours"], r["part_number"])
        if key not in seen:
            seen.add(key)
            out.append(r)
        if len(out) == limit:
            break
    return out


def price_of(text):
    try:
        return float(str(text).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------- jobs and quotes
@dataclass
class LaborLine:
    what: str
    hours: float
    source: str          # "CHARM book time: <operation>" or "mechanic"


@dataclass
class PartLine:
    what: str
    part_number: str = ""
    qty: float = 1
    unit_price: float = None
    source: str = ""     # "CHARM list price (old)" / "mechanic" / "manual: must be new"


@dataclass
class Job:
    title: str
    reason: str                              # "FSM P0304 chart, step 23" / "mechanic's call: ..."
    procedures: list = field(default_factory=list)   # section ids (manual topics)
    labor: list = field(default_factory=list)        # LaborLine
    parts: list = field(default_factory=list)        # PartLine

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        j = cls(d["title"], d["reason"], d.get("procedures", []))
        j.labor = [LaborLine(**x) for x in d.get("labor", [])]
        j.parts = [PartLine(**x) for x in d.get("parts", [])]
        return j


@dataclass
class Quote:
    lines: list          # (kind, text, amount)
    labor: float
    parts: float
    tax: float
    supplies: float
    total: float
    missing: list        # parts with no price: the quote is incomplete until they're filled in


def quote(jobs, rate, tax_rate=0.0, supplies_rate=0.0):
    lines, labor, parts, missing = [], 0.0, 0.0, []
    for j in jobs:
        lines.append(("job", j.title, None))
        for l in j.labor:
            amt = round(l.hours * rate, 2)
            labor += amt
            lines.append(("labor", f"Labor: {l.what}, {l.hours:g} h × ${rate:g}/h", amt))
        for p in j.parts:
            if p.unit_price is None:
                missing.append(f"{j.title}: {p.what} {p.part_number}".strip())
                lines.append(("part", f"{p.what} {p.part_number} × {p.qty:g}  (price needed)", None))
                continue
            amt = round(p.unit_price * p.qty, 2)
            parts += amt
            lines.append(("part", f"{p.what} {p.part_number} × {p.qty:g} @ ${p.unit_price:,.2f}", amt))
    tax = round(parts * tax_rate, 2)
    supplies = round(labor * supplies_rate, 2)
    total = round(labor + parts + tax + supplies, 2)
    return Quote(lines, round(labor, 2), round(parts, 2), tax, supplies, total, missing)
