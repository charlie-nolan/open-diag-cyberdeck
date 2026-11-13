"""
Lookups over subaru.db for the cyberdeck app. Standard library only (runs on the Orange Pi).

Drop-in for app/retrieval.py's Retriever: same methods (dtc, search_fsm, specs, context_for),
same string formats, but one read-only DB and no cache to build.

    from kb_retriever import Retriever
    r = Retriever("kb/subaru.db", model="Forester", year=2002)

Vector search is optional: pass embed=<callable(str) -> 1024 int8 bytes from Qwen3-Embedding-0.6B with prompt_name="query"> and have sqlite-vec
installed, and search_fsm fuses BM25 and vector ranks. Without it, FTS5 alone is used.
"""
import os
import re
import sqlite3
import threading

try:            # the app's step-table renderer (app/procedures.py); without it dtc() returns the raw text
    import procedures as _proc
except ImportError:
    _proc = None

DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
STOP = set("the a an and or of to in on for with is are was it this that at by from be as my car "
           "engine when after before under over what next could would should can does did how why "
           "which there then than about check think maybe still also just like really have has had "
           "you your but not yes now".split())


# Shop words -> the FSM's words. (On the SF/EJ251 the "exhaust manifold" is the front exhaust pipe.)
# "a|b" = alternatives. Vocabulary only: each entry says the same thing in the manual's words. Never map a symptom to a suspected
# cause here ("shakes" -> "misfire"), or every search would lean toward that cause.
SYNONYMS = {
    "exhaust manifold": "front exhaust pipe", "cam belt": "timing belt|camshaft drive belt",
    "timing belt": "camshaft drive belt", "o2 sensor": "oxygen sensor",
    "a/f sensor": "oxygen sensor", "air-fuel sensor": "oxygen sensor", "air fuel sensor": "oxygen sensor",
    "air-fuel ratio sensor": "oxygen sensor", "air fuel ratio sensor": "oxygen sensor", "af sensor": "oxygen sensor",
    "head gasket": "cylinder head",
    "lug nut": "wheel nut", "lug nuts": "wheel nut", "valve cover": "rocker cover", "tps": "throttle position sensor",
    "maf": "mass air flow", "ect": "engine coolant temperature", "cps": "crankshaft position sensor",
    "iac": "idle air control", "check engine": "malfunction indicator lamp", "cel": "malfunction indicator lamp",
    "mil": "malfunction indicator lamp", "tick": "engine noise", "ticking": "engine noise",
    "won't start": "engine will not start", "wont start": "engine will not start", "no start": "engine will not start",
    "doesn't start": "engine will not start", "flashing": "blinking", "flashes": "blinks", "flash": "blink",
    "shakes at idle": "rough idling", "shaking at idle": "rough idling", "shaky idle": "rough idling",
    "rough idle": "rough idling", "idles rough": "rough idling", "runs rough": "rough idling",
    "shakes": "vibration|rough", "shaking": "vibration|rough", "shake": "vibration|rough", "shudder": "vibration",
    "stumbles": "hesitation", "stumble": "hesitation", "hesitates": "hesitation", "bogs": "hesitation",
    "step on the gas": "acceleration", "gas pedal": "accelerator pedal", "alternator": "generator",
    "ac": "air conditioner", "gas smell": "gasoline smell|fuel odor",
    "transmission fluid": "atf", "trans fluid": "atf", "brake pads": "pad", "rotor": "disc rotor",
    "squeal": "squeak|noise", "squealing": "squeak|noise", "clunk": "noise", "rattle": "noise",
    "pulls to one side": "pulling to one side", "pulls": "pulling", "dies": "stalls", "stalling": "stalls",
    "antifreeze": "coolant", "overheats": "overheating",
    "battery dying": "battery discharge", "not charging": "charging system",
}
_SYN_RE = [(re.compile(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])"), v) for k, v in SYNONYMS.items()]
_REPAIR_RE = re.compile(r"\b(replace|replacement|remove|removal|install|installation|change|swap|how (do|to)|torque|"
                        r"overhaul|disassembl|adjust)", re.I)

# Where/what kind of fastener, not which part: kept out of the strict spec match (they stay when they are
# part of the manual's own name from SYNONYMS, as in "front exhaust pipe" or "wheel nut").
SPEC_GENERIC = set("front rear left right upper lower inner outer side new old gasket gaskets seal seals bolt bolts "
                   "nut nuts screw screws replace replacement repair removal remove install installation torque spec "
                   "specs leak leaking faulty worn clogged failure failing failed bad broken damaged defective dirty "
                   "loose cracked stuck weak open short circuit malfunction "
                   # wiring faults and control units: a torque value for these is never what a card needs
                   "wiring wire wires harness connector connectors ground grounds grounding terminal terminals "
                   "electrical ecu ecm pcm tcm".split())


def _slash_variants(text):
    """'o2/air-fuel sensor' -> ['o2 sensor', 'air-fuel sensor'] (both names kept); 'a/c', 'a/f' stay as they are."""
    m = re.search(r"([a-z0-9][a-z0-9-]+)/([a-z0-9][a-z0-9-]+)", text, re.I)
    if not m:
        return [text]
    left, right = text[:m.start()], text[m.end():]
    return [v for side in (m.group(1), m.group(2)) for v in _slash_variants(f"{left}{side}{right}")]

_VALUE_RE = re.compile(r"\d\s*(n·m|nm|ft-lb|in-lb|kpa|psi|mm|ohm|Ω|°[cf]|v\b|a\b|rpm|liters?|qts?)", re.I)


def _thin(path, body, min_chars=40):
    """True when a page has almost no text of its own: drop its heading words and figure/step labels and see
    what is left (CHARM pages whose content is all in figures read 'Part 1: Part 2: NOTE: ...')."""
    if _VALUE_RE.search(body):    # a short spec page ("Tensioner bolts 6.4 Nm") is not thin
        return False
    head = set(re.findall(r"[a-z0-9]+", path.lower()))
    rest = re.sub(r"\[fig\]|!\[[^\]]*\]\([^)]*\)|\b(part|step|steps|figure|fig)\s*[\d\s\-–]*:|wiring diagram:|"
                  r"\(s[0-9a-z]{6,}\)|malfunction of parts other than those listed is also possible\.?|"
                  r"after repair or replacement of faulty parts, conduct clear memory mode and inspection mode\.?|"
                  r"check or repair only faulty parts\.?|\b(note|caution|warning):", " ", body, flags=re.I)
    words = [w for w in re.findall(r"[a-z0-9]+", rest.lower()) if w not in head]
    return sum(len(w) + 1 for w in words) < min_chars


def _shingles(body, chars=1500):
    w = re.findall(r"[a-z0-9]+", body[:chars].lower())
    return {" ".join(w[i:i + 3]) for i in range(max(0, len(w) - 2))}


def _expand(text):
    """The query plus the manual's wording for any shop words in it."""
    low = f" {text.lower()} "
    return low, [alt for rx, v in _SYN_RE if rx.search(low) for alt in v.split("|")]


def _fts_query(text, max_terms=16):
    """OR of the question's words, each manual-wording synonym as one phrase (so 'check engine light' adds
    "malfunction indicator lamp" once instead of three loose words that drown the rest of the question),
    plus each adjacent word pair as a phrase so 'timing belt' outranks pages that only say 'timing' and 'belt'."""
    low, extra = _expand(text)
    words = [w for w in re.findall(r"[a-z0-9]{3,}", low) if w not in STOP]
    syn = []
    for alt in extra:
        toks = [w for w in re.findall(r"[a-z0-9]+", alt) if w not in STOP]
        if toks:
            syn.append(" ".join(toks))
    toks = re.findall(r"[a-z0-9]+", low)
    pairs = [f"{a_} {b_}" for a_, b_ in zip(toks, toks[1:])
             if a_ not in STOP and b_ not in STOP and len(a_) > 2 and len(b_) > 2]
    terms = list(dict.fromkeys(syn + pairs[:6] + words))[:max_terms + len(syn)]
    return " OR ".join(f'"{t}"' for t in terms)


# The knowledge table keeps NHTSA's model names; these are the same cars as the manuals' model names.
MODEL_ALIASES = {
    "IMPREZA": ["IMPREZA", "WRX", "WRX STI", "IMPREZA STI", "IMPREZA WRX", "OUTBACK SPORT"],
    "B9": ["B9 TRIBECA", "TRIBECA"], "TRIBECA": ["TRIBECA", "B9 TRIBECA"], "LOYALE": ["LOYALE", "LOYAL"],
    "XT-6": ["XT-6", "XT6"], "XT": ["XT", "XT-6", "XT6"], "CROSSTREK": ["CROSSTREK", "XV CROSSTREK"],
}


VEC_ALL_CARS_MAX = 200000    # rows searched with no year/model filter; keyword search still runs above this


class Retriever:
    def __init__(self, db_path, model=None, year=None, engine=None, embed=None):
        db_path = os.path.expanduser(db_path)
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.embed = None
        self._mats = {}
        self._mats_lock = threading.Lock()   # the app warms this up on a thread while the screen may ask too
        if embed is not None:
            try:
                import sqlite_vec
                self.db.enable_load_extension(True)
                sqlite_vec.load(self.db)
                self.db.enable_load_extension(False)
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='section_vec'").fetchone():
                    self.embed = embed
            except Exception:
                self.embed = None
        self.model, self.year = model, int(year) if year else None
        self.vehicle_ids = None
        if model:
            sql, args = "SELECT id FROM vehicle WHERE upper(model)=upper(?)", [model]
            if year:
                sql += " AND year=?"
                args.append(int(year))
            if engine:
                sql += " AND engine=?"
                args.append(engine)
            self.vehicle_ids = [r[0] for r in self.db.execute(sql, args)] or None

    # ---------------------------------------------------------------- helpers
    def _strip_car(self, text):
        """'2011 Legacy C0054' -> 'C0054' when this Retriever is already scoped to the 2011 Legacy: the year and
        model name only match index pages and other-year bulletins, the scope does that job exactly."""
        if not self.vehicle_ids and not self.model:
            return text
        drop = {str(self.year)} if self.year else set()
        drop |= {w.lower() for w in re.findall(r"[A-Za-z0-9]+", self.model or "")}
        out = " ".join(w for w in text.split() if w.lower().strip(",.") not in drop)
        return out if re.search(r"[A-Za-z0-9]{3,}", out) else text

    def _code_sections(self, text):
        """Sections the DB links to each trouble code named in the text, for this car: an exact lookup beats
        any ranking (C-codes and many P1xxx codes never appear in a page's words)."""
        out = []
        vf, args = self._veh_filter()
        for code in dict.fromkeys(c.upper() for c in DTC_RE.findall(text)):
            rows = self.db.execute(
                "SELECT s.id FROM dtc_procedure d JOIN section s ON s.id=d.section_id JOIN doc ON doc.id=s.doc_id "
                f"WHERE d.code=?{vf} GROUP BY s.id ORDER BY doc.kind='charm', s.id", [code] + args).fetchall()
            out += [r[0] for r in rows if r[0] not in out]
        return out

    def _veh_filter(self, col="s.id"):
        if not self.vehicle_ids:
            return "", []
        q = ",".join("?" * len(self.vehicle_ids))
        return (f" AND {col} IN (SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({q}))",
                list(self.vehicle_ids))

    def _fmt(self, path, page, body, chars, kind="fsm"):
        src = "CHARM" if kind == "charm" else "FSM"
        tag = f"[{src}: {path}" + (f", p.{page}]" if page else "]")
        return f"{tag}\n{body[:chars]}"

    def _row(self, section_id):
        return self.db.execute("SELECT s.path, s.page, s.body, d.kind FROM section s JOIN doc d ON d.id=s.doc_id "
                               "WHERE s.id=?", (section_id,)).fetchone()

    def section(self, section_id, chars=4000):
        row = self._row(section_id)
        return self._fmt(*row[:3], chars, row[3]) if row else ""

    def refs(self, section_id):
        """Sections this one points to with <Ref. to ...>: [(ref_text, section_id or None)]."""
        return self.db.execute("SELECT ref_text, to_id FROM section_ref WHERE from_id=?", (section_id,)).fetchall()

    # ---------------------------------------------------------------- lookups
    def dtc(self, code, chars=1500):
        """Definition + FSM diagnostic procedure for one DTC."""
        code = code.upper()
        out = []
        row = self.db.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone()
        if row and row[0]:
            out.append(f"[DTC] {code}: {row[0]}")
        vf, args = self._veh_filter()
        secs = self.db.execute(
            "SELECT s.path, s.page, s.body, doc.kind FROM dtc_procedure d JOIN section s ON s.id=d.section_id "
            f"JOIN doc ON doc.id=s.doc_id WHERE d.code=?{vf} GROUP BY s.id ORDER BY doc.kind='charm', s.id",
            [code] + args).fetchall()
        proc = _proc.fetch(self, code) if _proc is not None and chars > 0 else None
        if proc and proc["steps"]:     # whole numbered steps (app/procedures.py), not the first chars of a table
            out.append(_proc.render(proc, [code], budget=chars))
            return "\n".join(out)
        if any(kind != "charm" for *_, kind in secs):   # CHARM's copy of the same FSM procedure only eats the budget
            secs = [r for r in secs if r[3] != "charm"]
        used = 0
        for path, page, body, kind in secs:
            if used >= chars:
                break
            out.append(self._fmt(path, page, body, chars - used, kind))
            used += len(body)
        return "\n".join(out)

    FSM_BOOST = 5     # an FSM hit ranks as if it were this many places higher than a CHARM hit

    def card(self, code):
        """Pre-baked diagnostic card for this code and car (causes ranked by confirmed fixes, with sources)."""
        sql, args = "SELECT text FROM dtc_card WHERE code=?", [code.upper()]
        if self.model:
            sql += " AND upper(model)=upper(?)"
            args.append(self.model)
            if self.year:
                sql += " AND ? BETWEEN year_min AND year_max"
                args.append(self.year)
        try:
            row = self.db.execute(sql + " ORDER BY year_max - year_min LIMIT 1", args).fetchone()
        except sqlite3.OperationalError:      # DB built before the cards step existed
            return ""
        return f"[CARD] {row[0]}" if row else ""

    # What kind of page a question wants. Problem descriptions want the manual's symptom / trouble tables and
    # diagnostic procedures; repair jobs want removal / installation pages. This says nothing about causes.
    DIAG_PAGE = re.compile(r"general diagnos|trouble in general|symptom|diagnostic procedure|diagnostics? chart|"
                           r"\bdtc\b|basic diagnostic|check list for interview", re.I)
    REPAIR_PAGE = re.compile(r"removal|installation|replacement|replace|assembly|overhaul", re.I)
    INTENT_BOOST = 0.25   # fused score x (1 + this) for a page of the kind the question wants

    def _intent(self, text):
        if _REPAIR_RE.search(text):
            return self.REPAIR_PAGE
        if len(re.findall(r"[a-z]{3,}", text.lower())) >= 3 and not DTC_RE.search(text):
            return self.DIAG_PAGE
        return None

    def _bm25(self, q, n):
        vf, args = self._veh_filter()
        rows = self.db.execute(
            "SELECT s.id, d.kind FROM section_fts JOIN section s ON s.id=section_fts.rowid JOIN doc d ON d.id=s.doc_id "
            f"WHERE section_fts MATCH ?{vf} ORDER BY bm25(section_fts, 4.0, 2.0, 1.0) LIMIT ?",
            [q] + args + [n * 2]).fetchall()
        ranked = sorted(enumerate(rows), key=lambda x: x[0] - (self.FSM_BOOST if x[1][1] == "fsm" else 0))
        return [r[0] for _, r in ranked][:n]

    def _matrix(self, table, ids_sql, args):
        """int8 vectors for this car's rows of one vec table, loaded once: an exact dot-product search over
        them can't lose this car's pages behind other cars' near neighbours the way a global top-k can."""
        key = (table, ids_sql, tuple(args))
        with self._mats_lock:
            return self._matrix_locked(key, table, ids_sql, args)

    def _matrix_locked(self, key, table, ids_sql, args):
        if key not in self._mats:
            import numpy as np
            ids, vecs = [], bytearray()      # one buffer: a list of blobs plus their join needs twice the memory
            # only this car's rows: reading every row and filtering in Python took 6.6 s for a 2003 Tahoe on
            # chevrolet.db (1.2M rows) on the PC, this 0.4 s; the deck is several times slower at both
            sql = f"SELECT id, embedding FROM {table}" + (f" WHERE id IN ({ids_sql})" if ids_sql else "")
            for i, blob in self.db.execute(sql, args if ids_sql else []):
                ids.append(i)
                vecs += blob
            m = np.frombuffer(vecs, dtype=np.int8).reshape(len(ids), -1) if ids else None
            self._mats[key] = (np.array(ids), m)
        return self._mats[key]

    def _vec_search(self, table, ids_sql, args, text, n):
        try:
            import numpy as np
            # no car picked on a big make: minutes on the main thread, and the answer wouldn't be about this car
            if not ids_sql and self.db.execute(f"SELECT count(*) FROM {table}_rowids").fetchone()[0] > VEC_ALL_CARS_MAX:
                return []
            ids, m = self._matrix(table, ids_sql, args)
            if m is None:
                return []
            qv = np.frombuffer(self.embed(text), dtype=np.int8).astype(np.int32)
        except Exception as e:      # embedder missing or broken on this box: carry on with keywords only
            print(f"kb_retriever: vector search off ({e})")
            self.embed = None
            return []
        # a block at a time: the whole matrix as int32 is 4 GB+ on a big make, too much beside the deck's model
        sims = np.concatenate([m[i:i + 50000].astype(np.int32) @ qv for i in range(0, len(m), 50000)])
        top = np.argsort(-sims)[:n]
        return [int(ids[i]) for i in top]

    def _vector(self, text, n):
        vf = ""
        args = []
        if self.vehicle_ids:
            vf = f"SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({','.join('?' * len(self.vehicle_ids))})"
            args = list(self.vehicle_ids)
        return self._vec_search("section_vec", vf, args, text, n)

    def search_ids(self, text, k=3):
        first = self._code_sections(text)
        text = self._strip_car(text)
        q = _fts_query(text)
        lists = [self._bm25(q, 30)] if q else []
        if self.embed is not None:
            lists.append(self._vector(text, 30))
        weights = [1.0, self.VEC_WEIGHT if len(text.split()) >= 6 else 1.0][:len(lists)]
        score = {}
        for w, lst in zip(weights, lists):      # weighted reciprocal-rank fusion
            for rank, sid in enumerate(lst):
                score[sid] = score.get(sid, 0) + w / (60 + rank)
        want = self._intent(text)
        bodies = {}
        if score:
            q_ = ",".join("?" * len(score))
            for sid, path, body in self.db.execute(f"SELECT id, path, body FROM section WHERE id IN ({q_})", list(score)):
                bodies[sid] = body
                if want is not None and want.search(path):
                    score[sid] *= 1 + self.INTENT_BOOST
                if _thin(path, body):
                    score[sid] *= self.THIN_PENALTY
        # CHARM repeats one page per engine/emission variant; keep the best of near-identical pages so the
        # few slots the models get aren't five copies of the same text.
        if first:
            q_ = ",".join("?" * len(first))
            bodies.update(self.db.execute(f"SELECT id, body FROM section WHERE id IN ({q_})", first).fetchall())
        out, kept = [], []
        for sid in first + [i for i in sorted(score, key=score.get, reverse=True) if i not in first]:
            sh = _shingles(bodies.get(sid, ""))
            if any(len(sh & o) > self.DUP_JACCARD * len(sh | o) for o in kept if sh and o):
                continue
            kept.append(sh)
            out.append(sid)
            if len(out) == k:
                break
        return out

    THIN_PENALTY = 0.3  # pages whose text is only headings ("Part 1: Part 2:"), the content being in figures
    DUP_JACCARD = 0.6   # pages sharing more than this share of word triples count as one
    VEC_WEIGHT = 2.0   # long plain-English questions match keywords badly, so the vector list counts more

    def search_fsm(self, text, k=3, chars=900):
        """Top k procedures; parts of the same split procedure count once."""
        out, seen = [], set()
        for sid in self.search_ids(text, k * 3):
            row = self._row(sid)
            if row is None:   # vector/FTS index still holds a section that was deleted (e.g. a duplicate doc)
                continue
            path, page, body, kind = row
            base = path.split(" — ")[0]
            if base in seen:
                continue
            seen.add(base)
            out.append(self._fmt(path, page, body, chars, kind))
            if len(out) == k:
                break
        return out

    def specs(self, term, limit=8):
        """Torque specs as printed in the FSM (or CHARM tables), each with the procedure it came from."""
        q = _fts_query(term)
        if not q:
            return []
        # Strictest first: every part word of the job (or of its manual wording) must appear, so
        # 'exhaust manifold gasket' can't pull in front ABS sensors just because both say 'front'.
        # Then it drops one or two words at a time, keeping the last ones (the part noun comes last:
        # 'fuel injector', 'intake manifold'). It never falls back to any-word matching: no spec beats
        # a spec for a different part.
        part = lambda t: [w for w in re.findall(r"[a-z0-9]{3,}", t) if w not in STOP and w not in SPEC_GENERIC]  # noqa: E731
        variants = []
        for alt_term in _slash_variants(term):     # 'front o2/air-fuel sensor' = two names for one part
            low, _ = _expand(alt_term)
            low = re.sub(r"\b(cylinder|cyl|bank)\s*#?\d\b", " ", low)   # 'cylinder 3' says where, not what
            syn = []
            for rx, v in _SYN_RE:          # the manual's name first, kept whole ('front exhaust pipe', 'wheel nut')
                if rx.search(low):
                    syn += [[w for w in re.findall(r"[a-z0-9]{3,}", alt) if w not in STOP] + part(rx.sub(" ", low))
                            for alt in v.split("|")]
            own = part(low)
            if len(own) > 1 or not syn:      # a lone leftover word ('head' of 'head gasket') only if nothing else
                syn.append(own)
            variants += syn
        variants = [list(v) for v in dict.fromkeys(tuple(dict.fromkeys(ws)) for ws in variants if ws)]
        low = " ".join(_expand(t)[0] for t in _slash_variants(term))
        tiers = []
        for drop in (0, 1, 2):
            for ws in variants:
                n = len(ws) - drop
                if n < 1 or (drop and n < 2):       # never down to one word of a longer name ('head' of 'cylinder head')
                    continue
                for i in range(len(ws) - n, -1, -1):     # contiguous windows, the one ending at the last word first
                    tiers.append(" AND ".join(f'"{w}"' for w in ws[i:i + n]))
        vf, args = self._veh_filter()
        rows, seen = [], set()
        for tq in dict.fromkeys(tiers):
            for r in self.db.execute(
                    "SELECT p.id, p.component, p.value_si, p.tol_si, p.value_us, p.tol_us, p.unit_us, s.title, s.topic, "
                    "s.page, d.kind, p.raw FROM spec_fts f JOIN spec p ON p.id=f.rowid JOIN section s ON s.id=p.section_id "
                    f"JOIN doc d ON d.id=s.doc_id WHERE spec_fts MATCH ?{vf} "
                    "ORDER BY d.kind='charm', bm25(spec_fts, 3.0, 1.0) LIMIT ?", [tq] + args + [limit]):
                if r[0] not in seen:
                    seen.add(r[0])
                    rows.append(r[1:])
            if rows:
                break
        # Rows from the part's own procedure ('13. Crankshaft Pulley') before rows that only mention it.
        # 'front'/'rear' are too common to require, but they decide between the part's own procedures.
        pw = set(part(low)) | (set(re.findall(r"[a-z]+", low)) & {"front", "rear", "left", "right", "upper", "lower"})
        rows.sort(key=lambda r: (r[9] == "charm",
                                 -len(pw & set(re.findall(r"[a-z0-9]{3,}", f"{r[7] or ''} {r[6] or ''}".lower())))))
        out = []
        for comp, nm, nm_tol, us, us_tol, us_unit, title, topic, page, kind, raw in rows:
            vals = []
            if nm is not None:
                vals.append(f"{nm:g}" + (f"±{nm_tol:g}" if nm_tol else "") + " N·m")
            if us is not None:
                vals.append(f"{us:g}" + (f"±{us_tol:g}" if us_tol else "") + f" {us_unit or 'ft-lb'}")
            value = f"{vals[0]} ({vals[1]})" if len(vals) == 2 else vals[0] if vals else "?"
            where = f"{topic}, {title}" if topic and topic != title else title
            src = "CHARM" if kind == "charm" else f"FSM p.{page}"
            if (raw or "").startswith("image "):   # kb/car_db.py vision rows: check the picture if it matters
                src = "manual picture, read by vision model, unverified"
            out.append(f"[{src}, {where}] {comp}: {value}")
        return out

    def labor(self, term, limit=8):
        """Book labor hours, then part numbers/prices, from CHARM labor and parts tables for this car.
        Rows whose operation name (the last parts of 'Exhaust > Exhaust Manifold > Replace') holds the
        whole phrase rank first; a word matched only higher up the path counts less."""
        words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]{3,}", term.lower())) if w not in STOP][:6]
        if not words:
            return []
        phrase = " ".join(words)
        need = 1 if len(words) == 1 else 2
        hits = " + ".join("(l.operation LIKE ?)" for _ in words)
        vf, args = self._veh_filter("l.section_id")
        rows = self.db.execute(
            f"SELECT DISTINCT l.operation, l.hours, l.part_number, l.price FROM labor l "
            f"WHERE ({hits}) >= ?{vf} LIMIT 2000",
            [f"%{w}%" for w in words] + [need] + args).fetchall()

        def score(op):
            segs = [x.lower() for x in op.split(" > ")]
            tail, full = " ".join(segs[-3:]), " ".join(segs)
            sc = 4 * (phrase in tail) + 2 * (phrase in full)
            sc += sum(2 * (w in tail) + (w in full) for w in words)
            return sc - len(op) / 1000
        def tail_hits(op):           # words found in the operation itself, not in 'Engine, Cooling and Exhaust' above it
            tail = " ".join(op.lower().split(" > ")[-3:])
            return sum(w in tail for w in words)
        rows = [r for r in rows if (r[1] or r[2] or r[3]) and tail_hits(r[0]) >= need]
        exact = [r for r in rows if phrase in " ".join(r[0].lower().split(" > ")[-3:])]
        if len(words) > 1 and exact:  # 'exhaust manifold' rows exist: don't pad with 'intake manifold'
            rows = exact
        rows.sort(key=lambda r: (score(r[0]), bool(r[1])), reverse=True)   # best path match first; hours win ties
        seen, uniq = set(), []
        for r in rows:
            key = (" > ".join(r[0].split(" > ")[-2:]).lower(), r[1], r[2], r[3])
            if key not in seen:
                seen.add(key)
                uniq.append(r)
        out = []
        for op, hours, part, price in uniq[:limit]:
            bits = [f"{hours:g} h" if hours else None, f"part {part}" if part else None,
                    (price if price.startswith("$") else f"${price}") if price else None]
            out.append(f"[CHARM {'labor' if hours else 'part'}] {op}: " + ", ".join(b for b in bits if b))
        return out

    def _knowledge_scope(self, kinds=None, all_models=False):
        where, args = [], []
        if self.model and not all_models:
            names = MODEL_ALIASES.get(self.model.upper(), [self.model.upper()])
            where.append(f"upper(k.model) IN ({','.join('?' * len(names))})")
            args += names
            if self.year:
                where.append("? BETWEEN coalesce(k.year_min, 0) AND coalesce(k.year_max, 9999)")
                args.append(self.year)
        if kinds:
            where.append(f"k.kind IN ({','.join('?' * len(kinds))})")
            args += list(kinds)
        return where, args

    def knowledge_ids(self, text, k=3, kinds=None, all_models=False):
        text = self._strip_car(text)
        q = _fts_query(text)
        where, args = self._knowledge_scope(kinds, all_models)
        lists = []
        if q:
            sql = ("SELECT k.id FROM knowledge_fts f JOIN knowledge k ON k.id=f.rowid WHERE knowledge_fts MATCH ?"
                   + "".join(f" AND {w}" for w in where) + " ORDER BY bm25(knowledge_fts) LIMIT 30")
            lists.append([r[0] for r in self.db.execute(sql, [q] + args)])
        if self.embed is not None and self._has_table("knowledge_vec"):
            ids_sql = "SELECT k.id FROM knowledge k" + (" WHERE " + " AND ".join(where) if where else "")
            lists.append(self._vec_search("knowledge_vec", ids_sql, args, text, 30))
        score = {}
        for w, lst in zip([1.0, self.VEC_WEIGHT if len(text.split()) >= 6 else 1.0], lists):
            for rank, i in enumerate(lst):
                score[i] = score.get(i, 0) + w / (60 + rank)
        if score:   # the maker's own bulletins and recalls before one owner's complaint about the same thing
            q_ = ",".join("?" * len(score))
            for i, kind in self.db.execute(f"SELECT id, kind FROM knowledge WHERE id IN ({q_})", list(score)):
                score[i] *= self.KIND_WEIGHT.get(kind, 1.0)
        return sorted(score, key=score.get, reverse=True)[:k]

    KIND_WEIGHT = {"tsb": 1.5, "recall": 1.5}

    def knowledge(self, text, k=3, chars=600, kinds=None, all_models=False):
        """TSBs, NHTSA complaints/recalls and forum fixes, limited to this car's model/year unless all_models."""
        out, seen = [], set()
        for i in self.knowledge_ids(text, k * 3, kinds, all_models):
            row = self.db.execute("SELECT kind, model, year_min, title, body FROM knowledge WHERE id=?", (i,)).fetchone()
            if not row:
                continue
            kind, model, yr, title, body = row
            key = (body or "")[:300]        # NHTSA files one complaint once per component it names
            if key in seen:
                continue
            seen.add(key)
            out.append(f"[{kind}, {model} {yr}] {title or ''}\n{(body or '')[:chars]}")
            if len(out) == k:
                break
        return out

    def _has_table(self, name):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None

    def context_for(self, text, budget=2500):
        """Everything relevant to a free-text case description, trimmed to a char budget."""
        parts = []
        for code in dict.fromkeys(c.upper() for c in DTC_RE.findall(text)):
            c = self.card(code)
            if c:
                parts.append(c[:1200])
            d = self.dtc(code, chars=max(900, budget // 2 // max(1, len(set(DTC_RE.findall(text))))))
            if d:
                parts.append(d)
        parts += self.search_fsm(text, k=2)
        parts += self.knowledge(text, k=1)
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
