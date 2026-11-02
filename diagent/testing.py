"""Scenario tests: a scripted fake the user answers ask_human from diagent/scenarios/*.json, so whole runs are
checked without anyone at the car. Per scenario: did it follow the manual path, did every claim cite, did it
stop correctly when a step was impossible. One log + one case DB per test run under logs/diagent/."""
import glob
import json
import os
import re
import time

from . import HERE, ROOT, harness, kb
from .car import FakeCar
from .case import Case, Store
from .human import ScriptedHuman
from .identify import identify
from .policy import ScriptedModel
from .tools import SCHEMAS, Tools

WRITE_WORDS = ("clear", "write", "actuat", "flash", "mode04", "security")


def _check(sc, case, store, tools, human):
    exp, fails = sc.get("expect", {}), []
    for k, v in exp.get("ident", {}).items():
        if case.car.get(k) != v:
            fails.append(f"identified {k} {case.car.get(k)!r} != {v!r}")
    p = case.proc or {}
    res = case.result or {}
    if exp.get("result") and res.get("result") != exp["result"]:
        fails.append(f"result {res.get('result')} != {exp['result']}")
    if "path" in exp:
        got = [e["step"] for e in p.get("path", [])]
        if got != exp["path"]:
            fails.append(f"path {got} != {exp['path']}")
    if exp.get("mode") and p.get("mode") != exp["mode"]:
        fails.append(f"mode {p.get('mode')} != {exp['mode']}")
    if exp.get("end") and exp["end"].lower() not in (p.get("end_text") or "").lower():
        fails.append(f"end {p.get('end_text')!r} lacks {exp['end']!r}")
    if exp.get("via") is not None and p.get("via") != exp["via"]:
        fails.append(f"via {p.get('via')} != {exp['via']}")
    rows = store.audit_rows(case.id)
    rejects = [r for r in rows if r[2] == "reject"]
    if len(rejects) < exp.get("rejections_min", 0):
        fails.append(f"{len(rejects)} rejections < {exp['rejections_min']}")
    if exp.get("rejections_max") is not None and len(rejects) > exp["rejections_max"]:
        fails.append(f"{len(rejects)} rejections > {exp['rejections_max']}: " + "; ".join(r[5][:80] for r in rejects))
    if exp.get("notes_contain"):
        notes = " ".join(" ".join(t.get("notes", [])) for t in human.transcript if "notes" in t)
        if exp["notes_contain"].lower() not in notes.lower():
            fails.append(f"no instruction note containing {exp['notes_contain']!r}")
    if exp.get("confirmed_state"):
        if not any(exp["confirmed_state"].lower() in t.get("confirm", "").lower() for t in human.transcript):
            fails.append(f"no safety confirm for {exp['confirmed_state']!r}")
    # accuracy checks (review 2026-10-25): the chart fits the engine, the question was shown, live values were
    # read when the car could give them, DTC steps were answered only from a scan, another path was tried
    if p.get("mode") == "steps" and p.get("variant"):
        v = next((v for v in tools._proc(p["code"])["variants"] if v["key"] == p["variant"]), None)
        if v and v["tag"] and not kb.fits(kb.tag_features(v["tag"]), case.car.get("features") or {}):
            fails.append(f"chart {v['tag']} doesn't fit engine {case.car.get('engine')}")
    for t in human.transcript:
        if "ask" in t and (t["ask"].startswith("Step") or t["expect"] in ("yesno", "number")) and not t.get("question"):
            fails.append(f"no question shown at {t['ask']}")
    live_at = {r["step"] for r in case.readings if str(r["how"]).startswith("read live")}
    if set(exp.get("live_steps", [])) - live_at:
        fails.append(f"live value not read at steps {sorted(set(exp['live_steps']) - live_at)}")
    if any(e["how"].startswith("answered from this case's scan") for e in p.get("path", [])) and not case.scan_ts:
        fails.append("a DTC step was answered without a scan")
    if res.get("result") == "unconfirmed" and any(not u.get("alt") for u in p.get("unconfirmed", [])):
        fails.append("finished unconfirmed without trying another manual path")
    if p.get("detour"):
        fails.append("a detour was left open")
    for d in p.get("detours", []):
        if d["from"] is not None and d["from"] not in [e["step"] for e in p.get("path", [])] + [p.get("current")]:
            fails.append(f"after the detour the tree didn't resume at step {d['from']}")
    if sc.get("field") and sc.get("detour") == "yes" and exp.get("detour_expected") and not p.get("detours"):
        fails.append("accepted detour not recorded")
    if "market_note" in exp and bool(case.car.get("market_note")) != exp["market_note"]:
        fails.append(f"market note {'missing' if exp['market_note'] else 'unexpected'}")
    if exp.get("symptom") and case.symptoms != exp["symptom"]:
        fails.append("no codes, but the symptom wasn't asked")
    if exp.get("mode") == "symptom":
        walked = [r for r in rows if r[3] == "page_steps"]
        if p.get("mode") != "symptom" or not walked:
            fails.append("the symptom chart's suspects weren't walked")
    parts = [v["label"].split(": ", 1)[-1].split(" (")[0].lower() for v in case.sources.values() if v["kind"] == "field"]
    for r in rows:     # a field-report part looked up must be offered (or reported as not offered), never dropped
        q = (json.loads(r[4]).get("query") or "").lower() if r[3] == "search_manual" else ""
        if q and any(pt and pt in q for pt in parts) and not any(x[3] == "offer_detour" for x in rows):
            fails.append("searched for a field-report part but offered no detour")
            break
    if exp.get("watch_steps"):
        from . import watch as W
        wrows = store.db.execute("SELECT id, step, summary FROM watch WHERE case_id=?", (case.id,)).fetchall()
        for n in exp["watch_steps"]:
            hit = [w for w in wrows if w[1] == n]
            if not hit:
                fails.append(f"no watch_live at step {n}")
                continue
            summ = json.loads(hit[0][2])
            if (summ.get("trigger") or {}).get("met_at_s") is None:
                fails.append(f"watch at step {n}: trigger not met")
            stored, again = W.replay(store.db, hit[0][0], case.car)      # offline replay gives the same answer
            if json.dumps(stored, sort_keys=True) != json.dumps(again, sort_keys=True):
                fails.append(f"watch {hit[0][0]} replays differently")
    if exp.get("symptom_sid") and exp["symptom_sid"] not in {v.get("sid") for v in case.sources.values()}:
        fails.append("the symptom's own chart wasn't found")
    # every run: a finished case cites only ids from this run; accepted texts pass the claim check again
    known = case.known_ids()
    if case.status == "finished" and ((not res.get("citations") and known)
                                      or any(x not in known for x in res.get("citations", []))):
        fails.append(f"bad citations {res.get('citations')}")
    if exp.get("any_result"):
        pass
    elif case.status != "finished" and not exp.get("status"):
        fails.append(f"status {case.status}")
    if exp.get("status") and case.status != exp["status"]:
        fails.append(f"status {case.status} != {exp['status']}")
    for r in rows:
        if r[2] == "tool" and r[3] in harness.CHECKED_ARGS:
            args = json.loads(r[4])
            for k in harness.CHECKED_ARGS[r[3]]:
                if harness.check_claims(str(args.get(k) or ""), case, tools, store):
                    fails.append(f"accepted uncited text in {r[3]}.{k}")
    again = store.load(case.id)            # crash-safety: the saved state is the state
    if again.proc.get("path") != p.get("path") or again.status != case.status:
        fails.append("saved case differs from the live one")
    return fails, len(rejects), len(rows)


def prompt_content():
    """Concrete things found in the system prompt or tool descriptions: DTCs, number+unit values, makes and
    model names from the kb DBs. The prompts may only describe the format of a turn."""
    import sqlite3
    import vehicle_id
    texts = [harness.SYSTEM] + [d for _, d, props, _ in SCHEMAS for d in [d] + [x[1] for x in props.values()]]
    text = "\n".join(texts)
    bad = [f"code {c}" for c in set(harness.CODE.findall(text))]
    bad += [f"value '{m.group(0)}'" for m in harness.CLAIM_NUM.finditer(text)]
    words = set(re.findall(r"[a-z][a-z0-9-]+", text.lower()))
    bad += [f"make {m}" for m in vehicle_id.MAKES if m.lower() in words]
    for f in glob.glob(os.path.join(ROOT, "kb", "*.db")):
        if os.path.basename(f).count(".") > 1 or os.path.getsize(f) < 1e6:
            continue                                    # backups (x.pre_*.db) and small side DBs
        try:
            with sqlite3.connect(f"file:{f}?mode=ro", uri=True) as c:
                models = {m.lower() for (m,) in c.execute("SELECT DISTINCT model FROM vehicle") if m and len(m) > 3}
        except sqlite3.Error:
            continue
        bad += [f"model {m}" for m in models & words]
    return sorted(set(bad))


def run_one(sc, store, cfg, agent, log):
    human = ScriptedHuman(sc)
    reader = FakeCar(sc.get("fake_car"))
    car, r, path, notes = identify(cfg, human, reader, store, sc.get("given", {}))   # always first
    log("  car: " + " | ".join(notes))
    if car is None or sc.get("expect", {}).get("ident_fail"):
        ok = car is None and sc.get("expect", {}).get("ident_fail", False)
        return {"name": sc["name"], "ok": ok, "fails": [] if ok else ["identification: " + "; ".join(notes)],
                "status": "not identified" if car is None else "identified", "result": None, "path": [],
                "rejects": 0, "calls": 0, "s": 0, "case": None}
    if agent == "pc" and sc.get("inject"):     # injected bad calls are a scripted-model test only
        sc = dict(sc, expect={k: v for k, v in sc["expect"].items() if k != "rejections_min"})
    case = Case(car=car, codes=list(sc.get("codes", [])), symptoms=sc.get("symptoms", ""))
    store.new(case, label="test:" + sc["name"])
    tools = Tools(case, r, path, human, reader, store, cfg)
    if agent == "pc":
        name = cfg[cfg["agent_role"]]
        url = next((u for u in (cfg["local_url"], cfg["home_url"]) if harness.ollama_has(u, name)), None)
        if not url:
            raise SystemExit(f"{name} not reachable on Ollama")
        model = harness.OllamaChat(url, name, cfg["num_ctx"], cfg["temperature"], cfg["think"])
    else:
        model = ScriptedModel(case, tools, inject=sc.get("inject"), use_field=sc.get("field", False))
    t0 = time.time()
    harness.run(case, tools, model, human, store, cfg, log=log)
    fails, nrej, nrows = _check(sc, case, store, tools, human)
    log(f"  transcript: {json.dumps(human.transcript, ensure_ascii=False)[:3000]}")
    log(f"  result: {json.dumps(case.result, ensure_ascii=False)}")
    return {"name": sc["name"], "ok": not fails, "fails": fails, "status": case.status,
            "result": (case.result or {}).get("result"), "path": [e["step"] for e in case.proc.get("path", [])],
            "rejects": nrej, "calls": nrows, "s": time.time() - t0, "case": case.id}


def run_all(names, agent, cfg):
    files = sorted(glob.glob(os.path.join(HERE, "scenarios", "*.json")))
    scs = [json.load(open(f)) for f in files]
    if names:
        scs = [s for s in scs if any(n in s["name"] for n in names)]
    assert not any(any(w in name for w in WRITE_WORDS) for name, *_ in SCHEMAS), "a tool name looks like a car write"
    bad = prompt_content()
    if bad:
        print("Prompt check FAILED (prompts must hold no car, code, part or reading): " + "; ".join(bad))
        return 4
    if agent == "pc":
        from .__main__ import gpu_busy
        why = gpu_busy()
        if why:   # CLAUDE.md: no Home/Ollama while a GPU build holds the GPU
            print(f"Not running with the PC model: {why}. Try again when tools/status.sh gpu shows it free.")
            return 3
    stamp = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(ROOT, "logs", "diagent"), exist_ok=True)
    logp = os.path.join(ROOT, "logs", "diagent", f"test_{stamp}.log")
    cfg = dict(cfg, case_db=os.path.join(ROOT, "logs", "diagent", f"test_{stamp}.db"))
    store = Store(cfg["case_db"])
    out = []
    with open(logp, "w") as lf:
        def log(s):
            lf.write(s + "\n")
            lf.flush()
        for sc in scs:
            log(f"=== {sc['name']}: {sc.get('about', '')}")
            try:
                res = run_one(sc, store, cfg, agent, log)
            except Exception as e:   # one broken scenario mustn't hide the rest
                import traceback
                log(traceback.format_exc())
                res = {"name": sc["name"], "ok": False, "fails": [f"crash: {e!r}"], "status": "crash", "result": None,
                       "path": [], "rejects": 0, "calls": 0, "s": 0, "case": None}
            log(f"  -> {'PASS' if res['ok'] else 'FAIL'} {res['fails']}")
            out.append(res)
    print(f"{'scenario':34} {'ok':4} {'result':12} {'path':22} rej calls")
    for r in out:
        print(f"{r['name'][:34]:34} {'PASS' if r['ok'] else 'FAIL':4} {str(r['result'])[:12]:12} "
              f"{','.join(map(str, r['path']))[:22]:22} {r['rejects']:3} {r['calls']:4}"
              + ("" if r["ok"] else "  " + "; ".join(r["fails"])[:150]))
    n = sum(r["ok"] for r in out)
    print(f"{n}/{len(out)} pass ({agent}). Log: {logp}")
    return 0 if n == len(out) else 1
