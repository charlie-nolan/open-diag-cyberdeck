"""
Deterministic lookups that feed the LLMs. Standard library only (runs on the Orange Pi).

Sources, all read-only:
  subaru_diag.db            DTC definitions + FSM diagnostic procedures (built by build_subaru_diag_db.py)
  test_subaru_knowledge.db  FSM chunks + fast_specs (built by ingest/build_test_db.py etc.)
A small FTS5 cache over the FSM chunks is built once in cache/fsm_fts.db, so the
knowledge DB itself is never modified.

Every snippet carries a [source] tag so the model (and you) can see where it came from.
"""
import html
import os
import re
import sqlite3

DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
STOP = set("the a an and or of to in on for with is are was it this that at by from be as my car "
           "engine when after before under over what next could would should can does did how why "
           "which there then than about check think maybe still also just like really have has had "
           "you your but not yes now".split())


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def _fts_query(text, max_terms=8):
    words = [w for w in re.findall(r"[A-Za-z0-9]{3,}", text.lower()) if w not in STOP]
    seen, terms = set(), []
    for w in words:
        if w not in seen:
            seen.add(w)
            terms.append(f'"{w}"')
    return " OR ".join(terms[:max_terms])


def _clean(md, limit):
    md = re.sub(r"<[^>]+>", "", md)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "[diagram]", md)
    md = html.unescape(md.replace("**", ""))
    md = re.sub(r"[ \t]{2,}", " ", md)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return md[:limit]


class Retriever:
    def __init__(self, diag_db, knowledge_db, cache_dir):
        self.diag = _ro(diag_db) if os.path.isfile(diag_db) else None
        self.kb = _ro(knowledge_db) if os.path.isfile(knowledge_db) else None
        self.fts = None
        if self.kb is not None:
            os.makedirs(cache_dir, exist_ok=True)
            self.fts = self._open_fts(os.path.join(cache_dir, "fsm_fts.db"), knowledge_db)

    # ---------------------------------------------------------------- FSM full-text cache
    def _open_fts(self, path, knowledge_db):
        src_mtime = str(int(os.path.getmtime(knowledge_db)))
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        row = conn.execute("SELECT v FROM meta WHERE k='src_mtime'").fetchone()
        if not row or row[0] != src_mtime:
            conn.executescript("""
                DROP TABLE IF EXISTS fsm;
                CREATE VIRTUAL TABLE fsm USING fts5(chunk_id UNINDEXED, breadcrumb, content,
                                                    tokenize='porter unicode61');
            """)
            rows = self.kb.execute("SELECT id, breadcrumb, content FROM manual_chunks").fetchall()
            conn.executemany("INSERT INTO fsm VALUES (?,?,?)",
                             [(i, _clean(b or "", 300), _clean(c or "", 100000)) for i, b, c in rows])
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('src_mtime', ?)", (src_mtime,))
            conn.commit()
        return conn

    # ---------------------------------------------------------------- lookups
    def dtc(self, code):
        """Definition + FSM procedure for one DTC from subaru_diag.db."""
        if self.diag is None:
            return ""
        code = code.upper()
        out = []
        for desc, ref in self.diag.execute(
                "SELECT description, fsm_ref FROM dtc_subaru WHERE code=? LIMIT 2", (code,)):
            out.append(f"[FSM DTC list] {code}: {desc}" + (f" (ref {ref.split(',')[0]})" if ref else ""))
        steps = self.diag.execute(
            "SELECT section_title, content FROM dtc_procedure WHERE code=? ORDER BY seq", (code,)).fetchall()
        if steps:
            body = "\n".join(_clean(c, 600) for _, c in steps)
            out.append(f"[FSM procedure {code}]\n{body}")
        return "\n".join(out)

    def search_fsm(self, text, k=3, chars=900):
        if self.fts is None:
            return []
        q = _fts_query(text)
        if not q:
            return []
        rows = self.fts.execute(
            "SELECT breadcrumb, content FROM fsm WHERE fsm MATCH ? ORDER BY bm25(fsm, 0, 2.0, 1.0) LIMIT ?",
            (q, k)).fetchall()
        return [f"[FSM: {b}]\n{c[:chars]}" for b, c in rows]

    def specs(self, term, limit=8):
        """fast_specs rows. Hand-seeded rows are labelled unverified."""
        if self.kb is None or not term.strip():
            return []
        like = f"%{term.strip()}%"
        rows = self.kb.execute(
            "SELECT subsystem, component, torque_ft_lb, torque_nm, socket_size_mm, oem_part_no, notes "
            "FROM fast_specs WHERE (component LIKE ? OR aliases LIKE ? OR subsystem LIKE ?) "
            "AND oem_part_no = 'Catalog Ref' LIMIT ?",
            (like, like, like, limit)).fetchall()
        out = []
        for sub, comp, ft, nm, sock, pn, notes in rows:
            # Only rows parsed from the FSM's own tables. The hand-entered rows are Gemini's fabricated
            # "hand-verified" specs and the JIS rows are generic bolt charts, not Subaru values: a
            # wrong torque label is still a wrong torque, so neither is shown or fed to the model.
            if pn != "Catalog Ref":
                continue
            src = "FSM table"
            tool = f", {sock}" if sock and sock not in ("See FSM Callout", "See Procedure") else ""
            out.append(f"[{src}] {comp}: {nm} N·m ({ft} ft-lb){tool}" + (f" ({notes})" if notes else ""))
        return out

    def context_for(self, text, budget=2500):
        """Everything relevant to a free-text case description, trimmed to a char budget."""
        parts = []
        for code in dict.fromkeys(c.upper() for c in DTC_RE.findall(text)):
            d = self.dtc(code)
            if d:
                parts.append(d)
        parts += self.search_fsm(text, k=2)
        out, used = [], 0
        for p in parts:
            if used + len(p) > budget:
                p = p[:max(0, budget - used)]
            if p:
                out.append(p)
                used += len(p)
            if used >= budget:
                break
        return "\n\n".join(out)
