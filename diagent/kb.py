"""The make's knowledge DB and its DTC procedures, split the way the agent needs them.

app/procedures.py parses the FSM's "No. | Step | Check | Yes | No" tables. It is used here as is
(parse, answered, path_from_scan, _next), but its fetch() merges every section linked to a code into
one table, so two engine variants of the same chart (NA and turbo, each starting at step 1) overwrite
each other, and pages the PDF converter attached to the wrong title get mixed in. Here each section is
parsed on its own, a restart at step 1 starts a new variant, and every step keeps the section and page
it came from so it can be cited. Steps the converter mangled are marked broken, and sections whose
steps never name the code's own component are marked suspect: both are shown as page text, never walked.
"""
import os
import re

import procedures as P
import vehicle_id
from kb_retriever import Retriever, _expand

# words in a DTC title that don't say which component (kept out of the relevance check)
GENERIC = set("sensor sensors circuit circuits low high input output voltage malfunction system control range "
              "performance bank signal dtc detected the and for too engine problem side open short fault "
              "intermittent erratic value".split())
# vocabulary only (same rule as kb_retriever.SYNONYMS): other names the manual uses for the same part
ALSO = {"ho2s": ["oxygen"], "o2": ["oxygen"], "evap": ["evaporative"], "egr": ["exhaust gas recirculation"],
        "ckp": ["crankshaft"], "cmp": ["camshaft"], "map": ["pressure"], "tps": ["throttle"],
        "iat": ["intake air temperature"], "ect": ["coolant"], "maf": ["mass air flow"],
        "ecu": ["ecm", "engine control module"], "pcm": ["ecm"], "temp": ["temperature"]}
REFER_ONLY = re.compile(r"(diagnostic procedure|for the diagnosis)[^.]{0,40}refer to DTC ([PBCU]\d{4})", re.I)
LETTERS = re.compile(r"[A-Za-z]{3,}")


def open_kb(make, model=None, year=None, engine=None, kb_dir=None):
    """(Retriever, path) for the make's DB, scoped to the car. Raises FileNotFoundError when the make has no DB."""
    path, ok = vehicle_id.kb_path_for(make, kb_dir)
    if not ok:
        raise FileNotFoundError(f"no knowledge DB for {make!r} (looked for {path})")
    r = Retriever(path, model=model, year=year, engine=engine)
    return r, path


def models_for_year(r, year):
    return [m for (m,) in r.db.execute("SELECT DISTINCT model FROM vehicle WHERE year=? ORDER BY 1", (int(year),))]


def _linked(r, code):
    """[(section_id, path, page, kind, body)] linked to code for this car, manual order; CHARM only without FSM."""
    vf, args = r._veh_filter()
    rows = r.db.execute(
        "SELECT s.id, s.path, s.page, doc.kind, s.body FROM dtc_procedure d JOIN section s ON s.id=d.section_id "
        f"JOIN doc ON doc.id=s.doc_id WHERE d.code=?{vf} GROUP BY s.id ORDER BY doc.kind='charm', s.id",
        [code] + list(args)).fetchall()
    if any(k != "charm" for _, _, _, k, _ in rows):
        rows = [x for x in rows if x[3] != "charm"]
    return rows


def _keywords(r, code, title):
    row = r.db.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone()
    text = f"{(row[0] if row and row[0] else '')} {title}".lower()
    text = re.sub(r"\bdtc [pbcu]\d{4}\b", " ", text)
    words = {w for w in re.findall(r"[a-z0-9]{2,}", text) if w not in GENERIC and not w.isdigit()
             and not re.fullmatch(r"[pbcu]\d{4}", w)}
    alts = set()
    for w in list(words):
        alts.update(ALSO.get(w, []))
    alts.update(_expand(text)[1])
    return {w for w in words if len(w) >= 3} | alts


def _mentions(text, keys):
    low = text.lower()
    return [k for k in keys if re.search(rf"(?<![a-z]){re.escape(k)}", low)]


def _broken(step, check, yes, no):
    """True when a row isn't a usable step: number-table junk, or no check / no branches."""
    if not LETTERS.search(step) or not LETTERS.search(check):
        return True
    return not (LETTERS.search(yes) and LETTERS.search(no))


HEAD_RE = re.compile(r"^\s*#{1,4}\s*(?:[A-Z]{1,3}\s*:\s*)?DTC\s+[PBCU]\d{4}")
REF_TAG = re.compile(r"<Ref\. to [A-Z]{2,3}\(([A-Z0-9]+)\)")
TAG_RE = re.compile(r"\(([A-Z0-9][A-Z0-9 .-]*)\)")


def _segments(rows, code):
    """Split the linked sections at the manual's own DTC headings. The PDF converter often hangs many
    procedures under one title (each section starts with that title again), so a step table belongs to the
    last real heading above it, not to the section's title. Yields (sid, path, page, heading, codes, text)."""
    prev_sid, prev_codes, prev_head = None, None, None
    for sid, path, page, kind, body in rows:
        title = path.split(" > ")[-1].split(" — ")[0]
        title_codes = set(P.CODE_RE.findall(title))
        if prev_sid is not None and sid == prev_sid + 1 and prev_codes is not None:
            codes, head = prev_codes, prev_head            # a continued section: same procedure as before
        else:
            codes, head = title_codes, title
        seg, seen_row, first_head = [], False, True
        for line in body.splitlines():
            st = line.strip()
            if HEAD_RE.match(st):
                hc = set(P.CODE_RE.findall(st.split("—")[0]))
                if first_head and not seen_row and hc == title_codes:
                    first_head = False                     # the title repeated at the top of the section
                    continue
                first_head = False
                if seg:
                    yield sid, path, page, head, codes, "\n".join(seg)
                seg, codes, head = [], hc, P._clean(st.lstrip("# "))
                continue
            if st.startswith("|") and re.match(r"\|\s*\d+\s*\|", st):
                seen_row = True
            seg.append(line)
        if seg:
            yield sid, path, page, head, codes, "\n".join(seg)
        prev_sid, prev_codes, prev_head = sid, codes, head


def tag_of(path, text):
    """The manual's engine/variant tag: from the chapter name ('EN(H4SO)'), else the most common tag in the
    chart's own cross-references ('<Ref. to GD(H4DOTC)-84 ...>'), else ''."""
    m = TAG_RE.search(path.split(" > ")[0])
    if m and tag_features(m.group(1)):
        return m.group(1)
    refs = [t for t in REF_TAG.findall(text) if tag_features(t)]
    return max(set(refs), key=refs.count) if refs else ""


def tag_features(tag):
    """{cyl, cam, turbo} a manual tag states: 'H4SO' -> 4 cyl SOHC, 'H4DOTC' -> 4 cyl DOHC turbo, 'TURBO'."""
    t, f = (tag or "").upper(), {}
    m = re.search(r"(?<![A-Z0-9])[HFVIL]?(\d{1,2})(SO|DO)(TC)?(?![A-Z0-9])", t)
    if m:
        f.update(cyl=int(m.group(1)), cam="SOHC" if m.group(2) == "SO" else "DOHC", turbo=bool(m.group(3)))
    if re.search(r"NON-?TURBO|\bNA\b", t):
        f["turbo"] = False
    elif "TURBO" in t:
        f["turbo"] = True
    return f


def engine_features(text, decode=False):
    """{cyl, disp, cam, turbo} an engine description states; {} for a bare code name, which says nothing by
    itself. decode=True also reads a trim's displacement ('3.0R', '2.0i' in an ECU list or VIN decode)."""
    t, f = text or "", {}
    if decode:
        d = re.search(r"(?<![\d.])(\d\.\d)(?:\s*L\b|[A-Za-z]{0,2}\b)", t)
        if d:
            f["disp"] = d.group(1)
    m = re.search(r"(?<![A-Z0-9])[FHVIL](\d{1,2})(?![0-9])", t, re.I)
    if m:
        f["cyl"] = int(m.group(1))
    d = re.search(r"(\d\.\d)\s*L\b", t, re.I)
    if d:
        f["disp"] = d.group(1)
    if decode and "turbo" not in f and not re.search(r"turbo|supercharg", t, re.I):
        f.pop("turbo", None)        # a trim name that doesn't say turbo doesn't say "not turbo" either
    c = re.search(r"\b(SOHC|DOHC)\b", t, re.I)
    if c:
        f["cam"] = c.group(1).upper()
    if re.search(r"turbo|supercharg", t, re.I):
        f["turbo"] = True
    elif (f.get("disp") or f.get("cyl")) and not decode:
        f["turbo"] = False          # kb descriptions name forced induction when there is one
    return f


def fits(tagf, carf):
    """False only when the tag and the car's engine state different things; unknown never excludes."""
    return all(tagf[k] == carf[k] for k in ("cyl", "cam", "turbo", "disp") if k in tagf and k in carf)


def tag_extras(tag):
    """Tokens in a tag beyond the engine code: market/emission spec sections ('H4SO U5' -> {'U5'})."""
    toks = re.findall(r"[A-Z0-9]+", (tag or "").upper())
    return {t for t in toks if not re.fullmatch(r"[HFVIL]?\d{1,2}(SO|DO)(TC)?|TURBO|NON|NA", t)}


def spec_tokens(*texts):
    """Spec codes a decode names (two-character letter+digit tokens like 'U5'), compared with tag_extras."""
    return {t.upper() for x in texts for t in re.findall(r"(?<![A-Za-z0-9])[A-Za-z]\d(?![A-Za-z0-9])", x or "")}


def _doc_years(r, sid):
    row = r.db.execute("SELECT d.title FROM section s JOIN doc d ON d.id=s.doc_id WHERE s.id=?", (sid,)).fetchone()
    return {int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", row[0] if row else "")}


def engine_choices(r, model, year):
    """(choices, {choice: vehicle ids}). Manuals name engines two ways for one car: code names ('EJ251') from
    the FSM, whose sections link to every engine of the model, and descriptions ('F4-2.5L SOHC') from CHARM.
    When descriptions exist they are the choices, and the code-name rows join every choice's scope."""
    rows = r.db.execute("SELECT id, engine FROM vehicle WHERE upper(model)=upper(?) AND year=?",
                        (model, int(year))).fetchall()
    desc = sorted({e for _, e in rows if e and engine_features(e)})
    codes = [i for i, e in rows if not e or not engine_features(e)]
    if not desc:
        names = sorted({e for _, e in rows if e})
        return names, {n: [i for i, e in rows if e == n] for n in names}
    return desc, {n: [i for i, e in rows if e == n] + codes for n in desc}


def procedure(r, code, _seen=None, car_features=None, car=None):
    """{code, title, via, keywords, variants, pages, excluded} or None when nothing is linked to code here.
    variants: [{key, label, tag, steps: {n: {...}}}]; excluded: labels of variants for another engine."""
    code = code.upper()
    seen = _seen or set()
    seen.add(code)
    rows = _linked(r, code)
    if not rows:
        return None
    bodies = [b for *_, b in rows]
    m = None
    for b in bodies:
        m = m or REFER_ONLY.search(b)
    segs = [sg for sg in _segments(rows, code) if not sg[4] or code in sg[4]]   # only this code's own charts
    parsed = [(sg, {n: st for n, st in P.parse([sg[5]])[1].items() if n < 100}) for sg in segs]
    if not any(st for _, st in parsed) and m and m.group(2) not in seen:   # "For the diagnostic procedure, refer to DTC X."
        target = procedure(r, m.group(2), seen, car_features, car)
        if target:
            target["via"] = code
            return target
    t = re.search(r"DTC [PBCU]\d{4}[^\n(]*", bodies[0])
    title = P._clean(t.group(0)) if t else code
    keys = _keywords(r, code, title)
    variants, cur, cur_head = [], None, None
    for (sid, path, page, head, codes, text), steps in parsed:
        if not steps:
            continue
        if cur is None or (cur["steps"] and (min(steps) == 1 or head != cur_head)):
            cur = {"key": f"v{len(variants) + 1}", "steps": {}, "sections": [], "path": path, "page": page,
                   "head": head, "text": ""}
            variants.append(cur)
            cur_head = head
        cur["text"] += "\n" + text
        rows_text = " ".join(" ".join(x) for x in steps.values())
        # under its own DTC heading a chart is this code's chart; the word check is only for tables with no heading
        suspect = not codes and bool(keys) and not _mentions(rows_text, keys)
        if sid not in cur["sections"]:
            cur["sections"].append(sid)
        for n, (step, check, yes, no) in steps.items():
            status = "broken" if _broken(step, check, yes, no) else "suspect" if suspect else "ok"
            cur["steps"][n] = {"n": n, "step": step, "check": check, "yes": yes, "no": no,
                               "sid": sid, "page": page, "path": path, "status": status}
    kept, excluded = [], []
    for v in variants:
        v["tag"] = tag_of(v["path"], v.pop("text"))
        first = v["steps"][min(v["steps"])]["step"].split(";")[0]
        good = sum(st["status"] == "ok" for st in v["steps"].values())
        v["usable"] = good
        v["label"] = (f"{v['tag'] or v['path'].split(' > ')[0][:40]} | {v['head'][:70]} | p.{v['page']} | "
                      f"{len(v['steps'])} steps, {good} usable | step {min(v['steps'])}: {first[:50]} | "
                      f"{v['steps'][min(v['steps'])]['check'][:60]}")
        if car_features and v["tag"] and not fits(tag_features(v["tag"]), car_features):
            excluded.append(v["label"] + " (other engine)")
        else:
            kept.append(v)
    year = (car or {}).get("year")
    if year and len(kept) > 1:       # a chart from a manual whose title names other years only
        yrs = {v["key"]: _doc_years(r, v["sections"][0]) for v in kept}
        if any(year in y for y in yrs.values()):
            excluded += [v["label"] + " (manual for other years)" for v in kept if yrs[v["key"]] and year not in yrs[v["key"]]]
            kept = [v for v in kept if not yrs[v["key"]] or year in yrs[v["key"]]]
    spec = (car or {}).get("spec") or set()
    if spec and len(kept) > 1:       # market/emission spec sections the car's decode names
        hit = [v for v in kept if tag_extras(v["tag"]) and tag_extras(v["tag"]) <= set(spec)]
        if hit:
            excluded += [v["label"] + " (other spec)" for v in kept if v not in hit and tag_extras(v["tag"])]
            kept = [v for v in kept if v in hit or not tag_extras(v["tag"])]
    uniq = []
    for v in kept:                   # the same table printed twice is one chart
        sig = tuple((n, x["step"], x["check"], x["yes"], x["no"]) for n, x in sorted(v["steps"].items()))
        if all(sig != u[0] for u in uniq):
            uniq.append((sig, v))
    kept = [v for _, v in uniq]
    for v in kept:                   # what tells the remaining ones apart, for the question to the user
        others = [o for o in kept if o is not v]
        diff = sorted({n for o in others for n in set(v["steps"]) | set(o["steps"])
                       if (v["steps"].get(n) or {}).get("check") != (o["steps"].get(n) or {}).get("check")})
        v["differs"] = (f"spec {' '.join(sorted(tag_extras(v['tag'])))}; " if tag_extras(v["tag"]) else "") + \
            (f"steps {', '.join(map(str, diff[:6]))} differ" if diff else "")
    for i, v in enumerate(kept, 1):
        v["key"] = f"v{i}"
    pages = [{"sid": sid, "path": path, "page": page, "kind": kind, "chars": len(body)}
             for sid, path, page, kind, body in rows]
    return {"code": code, "title": title, "via": None, "keywords": sorted(keys), "variants": kept,
            "excluded": excluded, "pages": pages}


def next_of(cell):
    return P._next(cell)


def section_row(r, sid):
    return r.db.execute("SELECT s.path, s.page, s.body, d.kind, s.images FROM section s JOIN doc d ON d.id=s.doc_id "
                        "WHERE s.id=?", (int(sid),)).fetchone()


def section_in_scope(r, sid):
    if not r.vehicle_ids:
        return True
    q = ",".join("?" * len(r.vehicle_ids))
    return r.db.execute(f"SELECT 1 FROM section_vehicle WHERE section_id=? AND vehicle_id IN ({q}) LIMIT 1",
                        [int(sid)] + list(r.vehicle_ids)).fetchone() is not None


def figures(r, sid, image_dirs):
    """[(name, local path or '')] for a section's figures: FSM images, then CHARM pictures."""
    row = section_row(r, sid)
    out = []
    if row and row[4]:
        import json
        try:
            names = json.loads(row[4])
        except ValueError:
            names = []
        for n in names:
            path = next((os.path.join(d, n) for d in image_dirs if os.path.isfile(os.path.join(d, n))), "")
            out.append((n, path))
    try:
        for (key,) in r.db.execute("SELECT img_key FROM charm_image WHERE section_id=? LIMIT 8", (int(sid),)):
            out.append((key, ""))
    except Exception:
        pass
    return out


def scan_answers(steps, codes):
    """{step: 'Yes'|'No'} for the steps that only ask which DTCs the scan shows (procedures.answered)."""
    tup = {n: (s["step"], s["check"], s["yes"], s["no"]) for n, s in steps.items() if s["status"] != "broken"}
    out = P.answered(tup, codes)
    have = set(codes)
    for n, (_, check, _, _) in tup.items():   # "Does the scan tool display DTC X and Y (at the same time)?"
        listed = P.CODE_RE.findall(check)       # procedures.answered only knows 'indicate' / 'displayed'
        m = re.search(r"\b(display|show)s?\b[^?]*?\bDTCs?\b", check, re.I)
        if n not in out and listed and m and not re.search(r"\bor\b", check[m.end():]):   # 'X or Y': not sure
            out[n] = "Yes" if all(c in have for c in listed) else "No"
    return out


def open_car(car, kb_dir):
    """The Retriever for an identified car: scoped to the vehicle rows identify() picked (engine merge)."""
    r, path = open_kb(car["make"], car.get("model"), car.get("year"), None, kb_dir)
    if car.get("vehicle_ids"):
        r.vehicle_ids = list(car["vehicle_ids"])
    return r, path


# words that start a manual instruction (plain English imperative verbs; nothing about any car)
ACTION = re.compile(r"^(?:\d+[.)]\s*)?(measure|check|inspect|verify|turn|disconnect|connect|start|stop|remove|install|"
                    r"test|observe|perform|replace|repair|clear|command|operate|ensure|apply|probe|ignition|"
                    r"raise|run|drive|set|select|read|monitor|record|compare|wiggle|move|backprobe|unplug|"
                    r"plug|locate|key|allow|wait|shake|warm|depress|release|open|close|jump|use)\b", re.I)


# block headings on a page; background blocks are skipped when the page also has test blocks
BLOCK_HEAD = re.compile(r"^(#+\s*)?([A-Z][A-Za-z/ ,()&-]{3,60}|[A-Z][A-Z/ ,()&-]{3,60})$")
BACKGROUND = re.compile(r"description|conditions? for|action taken|clearing|diagnostic aids|possible cause|reference|"
                        r"descriptor|notice|important|caution|warning|specification|circuit operation|"
                        r"theory|overview|location", re.I)
TESTING = re.compile(r"verification|testing|\btest\b|diagnostic (procedure|table|chart)|inspection|"
                     r"troubleshoot", re.I)


def _blocks(body):
    """[(heading, [lines])] in page order."""
    out, head, cur = [], "", []
    for raw in (body or "").splitlines():
        st = raw.strip()
        if st and not st.endswith((".", ":", "?")) and BLOCK_HEAD.match(st) and len(st.split()) <= 8 \
                and (BACKGROUND.search(st) or TESTING.search(st)):
            if cur:
                out.append((head, cur))
            head, cur = st.lstrip("# "), []
        else:
            cur.append(raw)
    if cur:
        out.append((head, cur))
    return out


def page_units(body, limit=40):
    """The page's instructions in its own words, in the page's order: [{n, text, question, block, step}].
    Background blocks (description, conditions, diagnostic aids ...) are left out when the page has test
    blocks. A numbered item ('3.' on its own line) keeps its number: it is that step of the page's table.
    Lines the converter broke mid-sentence are joined; a question or 'If ...' goes with the action before it."""
    blocks = _blocks(body)
    tests = [(h, ls) for h, ls in blocks if h and TESTING.search(h)]     # 'TEST DESCRIPTION' is a test block
    use = tests or [(h, ls) for h, ls in blocks if not (h and BACKGROUND.search(h))] or blocks
    units = []
    for head, ls in use:
        lines, buf, step = [], "", None
        for raw in ls:
            st = raw.strip()
            num = re.fullmatch(r"(\d{1,2})\.", st)
            if num:
                if buf:
                    lines.append((buf.strip(), step))
                buf, step = "", int(num.group(1))
                continue
            if not st or st in ("*", "-", "•", "^", "[fig]") or st.startswith("|---"):
                if buf:
                    lines.append((buf.strip(), step))
                    buf = ""
                continue
            if st.startswith(("#", "|")) or re.match(r"^\d+[.)]\s", st) or buf.endswith((".", "?", ":")):
                if buf:
                    lines.append((buf.strip(), step))
                buf = st.lstrip("# ")
            else:
                buf += " " + st
        if buf:
            lines.append((buf.strip(), step))
        for ln, step in lines:
            ln = P._clean(ln.replace("|", " "))
            for sent in [x.strip() for x in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9])", ln) if x.strip()]:
                if re.match(r"^(if|does|do|is|are|was|were|did|has|have)\b", sent, re.I) and units:
                    units[-1]["question"] = (units[-1]["question"] + " " + sent).strip()
                elif step is not None and units and units[-1]["step"] == step and units[-1]["block"] == head:
                    units[-1]["text"] += " " + sent          # one table step, one prompt
                elif (ACTION.match(sent) or step is not None) and len(sent) > 12:
                    units.append({"n": len(units) + 1, "text": sent, "question": "", "block": head, "step": step})
                if len(units) >= limit:
                    return units
    return units


SYMPTOM_PATH = re.compile(r"symptom|trouble in general|general diagnostic|diagnostic chart|troubleshoot", re.I)


def _stem(w):
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)]
    return w


def symptom_hits(r, text, k=4):
    """Symptom-chart rows matching the user's words in the manual's terms: [(sid, path, page, row text, score)].
    Only chart rows (table rows / list items on pages whose path names a symptom or trouble chart) are searched,
    FSM before CHARM, so the system comes from the chart the row sits in, never from a guess."""
    low, alts = _expand(text)
    words = {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", low + " " + " ".join(alts)) if w not in _STOPWORDS}
    if not words:
        return []
    vf, args = r._veh_filter()
    like = " OR ".join("s.body LIKE ?" for _ in words)
    rows = r.db.execute(f"SELECT s.id, s.path, s.page, s.body, d.kind FROM section s JOIN doc d ON d.id=s.doc_id "
                        f"WHERE ({like}){vf} LIMIT 5000", [f"%{w}%" for w in words] + list(args)).fetchall()
    out = []
    for sid, path, page, body, kind in rows:
        if not SYMPTOM_PATH.search(path.split(" > ")[-1] + " " + path.split(" > ")[1 if " > " in path else 0]):
            continue
        for line in body.splitlines():
            st = line.strip()
            if not st.startswith(("|", "-", "*", "•")) and not re.match(r"^\d+[.)]", st):
                continue
            lw = {_stem(w) for w in re.findall(r"[a-z0-9]{3,}", st.lower())}
            sc = len(words & lw) / len(words)
            if sc >= 0.5:
                out.append((sid, path, page, P._clean(st.replace("|", " "))[:200], sc, kind == "charm"))
    out.sort(key=lambda x: (x[5], -x[4]))
    seen, best = set(), []
    for h in out:
        if h[0] not in seen:
            seen.add(h[0])
            best.append(h[:5])
        if len(best) == k:
            break
    return best


from kb_retriever import STOP as _STOPWORDS  # noqa: E402


# ------------------------------------------------------------------ symptom charts ("Engine Trouble in General")
SYM_HEAD = re.compile(r"^(\d{1,2})\)\s+(.+?[.?])\s*(.*)$")
RANK = re.compile(r"^[ABC]$")


def chart_symptoms(r, sid):
    """[{n, symptom, items: [{text, rank, ref_sid}]}] from a symptom chart: each numbered symptom with its
    suspect items in the manual's order, the likelihood rank the chart prints, and the section an item's own
    <Ref. to ...> points at. Continued sections (same chart, next ids) are read on."""
    rows, cur, out = [], None, []
    sids = [sid] + [x for (x,) in r.db.execute(
        "SELECT id FROM section WHERE id > ? AND id <= ? + 6 AND doc_id=(SELECT doc_id FROM section WHERE id=?) "
        "AND title=(SELECT title FROM section WHERE id=?) ORDER BY id", (sid, sid, sid, sid))]
    refs = {}
    for s_ in sids:
        for ref_text, to_id in r.db.execute("SELECT ref_text, to_id FROM section_ref WHERE from_id=?", (s_,)):
            refs.setdefault(ref_text.strip(), to_id)
        rows += [ln.strip() for ln in (section_row(r, s_) or ("", "", ""))[2].splitlines() if ln.strip().startswith("|")]
    system = ""
    for ln in rows:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if not any(cells) or set(ln) <= set("|-: ") or "Possible cause" in ln or "Problem parts" in ln:
            continue
        rank = cells[-1] if RANK.match(cells[-1] or "") else ""
        body = [c for c in (cells[:-1] if rank else cells) if c]
        if not body:
            continue
        m = SYM_HEAD.match(body[0])
        if m:
            cur = {"n": int(m.group(1)), "symptom": m.group(2).strip(), "items": []}
            out.append(cur)
            body = ([m.group(3)] if m.group(3) else []) + body[1:]
        if cur is None or not body:
            continue
        if len(body) > 1:
            system, part = body[0], " ".join(body[1:])
        else:
            part = body[0]
        text = f"{system}: {part}" if system and not part.startswith(system) else part
        ref = re.search(r"<Ref\. to ([^>]+)>", part)
        ref_sid = None
        if ref:
            ref_sid = next((v for k, v in refs.items() if k and (k in ref.group(1) or ref.group(1) in k)), None)
        cur["items"].append({"text": P._clean(text), "rank": rank, "ref_sid": ref_sid})
    return [x for x in out if x["items"]]


def inspection_for(r, item, car_features=None, k=6):
    """The manual section that inspects a chart item: its own <Ref.> when the chart gives one, else the best
    search hit whose path is an inspection/diagnosis page for this car's engine. None when there is none."""
    words = [w for w in re.findall(r"[a-z0-9]{3,}", item.lower()) if w not in _STOPWORDS
             and w not in ("defective", "loosened", "cracked", "improper", "incorrect", "worn", "damaged", "clogged")]
    if not words:
        return None
    for sid in r.search_ids(" ".join(words) + " inspection", k * 3):
        row = section_row(r, sid)
        if not row:
            continue
        path, body = row[0], row[2]
        if not re.search(r"inspect|inspection|test|diagnos|check", path, re.I):
            continue
        tag = tag_of(path, body)
        if car_features and tag and not fits(tag_features(tag), car_features):
            continue
        if all(w in (path + " " + body[:1500]).lower() for w in words[:3]):
            return sid
    return None
