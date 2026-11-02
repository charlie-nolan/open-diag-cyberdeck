"""Read-only access to the knowledge database (kb/<make>.db).

Everything the app shows comes from here with its source attached: which manual, which page, which
section. Nothing in this file decides anything about a car; it only finds and returns manual text.
"""
import re
import sqlite3
from dataclasses import dataclass, field

CODE_RE = re.compile(r"\b[PBCU][0-9A-F]\d{3}\b")


@dataclass
class Vehicle:
    model: str
    year: int
    engine: str                      # label shown to the user, e.g. "2.5L" or "2.5L turbo"
    ids: list = field(default_factory=list)   # every vehicle row (FSM and CHARM) for this car

    @property
    def label(self):
        return f"{self.year} {self.model} {self.engine}".strip()


@dataclass
class Section:
    id: int
    doc: str            # manual title
    kind: str           # fsm | charm
    path: str
    title: str
    page: int
    body: str

    @property
    def source(self):
        if self.kind == "charm":
            return f"CHARM: {self.path}"
        short = re.sub(r"\s*\(.*?\)|\s+G\d{4}\w*|\s+MSA\w+", "", self.doc).strip()
        return f"{short}, p.{self.page}" if self.page else short


def engine_key(engine):
    """'EJ251' and 'F4-2.5L SOHC' describe the same engine; reduce both to ('2.5', turbo?)."""
    e = (engine or "").upper()
    m = re.search(r"(\d\.\d)L", e)
    if m:
        return m.group(1), "TURBO" in e
    m = re.match(r"(EJ|EZ|FB|FA)(\d{2})(\d?)", e)
    if m:
        size = {"EJ20": "2.0", "EJ22": "2.2", "EJ25": "2.5", "EZ30": "3.0", "EZ36": "3.6",
                "EJ18": "1.8", "EJ15": "1.5", "FB25": "2.5", "FB20": "2.0", "FA20": "2.0"}.get(m.group(1) + m.group(2))
        turbo = (m.group(1), m.group(2), m.group(3)) in {("EJ", "20", "5"), ("EJ", "20", "7"), ("EJ", "25", "5"),
                                                          ("EJ", "25", "7"), ("FA", "20", "1")}
        return size, turbo
    return None, False


def engine_label(key):
    size, turbo = key
    if not size:
        return "engine unknown"
    return f"{size}L" + (" turbo" if turbo else "")


class KB:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}

    # ------------------------------------------------------------------ vehicles
    def models(self):
        return [r[0] for r in self.db.execute("SELECT DISTINCT model FROM vehicle ORDER BY model")]

    def years(self, model):
        return [r[0] for r in self.db.execute("SELECT DISTINCT year FROM vehicle WHERE model=? ORDER BY year", (model,))]

    def vehicles(self, model, year):
        """One Vehicle per engine for this model/year, each holding every matching row (manual and CHARM)."""
        rows = self.db.execute("SELECT id, engine FROM vehicle WHERE model=? AND year=?", (model, year)).fetchall()
        groups = {}
        for r in rows:
            groups.setdefault(engine_key(r["engine"]), []).append(r["id"])
        unknown = groups.pop((None, False), [])
        out = [Vehicle(model, year, engine_label(k), ids + unknown) for k, ids in sorted(groups.items())]
        return out or [Vehicle(model, year, "engine unknown", unknown)]

    def coverage(self, veh):
        """What the kb holds for this car: manual titles and CHARM page count."""
        q = ",".join("?" * len(veh.ids))
        return self.db.execute(
            f"SELECT doc.title, doc.kind, COUNT(*) n FROM section_vehicle sv JOIN section s ON s.id=sv.section_id "
            f"JOIN doc ON doc.id=s.doc_id WHERE sv.vehicle_id IN ({q}) GROUP BY doc.id ORDER BY doc.kind DESC",
            veh.ids).fetchall()

    # ------------------------------------------------------------------ sections
    def _section(self, row):
        return Section(row["id"], row["doc"], row["kind"], row["path"], row["title"], row["page"], row["body"])

    _SEL = ("SELECT s.id, doc.title doc, doc.kind kind, s.path, s.title, s.page, s.body FROM section s "
            "JOIN doc ON doc.id=s.doc_id ")

    def section(self, sid):
        r = self.db.execute(self._SEL + "WHERE s.id=?", (sid,)).fetchone()
        return self._section(r) if r else None

    def topic_sections(self, sid):
        """All sections of the same manual topic (e.g. '2. Spark Plug': removal, installation, inspection)."""
        r = self.db.execute("SELECT doc_id, chapter, topic FROM section WHERE id=?", (sid,)).fetchone()
        if not r or not r["topic"]:
            return [self.section(sid)]
        rows = self.db.execute(self._SEL + "WHERE s.doc_id=? AND s.chapter IS ? AND s.topic=? ORDER BY s.page, s.id",
                               (r["doc_id"], r["chapter"], r["topic"])).fetchall()
        return [self._section(x) for x in rows]

    def resolve_ref(self, from_id, ref_text):
        """A '<Ref. to FU-52, ...>' inside a section, as the section it points to (when the kb linked it)."""
        if "section_ref" not in self.tables:
            return None
        r = self.db.execute("SELECT to_id FROM section_ref WHERE from_id=? AND ref_text=?",
                            (from_id, ref_text)).fetchone()
        return self.section(r[0]) if r and r[0] else None

    def refs(self, from_id):
        if "section_ref" not in self.tables:
            return []
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT ref_text, to_id FROM section_ref WHERE from_id=? AND to_id IS NOT NULL", (from_id,))]

    def search(self, veh, text, kind=None, limit=12, any_word=False):
        """Manual sections for this car matching the words (all of them, or any with any_word), best first."""
        words = [w for w in re.findall(r"[A-Za-z0-9]{2,}", text) if w.lower() not in STOP]
        if not words:
            return []
        match = (" OR " if any_word else " ").join(f'"{w}"' for w in words)
        q = ",".join("?" * len(veh.ids))
        kind_sql = " AND doc.kind=?" if kind else ""
        args = [match] + veh.ids + ([kind] if kind else []) + [limit]
        rows = self.db.execute(
            self._SEL.replace("FROM section s", "FROM section_fts f JOIN section s ON s.id=f.rowid") +
            f"WHERE section_fts MATCH ? AND s.id IN (SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({q}))"
            f"{kind_sql} ORDER BY bm25(section_fts, 8.0, 4.0, 1.0) LIMIT ?", args).fetchall()
        return [self._section(r) for r in rows]

    # ------------------------------------------------------------------ trouble codes
    def dtc_name(self, code):
        r = self.db.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone()
        return r[0] if r and r[0] else ""

    def procedure_sections(self, veh, code):
        """The manual's diagnostic procedure for a code on this car, in page order. Factory manual pages win
        over CHARM's copy (CHARM's step charts are mostly pictures)."""
        q = ",".join("?" * len(veh.ids))
        rows = self.db.execute(
            self._SEL + f"JOIN dtc_procedure p ON p.section_id=s.id WHERE p.code=? AND p.vehicle_id IN ({q}) "
            "GROUP BY s.id ORDER BY doc.kind='charm', s.doc_id, s.page, s.id", [code] + veh.ids).fetchall()
        secs = [self._section(r) for r in rows]
        fsm = self.engine_chapters(veh, [s for s in secs if s.kind == "fsm"])
        charm = [s for s in secs if s.kind == "charm" and "Technical Service Bulletin" not in s.path]
        return fsm or charm

    def engine_chapters(self, veh, secs):
        """Manuals covering several engines repeat each chart per engine chapter, e.g. 'EN(H4SO)' and
        'EN(H4DOTC)'. Keep the chapter(s) for this car's engine (H4/H6 = 4 or 6 cylinders, TC = turbo) and
        chapters with no engine tag; when two tagged variants still fit, keep the first and note the other."""
        size, turbo = engine_key(veh.engine.replace("turbo", "TURBO"))
        six = size in ("3.0", "3.6")
        groups = {}
        for s in secs:
            groups.setdefault(s.path.split(" > ")[0], []).append(s)
        if len(groups) < 2 or not size:
            return secs
        keep, tagged = [], []
        for chap, part in groups.items():
            tag = re.search(r"\(([^)]*)\)", chap)
            t = tag.group(1).upper() if tag else ""
            if not re.search(r"H[46]|TURBO|TC", t):
                keep += part
                continue
            if ("TC" in t or "TURBO" in t) != turbo:
                continue
            if "H6" in t and not six or "H4" in t and six:
                continue
            tagged.append((chap, part))
        # the same chart in two chapters that both fit (e.g. 'EN(H4SO)' and 'EN(H4SO U5)'): one set of steps only
        seen_kinds = {}
        for chap, part in tagged:
            kind = chap.split("(")[0]
            if kind in seen_kinds:
                note = Section(-1, part[0].doc, "note", chap, "", part[0].page,
                               f"This manual also has a version of this chart in chapter {chap} (p.{part[0].page}).")
                keep.append(note)
                continue
            seen_kinds[kind] = chap
            keep += part
        return sorted(keep, key=lambda s: (s.kind == "note", s.page or 0, s.id)) if keep else secs

    def bulletins(self, veh, code):
        """Subaru technical service bulletins (CHARM's copy) that name this code, for this car."""
        q = ",".join("?" * len(veh.ids))
        rows = self.db.execute(
            self._SEL + f"JOIN dtc_procedure p ON p.section_id=s.id WHERE p.code=? AND p.vehicle_id IN ({q}) "
            "AND s.path LIKE '%Technical Service Bulletin%' GROUP BY s.id", [code] + veh.ids).fetchall()
        return [self._section(r) for r in rows]

    def symptom_tables(self, veh):
        """The manual's 'General Diagnostic Table' pages for this car (symptom -> parts to check, in order)."""
        q = ",".join("?" * len(veh.ids))
        rows = self.db.execute(
            self._SEL + f"WHERE doc.kind='fsm' AND (s.topic LIKE '%General Diagnostic Table%' "
            f"OR s.path LIKE '%General Diagnostic Table%') "
            f"AND s.id IN (SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({q})) ORDER BY s.page, s.id",
            veh.ids).fetchall()
        return [self._section(r) for r in rows]

    # ------------------------------------------------------------------ labor and parts (CHARM)
    def labor_rows(self, veh):
        if "labor" not in self.tables:
            return []
        q = ",".join("?" * len(veh.ids))
        return [dict(r) for r in self.db.execute(
            f"SELECT DISTINCT l.operation, l.hours, l.part_number, l.price FROM labor l "
            f"JOIN section_vehicle sv ON sv.section_id=l.section_id WHERE sv.vehicle_id IN ({q})", veh.ids)]


STOP = {"the", "and", "for", "with", "from", "into", "of", "to", "on", "in", "a", "an", "or", "is", "it", "at",
        "by", "be", "as", "my", "car", "check", "replace", "repair", "new", "old"}
