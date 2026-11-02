"""A factory diagnostic chart (the 'No. | Step | Check | Yes | No' table) as something you can walk.

The deck never guesses an answer. A step is answered in one of three ways, and the record says which:
  scan      the question only asks which trouble codes are stored, so the scan answers it
  measured  the mechanic typed a reading and the chart's own limit decided Yes or No ("13.2 V > 10 V")
  mechanic  the mechanic pressed Yes or No
Charts that didn't convert cleanly (missing steps, dead 'go to step' links, cut-off cells) are flagged as
damaged, and the app then shows the manual text and page instead of walking a broken tree.
"""
import re
from dataclasses import dataclass, field

CODE_RE = re.compile(r"\b[PBCU][0-9A-F]\d{3}\b")
GOTO_RE = re.compile(r"Go to step\s*(\d+)", re.I)
SEP_RE = re.compile(r"^\|?[\s\-:|]+\|?$")
REF_RE = re.compile(r"<Ref\.?\s*(?:to\s*)?([^>]*)>")


def clean(text):
    """Manual cell text for reading: no html, no '\\Omega', no doubled spaces. Refs are kept."""
    text = re.sub(r"<(?!Ref)[^>]+>", " ", text)
    text = text.replace("\\Omega", "Ω").replace("\\(", "(").replace("\\)", ")").replace("\\*", "*")
    return re.sub(r"[ \t]+", " ", text).strip(" ;")


def lines_of(cell):
    """A cell's '; '-joined lines as a list (the converter joined the manual's lines with '; ')."""
    return [x.strip() for x in clean(cell).split(";") if x.strip()]


# ---------------------------------------------------------------------------------------------- outcomes
@dataclass
class Outcome:
    kind: str                 # goto | other_code | intermittent | action | end
    text: str                 # the manual's words
    step: int = None          # goto target (also set on an action that continues to a step)
    codes: list = field(default_factory=list)

    @property
    def is_finding(self):
        return self.kind == "action"


INTERMITTENT_RE = re.compile(r"returned to a normal condition|temporary poor contact|intermittent", re.I)
OTHER_CODE_RE = re.compile(r"relevant DTC|appropriate DTC|according to DTC|Check DTC using|Go to DTC|"
                           r"perform the diagnosis .*DTC|List of Diagnostic Trouble Code", re.I)


def parse_outcome(cell):
    text = clean(cell)
    if not text:
        return Outcome("end", "")
    goto = GOTO_RE.search(text)
    rest = GOTO_RE.sub("", text).strip(" .;")
    if goto and len(rest) < 4:
        return Outcome("goto", text, int(goto.group(1)))
    if OTHER_CODE_RE.search(text) and not re.match(r"\s*(Repair|Replace)", text, re.I):
        return Outcome("other_code", text, int(goto.group(1)) if goto else None, CODE_RE.findall(text))
    if INTERMITTENT_RE.search(text) and not re.match(r"\s*(Repair|Replace)", text, re.I):
        return Outcome("intermittent", text, int(goto.group(1)) if goto else None)
    return Outcome("action", text, int(goto.group(1)) if goto else None)


# ---------------------------------------------------------------------------------------------- limits
UNIT = r"(k\s*Ω|M\s*Ω|Ω|mV|V|kPa|mmHg|°C|°F|A|%|mm|ms|rpm|Hz|g/s|deg|°)"
NUM = r"(-?\d+(?:\.\d+)?)"
SCALE = {"kΩ": 1e3, "MΩ": 1e6, "mV": 1e-3}


@dataclass
class Limit:
    op: str          # '>', '<', '>=', '<=', 'between'
    lo: float
    hi: float = None
    unit: str = ""

    def judge(self, value):
        if self.op == ">":
            return value > self.lo
        if self.op == ">=":
            return value >= self.lo
        if self.op == "<":
            return value < self.lo
        if self.op == "<=":
            return value <= self.lo
        return self.lo <= value <= self.hi

    def describe(self):
        u = f" {self.unit}" if self.unit else ""
        if self.op == "between":
            return f"between {self.lo:g} and {self.hi:g}{u} (inclusive)"
        return {">": "more than", ">=": "at least", "<": "less than", "<=": "at most"}[self.op] + f" {self.lo:g}{u}"


def parse_limit(question):
    """The pass/fail limit in a check question, when it has exactly one: 'Is the voltage more than 10 V?'.
    Questions about a change ('Does the voltage change ... by shaking') or a moment are left to the mechanic."""
    q = clean(question).replace("k Ω", "kΩ").replace("M Ω", "MΩ")
    if re.search(r"\bchange|for a moment|shaking|each|every", q, re.I):
        return None
    m = re.search(r"between\s+" + NUM + r"\s*" + UNIT + r"?\s*(?:and|—|-|to)\s*" + NUM + r"\s*" + UNIT, q)
    if m:
        unit = (m.group(4) or m.group(2) or "").replace(" ", "")
        return Limit("between", float(m.group(1)), float(m.group(3)), unit)
    # every bound in the question, ignoring the converted units in brackets: (eq, word, number, unit)
    found = re.findall(r"(equal to (?:or )?)?\b(more|greater|higher|less|lower) than\s+" + NUM + r"\s*" + UNIT + "?",
                       re.sub(r"\([^)]*\)", "", q))
    if len(found) == 2:   # "equal to or more than 0.85 and equal to less than 1.15": a range
        lows = [f for f in found if f[1] in ("more", "greater", "higher")]
        highs = [f for f in found if f[1] in ("less", "lower")]
        if len(lows) == 1 and len(highs) == 1 and float(lows[0][2]) < float(highs[0][2]):
            return Limit("between", float(lows[0][2]), float(highs[0][2]), (lows[0][3] or highs[0][3]).replace(" ", ""))
        return None
    if len(found) == 1:
        eq, word, num, unit = found[0]
        up = word in ("more", "greater", "higher")
        return Limit((">=" if up else "<=") if eq else (">" if up else "<"), float(num), unit=unit.replace(" ", ""))
    return None


def parse_reading(text, limit):
    """Typed readings ('13.2', '0.4k', '12 13.1 12.8') as numbers in the limit's unit."""
    vals = []
    for num, suffix in re.findall(r"(-?\d+(?:\.\d+)?)\s*([kKmM]?)", text):
        v = float(num)
        if suffix in ("k", "K") and limit.unit == "Ω":
            v *= 1e3
        if suffix in ("k", "K") and limit.unit == "MΩ":
            v /= 1e3
        if suffix == "M" and limit.unit == "kΩ":
            v *= 1e3
        vals.append(v)
    return vals


# ---------------------------------------------------------------------------------------------- steps
@dataclass
class Step:
    n: int
    title: str
    instructions: list
    question: str
    yes: Outcome
    no: Outcome
    raw: list
    broken: bool = False      # didn't convert: shown as the manual's text, the mechanic reads the page and jumps

    @property
    def limit(self):
        return parse_limit(self.question)

    def scan_answer(self, codes, own=()):
        """'Yes'/'No' when the question only asks which codes the scan shows, else None.
        `own`: the code(s) this chart is being walked for, which are never 'other' codes."""
        q = self.question
        have = set(codes)
        if not re.search(r"\bindicate\b|\bdisplay", q, re.I):
            return None
        if re.search(r"only one DTC", q, re.I):
            return "Yes" if len(have) == 1 else "No"
        if re.search(r"any other DTC|other DTC on display", q, re.I):
            mine = set(CODE_RE.findall(self.yes.text + " " + self.no.text))   # the NOTE: 'not necessary to inspect ...'
            others = have - mine - set(own) if own else (have - mine if mine else (have if len(have) > 1 else set()))
            return "Yes" if others else "No"
        listed = CODE_RE.findall(q)
        if listed and re.search(r"indicate DTC", q, re.I):
            if " or " in q:
                return "Yes" if any(c in have for c in listed) else "No"
            return "Yes" if all(c in have for c in listed) else "No"
        return None


@dataclass
class Chart:
    code: str
    title: str
    sections: list            # kb.Section objects, in page order
    head: list                # detecting condition, symptoms, cautions (lines)
    steps: dict
    problems: list            # why the chart can't be trusted as a tree, empty when it can
    via: str = None           # the code whose page said "refer to DTC <this one>"
    variant: str = ""         # set when the manual has more than one procedure under this code
    alternates: list = field(default_factory=list)   # the other procedures for the same code

    @property
    def ok(self):
        return bool(self.steps) and not self.problems

    @property
    def source(self):
        pages = sorted({s.page for s in self.sections if s.page})
        doc = self.sections[0].source.split(", p.")[0] if self.sections else ""
        if not pages:
            return doc
        return f"{doc}, p.{pages[0]}" + (f"-{pages[-1]}" if len(pages) > 1 else "")

    def first(self):
        return min(self.steps) if self.steps else None


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


DTC_HEAD_RE = re.compile(r"^(?:#+\s*)?(?:[A-Z]{1,2}\s*:\s*)?DTC\s*([PBCU][0-9A-F]\d{3})\b")


def own_lines(sections, code):
    """The lines that belong to this code's chart. A converted 'section' can run on into the next code's chart
    (P0106's last page also holds P0107 and P0108), so lines are kept only under a heading for this code."""
    current = code
    for sec in sections:
        body = sec.body.splitlines()
        continued = any(l.startswith("(continued") for l in body[:3])
        if not continued:   # a new manual section: its own heading says whose chart it is
            m = re.search(r"DTC\s*([PBCU][0-9A-F]\d{3})", sec.title or "")
            current = m.group(1) if m else code
        for i, line in enumerate(body):
            h = DTC_HEAD_RE.match(line.strip())
            if h and not (continued and i == 0):   # a continued part repeats the section title on top: not a heading
                current = h.group(1)
            if current == code:
                yield line


def parse(sections, code=None):
    """(head lines, steps, problems) from a procedure's sections."""
    head, rows, problems, cut = [], {}, [], {}
    last = None
    lines = own_lines(sections, code) if code else (l for sec in sections for l in sec.body.splitlines())
    for line in lines:
        s = line.strip()
        if not s or SEP_RE.match(s) or s.startswith("(continued") or s == "[fig]":
            continue
        if s.startswith("|"):
            c = _cells(s)
            if c and c[0] in ("No.", "") and len(c) >= 2 and c[1] in ("Step", "Step Check Yes No"):
                continue          # repeated table header
            if len(c) >= 5 and c[0].isdigit():
                n = int(c[0])
                if n in rows:
                    problems.append(f"step {n} appears twice")
                rows[n] = [c[1], c[2], c[3], " | ".join(x for x in c[4:] if x)]
                last = n
            elif c and c[0].isdigit() and int(c[0]) not in rows:
                n = int(c[0])
                cut[n] = [x for x in c[1:] if x]
                last = ("cut", n)
            elif isinstance(last, tuple) and c and (c[0] == "" or re.match(r"\d+\)$", c[0])):
                cut[last[1]].append(" ".join(x for x in c if x))
            elif c and c[0] == "" and last is not None and len(c) >= 5:
                for i, extra in enumerate(c[1:5]):     # continuation row: the manual cell went on
                    if extra:
                        rows[last][i] = (rows[last][i] + " " + extra).strip()
            elif c and c[0].isdigit():
                problems.append(f"step {c[0]} appears twice")
                last = None
            continue
        if s.startswith("### ") and "DTC" in s and ("—" in s or "-" in s):
            continue
        last = None
        head.append(clean(s.lstrip("#- ")))
    if not rows:
        lines = list(own_lines(sections, code) if code else (l for sec in sections for l in sec.body.splitlines()))
        old = parse_labelled(lines)
        if old:
            steps, oproblems = old
            note = [ORDER_NOTE] if ORDER in oproblems else []
            return note + head, steps, [p for p in oproblems if p != ORDER] + check(steps)
    steps = {}
    for n, (title_cell, question, yes, no) in sorted(rows.items()):
        parts = lines_of(title_cell)
        steps[n] = Step(n, parts[0] if parts else "", parts[1:], clean(question), parse_outcome(yes),
                        parse_outcome(no), [title_cell, question, yes, no])
    for n, texts in cut.items():
        if n not in steps:
            steps[n] = _broken(n, texts)
    problems += check(steps)
    return head, steps, problems


def _broken(n, texts):
    parts = [clean(t) for t in texts if clean(t)]
    return Step(n, parts[0] if parts else f"Step {n}", parts[1:], "", Outcome("end", ""), Outcome("end", ""), parts,
                broken=True)


# Older manuals (1999-2001) print each step as '10AA2 : CHECK ...' followed by the question and two answer lines
# whose YES / NO marks are pictures, so the converted text keeps the answers but not their labels.
LABEL_STEP_RE = re.compile(r"^(?:#+\s*)?(\d{1,2}[A-Z]{1,3})(\d{1,2})\s*:\s*(.+)$")
LABEL_QUESTION_RE = re.compile(r"^(?:CHECK\s*)?:\s*\*?(.+\?)\*?\s*$")
LABEL_ANSWER_RE = re.compile(r"^:\s*(.+)$")
ORDER_NOTE = ("This manual prints YES and NO as pictures, so the converted text lost those two words. The deck "
              "takes the answers in the manual's printed order (question, then YES, then NO), and only walks a step "
              "when it has exactly one question and two answers.")


ORDER = "answers taken in printed order"


def parse_labelled(lines):
    """({n: Step}, problems) for the older '10AA2 : CHECK ...' layout, or None if the lines aren't in it."""
    raw, prefix, cur = {}, None, None
    for line in lines:
        s = line.strip()
        if not s or s == "[fig]" or SEP_RE.match(s):
            continue
        m = LABEL_STEP_RE.match(s)
        if m and (prefix is None or m.group(1) == prefix):
            prefix = m.group(1)
            cur = raw.setdefault(int(m.group(2)), {"title": clean(m.group(3)), "ins": [], "q": [], "ans": [],
                                                   "yes": [], "no": [], "dup": False, "last": None, "lines": []})
            if cur["q"] or cur["ins"]:
                cur["dup"] = True
            continue
        if cur is None:
            continue
        cur["lines"].append(s)
        s = re.sub(r"^-\s+", "", s)
        lab = re.match(r"^(CHECK|YES|NO)\s*:\s*(.+)$", s)
        if lab:                                   # labels survived: use them, no guessing
            key = {"CHECK": "q", "YES": "yes", "NO": "no"}[lab.group(1)]
            cur[key].append(clean(lab.group(2).strip("* ")))
            cur["last"] = key
            continue
        if s.startswith(":") and "?" in s and re.search(r"\?\*?\s+:\s+", s):
            parts = [clean(x.strip("* ")) for x in re.split(r"(?<=\?)\*?\s+:\s+|\s+:\s+", s.lstrip(": "))]
            cur["q"].append(parts[0])
            cur["ans"] += parts[1:]
            cur["last"] = "ans"
            continue
        q = LABEL_QUESTION_RE.match(s)
        a = LABEL_ANSWER_RE.match(s)
        if q and not (cur["ans"] or cur["yes"] or cur["no"]):
            cur["q"].append(clean(q.group(1).strip("* ")))
            cur["last"] = "q"
        elif a and cur["q"]:
            cur["ans"].append(clean(a.group(1)))
            cur["last"] = "ans"
        elif cur["last"] in ("ans", "yes", "no") and cur[cur["last"]]:
            tail = re.split(r"\s+:\s+(?=\S)", s) if cur["last"] == "ans" and len(cur["ans"]) == 1 else [s]
            cur[cur["last"]][-1] += " " + clean(tail[0].lstrip("#- "))    # a NOTE under an answer belongs to it
            if len(tail) == 2:                    # '...NOTE text : Go to step 10AA13.': the next answer, run on
                cur["ans"].append(clean(tail[1]))
        elif not cur["q"]:
            cur["ins"].append(clean(s.lstrip("#- ")))
    if not raw:
        return None

    def local(text):
        """'Go to step 10AA7.' -> 'Go to step 7.' inside this chart; other charts' steps lose the 'step' form."""
        def sub(m):
            return f"step {int(m.group(2))}" if m.group(1) == prefix else f"chart step {m.group(1)}{m.group(2)}"
        return re.sub(r"step\s*(\d{1,2}[A-Z]{1,3})(\d{1,2})", sub, text)

    steps, problems, by_order = {}, [], False
    for n, r in sorted(raw.items()):
        if r["dup"]:
            problems.append(f"step {n} appears twice")
        if r["yes"] or r["no"]:
            answers = r["yes"] + r["no"] if (len(r["yes"]), len(r["no"]), len(r["ans"])) == (1, 1, 0) else None
        else:
            answers = r["ans"] if len(r["ans"]) == 2 else None
            by_order = by_order or answers is not None
        if len(r["q"]) != 1 or not answers:
            steps[n] = _broken(n, [r["title"]] + r["lines"])     # shown as the page text, never guessed
            continue
        yes, no = (local(x) for x in answers)
        steps[n] = Step(n, r["title"], r["ins"], r["q"][0], parse_outcome(yes), parse_outcome(no),
                        [r["title"], r["q"][0], yes, no])
    if by_order:
        problems.insert(0, ORDER)
    return steps, problems


def check(steps):
    """Reasons the tree can't be walked safely."""
    out = []
    if not steps:
        return ["no step table"]
    nums = sorted(steps)
    missing = [n for n in range(nums[0], nums[-1] + 1) if n not in steps]
    if missing:
        out.append("steps missing: " + ", ".join(map(str, missing[:8])))
    broken = [n for n, st in steps.items() if st.broken]
    if len(broken) * 2 > len(steps):
        out.append(f"{len(broken)} of {len(steps)} steps didn't convert")
    for st in steps.values():
        if st.broken:
            continue
        if not st.question:
            out.append(f"step {st.n} has no question")
        for name, o in (("Yes", st.yes), ("No", st.no)):
            if o.kind == "end":
                out.append(f"step {st.n} {name} is blank")
            if o.step is not None and o.step not in steps:
                out.append(f"step {st.n} {name} points to missing step {o.step}")
    return out


REFER_RE = re.compile(r"refer to DTC ([PBCU][0-9A-F]\d{3})", re.I)


def load(kb, veh, code, _seen=None):
    """The chart for a code on this car, following 'For the diagnostic procedure, refer to DTC X.'"""
    seen = _seen or set()
    seen.add(code)
    sections = kb.procedure_sections(veh, code)
    if not sections:
        return None
    sections = _chart_chapter(sections, code)
    procs = _procedures(sections)
    if len(procs) > 1:              # e.g. 'W: DTC C0041 ECM' and 'X: DTC C0041 PARAMETER SELECTION ERROR'
        charts = [_build(kb, veh, code, part, seen) for part in procs]
        charts = [c for c in charts if c]
        for c in charts:
            c.variant = re.sub(r"^[A-Z]{1,2}:\s*", "", c.sections[0].title.split(" (cont")[0]).strip()
        first = charts[0]
        first.alternates = charts[1:]
        return first
    return _build(kb, veh, code, sections, seen)


def _procedures(sections):
    """Split a code's sections into the manual's separate procedures, keyed by the procedure letter
    ('W:', 'X:') of their title; continued parts carry the same title and stay together."""
    groups = {}
    for s in sections:
        m = re.match(r"([A-Z]{1,2}):\s*DTC", s.title or "")
        groups.setdefault((s.doc, s.path.split(" > ")[0], m.group(1) if m else ""), []).append(s)
    real = [g for g in groups.values() if parse(g, None)[1]]
    return real if len(real) > 1 else [sections]


def _build(kb, veh, code, sections, seen):
    head, steps, problems = parse(sections, code)
    m = REFER_RE.search(" ".join(s.body for s in sections))
    if not steps and not m:
        m = _label_ref(kb, sections)
    if not steps and m and m.group(1) not in seen:
        target = load(kb, veh, m.group(1), seen)
        if target:
            target.via = code
            return target
    title = re.search(r"DTC [PBCU][0-9A-F]\d{3}[^\n(]*", sections[0].body)
    return Chart(code, clean(title.group(0)).strip(" —-") if title else code, sections, head, steps, problems)


def _chart_chapter(sections, code):
    """A code can be filed in two chapters: the diagnostic procedure (EN) and the detecting criteria (GD).
    Walk the chapter that holds the step chart; the other chapter's pages would add unrelated tables."""
    groups = {}
    for s in sections:
        groups.setdefault(s.path.split(" > ")[0], []).append(s)
    if len(groups) < 2:
        return sections
    scored = []
    for chap, part in groups.items():
        _, steps, problems = parse(part, code)
        scored.append((not steps, len(problems), -len(steps), chap, part))
    scored.sort(key=lambda t: t[:3])
    return scored[0][4] if not scored[0][0] else sections


LABEL_REF_RE = re.compile(r"refer to [\d-]+ \[T(\d{1,2}[A-Z]{1,3})0\]", re.I)


def _label_ref(kb, sections):
    """Older manuals say 'For the diagnostic procedure, refer to 2-7 [T10AA0].': find the chart whose steps are
    numbered 10AA1, 10AA2 ... in the same manual, and return a match-like object holding its code."""
    m = LABEL_REF_RE.search(" ".join(s.body for s in sections))
    if not m:
        return None
    label = m.group(1)
    row = kb.db.execute("SELECT s.title FROM section s JOIN doc ON doc.id=s.doc_id WHERE doc.title=? AND "
                        "(s.body LIKE ? OR s.body LIKE ?) ORDER BY s.page LIMIT 1",
                        (sections[0].doc, f"%\n{label}1 :%", f"%\n### {label}1 :%")).fetchone()
    code = CODE_RE.search(row[0] or "") if row else None
    return _Ref(code.group(0)) if code else None


class _Ref:
    def __init__(self, code):
        self._code = code

    def group(self, i):
        return self._code


# ---------------------------------------------------------------------------------------------- walking
@dataclass
class Answer:
    step: int
    answer: str          # Yes / No
    how: str             # scan | measured | mechanic
    detail: str = ""     # "13.2 V, limit more than 10 V" / "codes P0303, P0304"


class Walk:
    """One pass through a chart. Undo-able; every answer keeps how it was decided."""

    def __init__(self, chart, codes):
        self.chart = chart
        self.codes = list(codes)
        self.trail = []            # [Answer]
        self.current = chart.first()
        self.result = None         # the Outcome the walk ended on
        self.auto()

    def step(self):
        """The current step, if it can be answered here (a broken step is read from the page instead)."""
        st = self.chart.steps.get(self.current)
        return None if st is None or st.broken else st

    def broken_step(self):
        st = self.chart.steps.get(self.current)
        return st if st is not None and st.broken else None

    def auto(self):
        """Answer the scan-only steps from the codes, as far as they go."""
        while self.result is None and self.step():
            ans = self.step().scan_answer(self.codes, {self.chart.code, self.chart.via} - {None, ""})
            if ans is None:
                return
            self._take(ans, "scan", "codes " + ", ".join(self.codes))

    def answer(self, ans, how="mechanic", detail=""):
        self._take(ans, how, detail)
        self.auto()

    def measure(self, text):
        """Decide the current step from typed readings. Returns (answer, explanation) or (None, why not)."""
        st = self.step()
        lim = st.limit if st else None
        if not lim:
            return None, "this check has no single limit to compare against; answer it Y or N"
        vals = parse_reading(text, lim)
        if not vals:
            return None, "no number in that"
        ok = [lim.judge(v) for v in vals]
        ans = "Yes" if all(ok) else "No"
        shown = ", ".join(f"{v:g}" for v in vals)
        detail = f"read {shown} {lim.unit}, limit {lim.describe()}".replace("  ", " ")
        if len(vals) > 1 and not all(ok):
            detail += " (failing: " + ", ".join(f"{v:g}" for v, k in zip(vals, ok) if not k) + ")"
        self._take(ans, "measured", detail)
        self.auto()
        return ans, detail

    def _take(self, ans, how, detail):
        st = self.step()
        self.trail.append(Answer(st.n, ans, how, detail))
        out = st.yes if ans == "Yes" else st.no
        if out.kind == "goto":
            self.current = out.step
        else:
            self.result = out
            self.current = None

    def undo(self):
        """Back to the last step the mechanic (not the scan) answered."""
        while self.trail:
            a = self.trail.pop()
            self.result = None
            self.current = a.step
            if a.how != "scan":
                return True
        self.current = self.chart.first()
        self.auto()
        return False

    def jump(self, n):
        """Go straight to a step (the mechanic knows better); recorded in the trail."""
        if n in self.chart.steps:
            if self.current is not None:
                self.trail.append(Answer(self.current, "jump", "mechanic", f"jumped to step {n}"))
            self.result = None
            self.current = n
            return True
        return False
