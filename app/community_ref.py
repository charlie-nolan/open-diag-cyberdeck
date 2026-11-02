"""
Community knowledge lookup (Reddit + forum threads, built by kb/build_community.py). Standard library + sqlite
only, read-only, runs on the Pi. Works on kb/<make>.db (merged comm_* tables) or kb/community/<make>.community.db.

    from community_ref import community
    r = community("~/subaru_cyberdeck/kb/<make>.db", code="<DTC>", model="<model>", year=<year>)
    r = community(db, text="<symptom in plain words>", model=..., year=..., embed=<QueryEmbedder or None>)

Returns {"threads", "causes", "tests", "chains", "note"}. Every count is separate threads, every item carries
its quote and permalink. Community data ranks below the manual: it suggests, the manual confirms.

Every thread pass 0 kept, unextracted (kb/<make>.threads.db, build_community.py raw + rawembed):
    r = raw_threads("~/subaru_cyberdeck/kb/<make>.db", text="<symptom>", code=None, model=..., year=..., embed=...)
Returns {"threads": [{title, url, sub, score, car_match, excerpt, text}], "note"}: what people wrote, unchecked.
"""
import os
import re
import sqlite3

NOTE = "Community reports (Reddit/forums): they suggest what to test; the manual's procedure confirms."
DTC_RE = re.compile(r"\b[PCBU][0-3][0-9A-F]{3}\b", re.I)


def _open(db):
    if isinstance(db, sqlite3.Connection):
        return db
    p = os.path.expanduser(db)
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)


def _has(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _fts(text):
    words = [w for w in re.findall(r"[A-Za-z0-9]+", text or "") if len(w) > 2][:12]
    return " OR ".join(f'"{w}"' for w in words)


def _match(t_model, t_year, model, year, years_window):
    if not model or not t_model:
        return "other or unknown model"
    if t_model.strip().lower() != model.strip().lower():
        return "other or unknown model"
    if year and t_year and abs(int(t_year) - int(year)) <= years_window:
        return "same model and years"
    return "same model"


def thread_ids(c, code=None, text=None, embed=None, k=60):
    ids = []
    if code:
        ids += [r[0] for r in c.execute("SELECT thread_id FROM comm_code WHERE code=? AND op_had=1", (code.upper(),))]
    if text:
        q = _fts(text)
        if q and _has(c, "comm_doc_fts"):
            ids += [r[0] for r in c.execute(
                "SELECT d.thread_id FROM comm_doc_fts f JOIN comm_doc d ON d.id=f.rowid WHERE comm_doc_fts MATCH ? "
                "ORDER BY bm25(comm_doc_fts) LIMIT ?", (q, k))]
        if embed is not None and _has(c, "comm_doc_vec"):
            try:
                import sqlite_vec
                c.enable_load_extension(True)
                sqlite_vec.load(c)
                ids += [r[0] for r in c.execute(
                    "SELECT d.thread_id FROM comm_doc_vec v JOIN comm_doc d ON d.id=v.id "
                    "WHERE v.embedding MATCH vec_int8(?) AND k=?", (embed(text), k))]
            except Exception:
                pass
    return list(dict.fromkeys(ids))


def community(db, code=None, text=None, model=None, year=None, engine=None, embed=None, years_window=2, top=8):
    c = _open(db)
    if not _has(c, "comm_thread"):
        return {"threads": 0, "causes": [], "tests": [], "chains": [], "note": "no community data for this make"}
    if code is None and text:
        m = DTC_RE.search(text)
        code = m.group(0).upper() if m else None
    ids = thread_ids(c, code, text, embed)
    if not ids:
        return {"threads": 0, "causes": [], "tests": [], "chains": [], "note": NOTE}
    q = ",".join("?" * len(ids))
    th = {r[0]: r[1:] for r in c.execute(
        f"SELECT id, model, year, engine, url, quality FROM comm_thread WHERE id IN ({q})", ids)}
    match = {i: _match(v[0], v[1], model, year, years_window) for i, v in th.items()}
    order = {"same model and years": 0, "same model": 1, "other or unknown model": 2}
    causes = {}
    for tid, key, label, role in c.execute(
            f"SELECT thread_id, cause_key, label, role FROM comm_cause WHERE thread_id IN ({q})", ids):
        e = causes.setdefault(key, {"labels": {}, "threads": set(), "best_match": 2,
                                    **{r: set() for r in ("fix_confirmed", "fix_claimed", "suggested",
                                                          "ruled_out", "tested")}})
        e["labels"][label] = e["labels"].get(label, 0) + 1
        e["threads"].add(tid)
        e[role].add(tid)
        e["best_match"] = min(e["best_match"], order[match[tid]])
    rank = sorted(causes.items(), key=lambda kv: (kv[1]["best_match"], -len(kv[1]["fix_confirmed"]),
                                                   -len(kv[1]["threads"])))
    names = {v: k for k, v in order.items()}
    out_causes = []
    for key, e in rank[:top]:
        out_causes.append({
            "cause": max(e["labels"], key=e["labels"].get), "threads": len(e["threads"]),
            "op_confirmed": len(e["fix_confirmed"]), "fix_claimed": len(e["fix_claimed"]),
            "suggested": len(e["suggested"]), "ruled_out": len(e["ruled_out"]), "tested": len(e["tested"]),
            "car_match": names[e["best_match"]],
            "urls": [th[t][3] for t in sorted(e["threads"], key=lambda t: order[match[t]])[:3] if th[t][3]]})
    tests = []
    for tid, test, tool, comp, res, status, who, quote, rid in c.execute(
            f"SELECT thread_id, test, tool, component, result, status, who, quote, id FROM comm_test "
            f"WHERE thread_id IN ({q})", ids):
        secs = [r[0] for r in c.execute("SELECT section_id FROM comm_link WHERE kind='test' AND row_id=? ORDER BY rank",
                                        (rid,))]
        tests.append({"test": test, "tool": tool, "component": comp, "result": res, "status": status, "who": who,
                      "quote": quote, "url": th[tid][3], "car_match": match[tid], "manual_sections": secs})
    tests.sort(key=lambda t: (order[t["car_match"]], t["status"] != "ran"))
    chains = [{"steps": s, "url": th[tid][3], "car_match": match[tid]} for tid, s in c.execute(
        f"SELECT thread_id, steps FROM comm_chain WHERE thread_id IN ({q})", ids)]
    chains.sort(key=lambda x: order[x["car_match"]])
    return {"threads": len(ids), "causes": out_causes, "tests": tests[:top * 2], "chains": chains[:top], "note": NOTE}


RAW_NOTE = ("Unchecked Reddit/forum threads: what owners wrote, not verified by anyone. Use them for ideas and "
            "wording; the manual's procedure decides.")


def _raw_db(db):
    """kb/<make>.db (or kb/<make>.threads.db itself) -> the threads file next to it, or None."""
    p = os.path.expanduser(db)
    if not p.endswith(".threads.db"):
        p = p[:-3] + ".threads.db" if p.endswith(".db") else p + ".threads.db"
    return p if os.path.isfile(p) else None


def _car_match(tag_models, tag_years, model, year, years_window):
    ms = [m.strip().lower() for m in (tag_models or "").split(",") if m.strip()]
    if not model or not ms or model.strip().lower() not in ms:
        return "other or unknown model"
    ys = [int(y) for y in re.findall(r"\d{4}", tag_years or "")]
    if year and any(abs(y - int(year)) <= years_window for y in ys):
        return "same model and years"
    return "same model"


def _clip(s, n):
    """At most n chars, ending at a sentence end when one is in the last third, else at a word, with '…'."""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    cut = s[:n]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return cut[:end + 1] + " …" if end > n * 2 // 3 else cut.rsplit(" ", 1)[0] + " …"


def raw_threads(db, text=None, code=None, model=None, year=None, embed=None, k=5, years_window=2, excerpt=600):
    """Threads for a question: FTS and (with embed) vectors, merged by rank, then same car first."""
    p = _raw_db(db)
    if not p:
        return {"threads": [], "note": "no thread file for this make"}
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    if code is None and text:
        m = DTC_RE.search(text)
        code = m.group(0).upper() if m else None
    lists = []
    q = " OR ".join(x for x in (f'"{code}"' if code else "", _fts(text)) if x)
    if q:
        lists.append([r[0] for r in c.execute(
            "SELECT rowid FROM raw_thread_fts WHERE raw_thread_fts MATCH ? ORDER BY bm25(raw_thread_fts) LIMIT 60",
            (q,))])
    if embed is not None and text and _has(c, "raw_thread_vec"):
        try:
            import sqlite_vec
            c.enable_load_extension(True)
            sqlite_vec.load(c)
            lists.append([r[0] for r in c.execute(
                "SELECT id FROM raw_thread_vec WHERE embedding MATCH vec_int8(?) AND k=60 ORDER BY distance",
                (embed(text),))])
        except Exception:
            pass
    rrf = {}
    for lst in lists:
        for rank, i in enumerate(lst):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (60 + rank)
    if not rrf:
        return {"threads": [], "note": RAW_NOTE}
    ids = list(rrf)
    qm = ",".join("?" * len(ids))
    order = {"same model and years": 0, "same model": 1, "other or unknown model": 2}
    out = []
    for i, title, url, sub, score, tm, ty, body in c.execute(
            f"SELECT id, title, url, sub, score, tag_models, tag_years, text FROM raw_thread WHERE id IN ({qm})", ids):
        cm = _car_match(tm, ty, model, year, years_window)
        out.append({"id": i, "title": title, "url": url, "sub": sub, "score": score, "car_match": cm,
                    "excerpt": _clip(body, excerpt), "text": body, "_r": (order[cm], -rrf[i])})
    out.sort(key=lambda t: t.pop("_r"))
    return {"threads": out[:k], "note": RAW_NOTE}
