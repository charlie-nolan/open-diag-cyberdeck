"""The app's state and actions, without any screen code (so it can be tested and scripted)."""
import json
import os
import re
import time

from . import chart as C
from . import repair as R
from .case import Case, Note, HOME
from .kb import KB, Vehicle

DEFAULTS = {
    "kb_dir": "~/subaru_cyberdeck/kb",
    "make": "subaru",
    "labor_rate": 100.0,
    "tax_rate": 0.0,
    "supplies_rate": 0.0,
    "out_dir": "~/shopdeck_workorders",
}


def load_config():
    path = os.path.join(HOME, "config.json")
    cfg = dict(DEFAULTS)
    try:
        with open(path) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    os.makedirs(HOME, exist_ok=True)
    tmp = os.path.join(HOME, "config.json.tmp")
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, os.path.join(HOME, "config.json"))


class Desk:
    def __init__(self, cfg=None, kb_path=None):
        self.cfg = cfg or load_config()
        path = kb_path or os.path.join(os.path.expanduser(self.cfg["kb_dir"]), f"{self.cfg['make']}.db")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no knowledge database at {path}")
        self.kb = KB(path)
        self.case = None
        self.veh = None
        self._charts = {}
        self.new_case()

    # ------------------------------------------------------------------ tickets
    def new_case(self):
        self.case = Case(labor_rate=self.cfg["labor_rate"], tax_rate=self.cfg["tax_rate"],
                         supplies_rate=self.cfg["supplies_rate"])
        self.veh = None
        self._charts = {}

    def open_case(self, case):
        self.case = case
        self._charts = {}
        self.veh = None
        if case.model and case.year:
            for v in self.kb.vehicles(case.model, case.year):
                if v.engine == case.engine or not case.engine:
                    self.veh = v
                    break

    def save(self):
        if not self.case.empty or os.path.exists(self.case.path):
            self.case.save()

    # ------------------------------------------------------------------ car
    def set_car(self, veh: Vehicle):
        self.veh = veh
        self.case.model, self.case.year, self.case.engine = veh.model, veh.year, veh.engine
        self._charts = {}
        self.save()

    # ------------------------------------------------------------------ codes
    def set_codes(self, codes, source="typed"):
        clean = []
        for c in codes:
            c = c.strip().upper()
            if C.CODE_RE.fullmatch(c) and c not in clean:
                clean.append(c)
        self.case.codes = clean
        self.case.code_source = source
        self._charts = {}
        self.save()

    def charts(self):
        """{code: Chart} for the case's codes; codes that share one chart share the object."""
        if not self.veh:
            return {}
        for code in self.case.codes:
            if code not in self._charts:
                ch = C.load(self.kb, self.veh, code)
                for other in self._charts.values():   # P0301..P0304 all use one chart
                    if ch and other and other.title == ch.title and other.source == ch.source:
                        ch = other
                        break
                self._charts[code] = ch
        return {c: self._charts[c] for c in self.case.codes if c in self._charts}

    def chart_groups(self):
        """[(chart, [codes it covers])] in the case's code order, one entry per distinct chart."""
        out = []
        for code, ch in self.charts().items():
            for g in out:
                if g[0] is ch and ch is not None:
                    g[1].append(code)
                    break
            else:
                out.append((ch, [code]))
        for ch, codes in list(out):          # the manual's other procedures for the same code
            for alt in (ch.alternates if ch else []):
                out.append((alt, codes))
        return out

    # ------------------------------------------------------------------ walking
    def key(self, ch):
        if not ch:
            return ""
        return (ch.via or ch.code) + (f"#{ch.variant}" if ch.variant else "")

    def walk(self, ch):
        """A Walk for this chart, replaying what was already answered on this ticket."""
        w = C.Walk(ch, self.case.codes)
        saved = self.case.walks.get(self.key(ch), {}).get("trail", [])
        for a in saved:
            if w.current is None:
                break
            if a["how"] == "scan":
                continue                  # the scan answers itself again from today's codes
            if a["answer"] == "jump":
                n = int(re.search(r"\d+", a["detail"]).group(0))
                w.jump(n)
                continue
            if w.current == a["step"]:
                w.answer(a["answer"], a["how"], a.get("detail", ""))
        return w

    def save_walk(self, ch, w):
        res = w.result.text if w.result else ""
        self.case.walks[self.key(ch)] = {
            "chart": ch.title, "source": ch.source,
            "trail": [vars(a) for a in w.trail], "result": res,
            "result_kind": w.result.kind if w.result else "", "at": time.time()}
        self.save()

    # ------------------------------------------------------------------ jobs
    def job_from_text(self, title, reason, from_sections=(), text_for_refs=""):
        """A job with the manual procedures the text points to (its <Ref.> links), else none yet."""
        j = R.Job(title=title, reason=reason)
        for sec in R.resolve_refs(self.kb, list(from_sections), text_for_refs or title):
            if sec.id not in j.procedures:
                j.procedures.append(sec.id)
        return j

    def job_from_walk(self, ch, w):
        out = w.result
        last = w.trail[-1] if w.trail else None
        reason = f"{ch.source}: {ch.title}" + (f", step {last.step} ({last.answer})" if last else "")
        title = re.split(r"(?<=\.)\s|;|<Ref", out.text)[0].strip(" .") or "Repair"
        return self.job_from_text(title, reason, ch.sections, out.text)

    def add_job(self, job):
        self.case.jobs.append(job)
        self.save()
        return job

    def labor_options(self, job_or_text):
        name = job_or_text.title if isinstance(job_or_text, R.Job) else job_or_text
        if not self.veh:
            return []
        return R.match_labor(self.kb.labor_rows(self.veh), name)

    def add_charm_row(self, job, row):
        if row["hours"]:
            job.labor.append(R.LaborLine(" ".join(x.strip() for x in row["operation"].split(">")[-3:]),
                                         float(row["hours"]),
                                         "CHARM book time: " + row["operation"]))
        if row["part_number"] or (row["price"] and not row["hours"]):
            price = R.price_of(row["price"])
            job.parts.append(R.PartLine(row["operation"].split(">")[-1].strip(), row["part_number"] or "", 1,
                                        price, "CHARM list price (old)" if price is not None else "CHARM"))
        self.save()

    def must_be_new(self, job):
        """[(section, line)] from the job's manual procedures that say a part must be new."""
        out = []
        for sid in job.procedures:
            for sec in self.kb.topic_sections(sid):
                for line in R.must_be_new(sec):
                    out.append((sec, line))
        return out

    def quote(self):
        return R.quote(self.case.jobs, self.case.labor_rate, self.case.tax_rate, self.case.supplies_rate)

    # ------------------------------------------------------------------ the mechanic's knowledge
    def add_note(self, text, kind):
        self.case.notes.append(Note(text, kind))
        self.save()

    def reuse_check(self, part_text):
        """For a part the mechanic reused: the manual procedures that handle it and what they say must be new.
        Returns [(section, lines)] so the screen can show 'the manual says to use a new one here'."""
        if not self.veh:
            return []
        out = []
        for cand in R.procedure_candidates(self.kb, self.veh, part_text, limit=6):
            lines = []
            for sec in self.kb.topic_sections(cand.id):
                lines += [l for l in R.must_be_new(sec) if _mentions(l, part_text)]
            if lines:
                out.append((cand, lines))
        return out

    # ------------------------------------------------------------------ output
    def work_order(self):
        charts = {}
        for ch, codes in self.chart_groups():
            if ch:
                charts[self.key(ch)] = ch
        from .report import work_order
        return work_order(self.case, self.kb, charts, os.path.expanduser(self.cfg["out_dir"]))


def _mentions(line, part_text):
    """Does a 'must be new' line talk about the part named (any of its main words)?"""
    words = [w for w in re.findall(r"[a-z]{3,}", part_text.lower()) if w not in {"the", "and", "new", "old", "reused"}]
    low = line.lower()
    return any(w.rstrip("s") in low for w in words)
