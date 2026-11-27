"""
Community knowledge lookup (Reddit + forum + video threads, built by kb/build_community.py). Standard library + sqlite
only, read-only, runs on the Pi. Works on kb/<make>.db (merged comm_* tables) or kb/community/<make>.community.db.

    from community_ref import community
    r = community("kb/<make>.db", code="<DTC>", model="<model>", year=<year>)
    r = community(db, text="<symptom in plain words>", model=..., year=..., embed=<QueryEmbedder or None>)

Returns {"threads", "causes", "tests", "chains", "videos", "note"}. Every count is separate threads, every item carries
its quote and permalink. Community data ranks below the manual: it suggests, the manual confirms.

Every thread pass 0 kept, unextracted (kb/<make>.threads.db, build_community.py raw + rawembed):
    r = raw_threads("kb/<make>.db", text="<symptom>", code=None, model=..., year=..., embed=...)
Returns {"threads": [{title, url, sub, score, car_match, excerpt, text}], "note"}: what people wrote, unchecked.
"""
import os
import re
import sqlite3
import threading

NOTE = "Community reports (Reddit/forums): they suggest what to test; the manual's procedure confirms."
DTC_RE = re.compile(r"\b[PCBU][0-3][0-9A-F]{3}\b", re.I)

_CONN_CACHE = {}
_CONN_LOCK = threading.Lock()
_VEC_LOADED = set()
_ANN_READY = {}


def _close_conn(c):
    cid = id(c)
    _VEC_LOADED.discard(cid)
    _ANN_READY.pop(cid, None)
    try:
        c.close()
    except sqlite3.Error:
        pass


def _tune_ro(c):
    try:
        c.execute("PRAGMA cache_size=-16384")
        c.execute("PRAGMA mmap_size=67108864")
        c.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.Error:
        pass


def _open(db):
    if isinstance(db, sqlite3.Connection):
        return db, None
    p = os.path.abspath(os.path.expanduser(db))
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        mtime = 0
    with _CONN_LOCK:
        hit = _CONN_CACHE.get(p)
        if hit and hit[0] == mtime:
            return hit[1], p
        if hit:
            _close_conn(hit[1])
            _CONN_CACHE.pop(p, None)
        if len(_CONN_CACHE) >= 4:
            old_k = next(iter(_CONN_CACHE))
            _close_conn(_CONN_CACHE.pop(old_k)[1])
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        _tune_ro(c)
        _CONN_CACHE[p] = (mtime, c)
        return c, p


def _ensure_vec(c, db_path=None):
    cid = id(c)
    if cid not in _VEC_LOADED:
        import sqlite_vec
        c.enable_load_extension(True)
        sqlite_vec.load(c)
        c.enable_load_extension(False)
        _VEC_LOADED.add(cid)
    if cid not in _ANN_READY:
        _ANN_READY[cid] = False
        if not db_path:
            try:
                rows = c.execute("PRAGMA database_list").fetchall()
                db_path = next((r[2] for r in rows if r[1] == "main" and r[2]), None)
            except sqlite3.Error:
                db_path = None
        if db_path:
            ann_p = os.path.splitext(db_path)[0] + ".ann.db"
            if os.path.isfile(ann_p):
                try:
                    c.execute("ATTACH DATABASE ? AS comm_ann", (f"file:{ann_p}?mode=ro",))
                    if c.execute("SELECT 1 FROM comm_ann.sqlite_master WHERE name='comm_doc_vec_bin'").fetchone():
                        n_bin = c.execute("SELECT count(*) FROM comm_ann.comm_doc_vec_bin_rowids").fetchone()[0]
                        n = c.execute("SELECT count(*) FROM comm_doc_vec_rowids").fetchone()[0]
                        _ANN_READY[cid] = (n_bin == n)
                except sqlite3.Error:
                    pass
    return _ANN_READY[cid]


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


def _vec_thread_ids(c, text, embed, k, db_path=None):
    use_ann = _ensure_vec(c, db_path)
    qb = embed(text)
    if use_ann:
        import json
        import numpy as np
        cand = [r[0] for r in c.execute(
            "SELECT id FROM comm_ann.comm_doc_vec_bin WHERE embedding MATCH vec_quantize_binary(vec_int8(?)) AND k=?",
            (qb, k * 10))]
        if not cand:
            return []
        rows = c.execute(
            "SELECT v.id, v.embedding, d.thread_id FROM comm_doc_vec v JOIN comm_doc d ON d.id=v.id "
            "WHERE v.id IN (SELECT value FROM json_each(?))", (json.dumps(cand),)).fetchall()
        if not rows:
            return []
        m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.int8).reshape(len(rows), -1).astype(np.int32)
        sims = m @ np.frombuffer(qb, dtype=np.int8).astype(np.int32)
        return [rows[i][2] for i in np.argsort(-sims)[:k]]
    return [r[0] for r in c.execute(
        "SELECT d.thread_id FROM comm_doc_vec v JOIN comm_doc d ON d.id=v.id "
        "WHERE v.embedding MATCH vec_int8(?) AND k=?", (qb, k))]


def thread_ids(c, code=None, text=None, embed=None, k=60, db_path=None):
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
                ids += _vec_thread_ids(c, text, embed, k, db_path=db_path)
            except Exception:
                pass
    return list(dict.fromkeys(ids))


def _yt_jump(url, t_sec):
    """Append &t=<seconds> to a YouTube watch URL when a segment timestamp is known."""
    if not url or t_sec is None or t_sec <= 0 or "youtube.com/watch" not in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={int(t_sec)}"


def community(db, code=None, text=None, model=None, year=None, engine=None, embed=None, years_window=2, top=8):
    c, db_path = _open(db)
    if not _has(c, "comm_thread"):
        return {"threads": 0, "causes": [], "tests": [], "chains": [], "videos": [], "note": "no community data for this make"}
    if code is None and text:
        m = DTC_RE.search(text)
        code = m.group(0).upper() if m else None
    ids = thread_ids(c, code, text, embed, db_path=db_path)
    if not ids:
        return {"threads": 0, "causes": [], "tests": [], "chains": [], "videos": [], "note": NOTE}
    q = ",".join("?" * len(ids))
    th = {r[0]: r[1:] for r in c.execute(
        f"SELECT id, model, year, engine, url, quality, src, sub, title FROM comm_thread WHERE id IN ({q})", ids)}
    seg_times = {}
    if _has(c, "comm_seg_time"):
        for tid, seg, ts in c.execute(f"SELECT thread_id, seg, t_sec FROM comm_seg_time WHERE thread_id IN ({q})", ids):
            seg_times[(tid, seg)] = ts
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
    out_seg = {}
    videos = []
    for tid, fix, comp, seg, quote in c.execute(
            f"SELECT thread_id, fix, component, seg, quote FROM comm_outcome WHERE thread_id IN ({q})", ids):
        out_seg[tid] = seg
        info = th.get(tid)
        if info and info[3] and "youtube.com/watch" in info[3]:
            ts = seg_times.get((tid, seg))
            videos.append({
                "title": info[7] or "", "channel": info[6] or "",
                "url": _yt_jump(info[3], ts), "t_sec": ts or 0,
                "fix": fix or comp or "", "quote": quote or "", "car_match": match[tid],
            })
    videos.sort(key=lambda v: order[v["car_match"]])
    out_causes = []
    for key, e in rank[:top]:
        out_causes.append({
            "cause": max(e["labels"], key=e["labels"].get), "threads": len(e["threads"]),
            "op_confirmed": len(e["fix_confirmed"]), "fix_claimed": len(e["fix_claimed"]),
            "suggested": len(e["suggested"]), "ruled_out": len(e["ruled_out"]), "tested": len(e["tested"]),
            "car_match": names[e["best_match"]],
            "urls": [_yt_jump(th[t][3], seg_times.get((t, out_seg.get(t))))
                     for t in sorted(e["threads"], key=lambda t: order[match[t]])[:3] if th[t][3]]})
    tests = []
    for tid, test, tool, comp, res, status, who, quote, rid, seg in c.execute(
            f"SELECT thread_id, test, tool, component, result, status, who, quote, id, seg FROM comm_test "
            f"WHERE thread_id IN ({q})", ids):
        secs = [r[0] for r in c.execute("SELECT section_id FROM comm_link WHERE kind='test' AND row_id=? ORDER BY rank",
                                        (rid,))]
        ts = seg_times.get((tid, seg))
        tests.append({"test": test, "tool": tool, "component": comp, "result": res, "status": status, "who": who,
                      "quote": quote, "url": _yt_jump(th[tid][3], ts), "t_sec": ts,
                      "car_match": match[tid], "manual_sections": secs})
    tests.sort(key=lambda t: (order[t["car_match"]], t["status"] != "ran"))
    chains = [{"steps": s, "url": _yt_jump(th[tid][3], seg_times.get((tid, out_seg.get(tid)))),
               "car_match": match[tid]} for tid, s in c.execute(
        f"SELECT thread_id, steps FROM comm_chain WHERE thread_id IN ({q})", ids)]
    chains.sort(key=lambda x: order[x["car_match"]])
    return {"threads": len(ids), "causes": out_causes, "tests": tests[:top * 2], "chains": chains[:top],
            "videos": videos[:top], "note": NOTE}


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
    _tune_ro(c)
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
