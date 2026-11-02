"""Field knowledge as a backup (DIAG_AGENT_APP.md): TSBs/recalls, baked cards, Reddit, forums and NHTSA owner
complaints for this car, counted and car-matched in Python. Read-only, existing DBs only (no scrapers).

Field knowledge may suggest an order or an extra manual test and may add a labeled cause to the board. It
never supplies a spec or a step, never changes a manual branch and never makes a diagnosis on its own."""
import glob
import json
import os
import re
import sqlite3

from . import ROOT

CODE = re.compile(r"\b[PBCU][0-9A-F]{4}\b")


def _ro(path):
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _match(models, years, car):
    """'same model and year' / 'same model' / 'other or unknown model' from a post's tagged models/years."""
    ms = (models or "").lower()
    names = [n.lower() for n in (car.get("model_names") or [car.get("model") or ""]) if n]
    if any(re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", ms) for n in names):
        ys = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", years or "")]
        return "same model and year" if ys and car.get("year") and min(ys) <= car["year"] <= max(ys) else "same model"
    return "other or unknown model"


def _system_text(r, code):
    """The code's own system in this car's manual: its DTC description and title plus its chart's steps
    (kb.procedure). Falls back to the code's linked pages only when there is no chart."""
    if not code:
        return ""
    from . import kb
    row = r.db.execute("SELECT description FROM dtc WHERE code=?", (code,)).fetchone()
    parts = [row[0] if row and row[0] else ""]
    p = kb.procedure(r, code)
    if p and p["variants"]:
        parts.append(p["title"])
        parts += [" ".join((x["step"], x["check"], x["yes"], x["no"])) for v in p["variants"] for x in v["steps"].values()]
    else:
        parts += [b[:3000] for (b,) in r.db.execute(
            "SELECT DISTINCT s.body FROM dtc_procedure d JOIN section s ON s.id=d.section_id WHERE d.code=? LIMIT 20",
            (code,))]
    return " ".join(parts).lower()


GENERIC_PART = set("defective faulty bad other system unit assembly assy replace replaced repair new worn part parts "
                   "and the for with issue problem".split())


def fits_code(cause, system_text, parts=()):
    """True when the cause (or one of its parts), in its own words or the manual's wording for them
    (kb_retriever synonyms, 'a/b' read as either), has every distinctive word in the code's chart/system text."""
    if not system_text:
        return True
    from kb_retriever import _expand, _slash_variants

    def whole(name):
        names = []
        for v in _slash_variants(name.lower()):
            names.append(v)
            names += _expand(v)[1]
        from .kb import ALSO

        def has(w):        # the word, or the manual's wording for it (vocabulary only)
            return any(re.search(rf"(?<![a-z0-9]){re.escape(x)}", system_text) for x in [w] + ALSO.get(w, []))
        for n in names:
            words = [w for w in re.findall(r"[a-z0-9]{2,}", n) if w not in GENERIC_PART]
            if words and all(has(w) for w in words):
                return True
        return False
    return any(whole(x) for x in [cause, *parts] if x)


def reports(r, car, query, make_slug):
    """[{kind, label, count, match, text, refs}] best first: TSB/recall, baked card, forum fixes, Reddit,
    forum threads, owner complaints. Counts are separate reports/threads, never posts quoted as patterns."""
    out = []
    code = (CODE.findall(query.upper()) or [None])[0]
    # 1. TSBs and recalls for this model/year (the retriever's own scope)
    for kind in ("tsb", "recall"):
        ids = r.knowledge_ids(query, k=6, kinds=[kind])
        rows = [r.db.execute("SELECT ref, title, year_min, year_max, model FROM knowledge WHERE id=?", (i,)).fetchone()
                for i in ids]
        rows = [x for x in rows if x and (not code or code in f"{x[1]}" or _body_has(r, x, code))]
        for ref, title, y0, y1, model in rows[:4]:
            out.append({"kind": kind, "label": f"{kind.upper()} {ref or ''}".strip(), "count": 1,
                        "match": f"{model} {y0}-{y1}", "text": (title or "")[:200], "refs": [ref or ""]})
    sys_text = _system_text(r, code)      # the code's own manual pages: a cause must show up there
    # 2. the baked card for this code (forum/TSB fixes with counts)
    if code:
        row = None
        try:
            row = r.db.execute("SELECT text, forum_counts_json, year_min, year_max FROM dtc_card WHERE code=? AND "
                               "upper(model)=upper(?) AND ? BETWEEN year_min AND year_max ORDER BY year_max-year_min "
                               "LIMIT 1", (code, car.get("model") or "", car.get("year") or 0)).fetchone()
        except sqlite3.Error:
            pass
        if row:
            try:
                counts = json.loads(row[1] or "{}")
            except ValueError:
                counts = {}
            for scope, items in counts.items():        # {"this_car": [{cause, confirmed_fixes}], "all_<make>": ...}
                for it in (items or [])[:4]:
                    if isinstance(it, dict) and it.get("cause"):
                        flag = "" if fits_code(it["cause"], sys_text, it.get("parts") or []) else \
                            " (doesn't match the code's system)"
                        out.append({"kind": "baked card", "label": f"{code} card: {it['cause']}{flag}",
                                    "count": int(it.get("confirmed_fixes") or 0),
                                    "match": "this car's model/years" if scope == "this_car" else scope.replace("_", " "),
                                    "text": it["cause"] + " " + " ".join(it.get("parts") or []), "refs": []})
    # 3. forum fixes already extracted into the kb (grouped by the fix, counted by thread)
    fixes = {}
    for scope_all in (False, True):
        for i in r.knowledge_ids(query, k=40, kinds=["forum_fix"], all_models=scope_all):
            k = r.db.execute("SELECT body, url, confirmed, model, year_min FROM knowledge WHERE id=?", (i,)).fetchone()
            if not k or (code and code not in k[0]):
                continue
            m = re.search(r"Fix:[^\n]*\(([^)]+)\)\s*$", k[0], re.M) or re.search(r"Fix:\s*([^\n]+)", k[0])
            fix = re.sub(r"\s+", " ", m.group(1)).strip().lower()[:120] if m else "(no fix stated)"   # the extractor's component
            ent = fixes.setdefault(fix, {"urls": set(), "confirmed": 0, "same": 0})
            if k[1] not in ent["urls"]:
                ent["urls"].add(k[1])
                ent["confirmed"] += int(k[2] or 0)
                ent["same"] += int(not scope_all)
    for fix, e in sorted(fixes.items(), key=lambda x: (-x[1]["same"], -len(x[1]["urls"])))[:5]:
        flag = "" if fits_code(fix, sys_text) else " (doesn't match the code's system)"
        out.append({"kind": "forum fix", "label": fix + flag, "count": len(e["urls"]),
                    "match": f"{e['same']} same model/year, {len(e['urls']) - e['same']} other", "text": fix,
                    "refs": sorted(e["urls"])[:3], "confirmed": e["confirmed"]})
    # 4. Reddit posts and 5. forum threads tagged with the code
    if code:
        rp = os.path.join(ROOT, "raw", "reddit", f"reddit_{make_slug}.db")
        db = _ro(rp) if os.path.isfile(rp) else None
        if db:
            try:
                rows = db.execute("SELECT p.models, p.years, p.title, p.url FROM reddit_post_dtc d JOIN reddit_post p "
                                  "ON p.id=d.post_id WHERE d.dtc=?", (code,)).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                by = {}
                for models, years, title, url in rows:
                    by.setdefault(_match(models, years, car), []).append((title, url))
                for match, items in sorted(by.items()):
                    out.append({"kind": "reddit", "label": f"Reddit posts with {code}", "count": len(items),
                                "match": match, "text": "; ".join(t for t, _ in items[:3])[:300],
                                "refs": [u for _, u in items[:3]]})
        for fp in sorted(glob.glob(os.path.join(ROOT, "raw", "forums", "*.db"))):
            db = _ro(fp)
            if not db:
                continue
            try:
                rows = db.execute("SELECT t.models, t.years, t.title, t.url FROM thread_dtc d JOIN thread t ON "
                                  "t.id=d.thread_id WHERE d.dtc=?", (code,)).fetchall()
            except sqlite3.Error:
                rows = []
            same = [x for x in rows if _match(x[0], x[1], car) != "other or unknown model"]
            if same:
                out.append({"kind": "forum", "label": f"{os.path.basename(fp)[:-3]} threads with {code}",
                            "count": len(same), "match": "same model", "text": "; ".join(x[2] for x in same[:3])[:300],
                            "refs": [x[3] for x in same[:3]]})
    # 6. owner complaints for this model/year that match the query
    ids = r.knowledge_ids(query, k=25, kinds=["nhtsa_complaint"])
    if ids:
        out.append({"kind": "NHTSA complaints", "label": f"owner complaints matching '{query[:40]}'",
                    "count": len(ids), "match": f"{car.get('model')} {car.get('year')}", "text": "", "refs": []})
    return out


def _body_has(r, row, code):
    try:
        return r.db.execute("SELECT 1 FROM knowledge WHERE ref=? AND body LIKE ? LIMIT 1",
                            (row[0], f"%{code}%")).fetchone() is not None
    except sqlite3.Error:
        return False
