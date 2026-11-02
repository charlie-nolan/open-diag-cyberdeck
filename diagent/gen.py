"""Test scenarios chosen by script, not by hand (DIAG_AGENT_APP.md "No examples in the agent").

A seeded run picks cars and codes from the kb DBs by DTC family, picks a random branch at each step of the
chosen manual table, writes the reading that takes that branch (from the step's own check), and records the
path and end the harness must reach. Other makes (CHARM pages, no step tables) give page-mode cases, a
symptom-chart page title gives the no-code case, and romid.db gives the ECU-id identification cases.

    python -m diagent gen [--seed N]        # rewrites diagent/scenarios/gen_*.json
"""
import glob
import json
import math
import os
import random
import re
import sqlite3

from . import HERE, ROOT, kb, live, units, watch
from .identify import _engines, _match_model
from kb_retriever import Retriever

FAMILIES = {"misfire": r"P030\d", "o2_af": r"P013\d|P014\d|P015\d|P003\d|P005\d", "egr": r"P040\d",
            "evap": r"P04[4-5]\d", "sensor_circuit": r"P01[0-2]\d|P033\d|P034\d", "fuel_trim": r"P017\d",
            "idle": r"P050\d|P150\d"}
PAGE_MAKES = ["chevrolet", "ford", "honda", "nissan", "dodge", "toyota", "hyundai", "mazda"]
UNIT_TEXT = {"Ω": "ohm", "°C": "C", "N·m": "Nm"}
COMMANDS = ["can't", "skip", "done"]


def _reading(cmp_, want_yes, rng):
    """A reply that takes the wanted branch of a one-value check, in the check's own unit."""
    op, lo, hi, dim, _ = cmp_
    span = abs(hi - lo) if hi is not None else max(abs(lo), 1.0)
    pick = {
        "range": (lambda: rng.uniform(lo, hi)) if want_yes else (lambda: hi + span * rng.uniform(0.2, 1.0) + 0.1),
        ">": (lambda: lo + span * rng.uniform(0.1, 1.0) + 0.01) if want_yes else (lambda: lo - span * rng.uniform(0.1, 0.9) - 0.01),
        ">=": (lambda: lo + span * rng.uniform(0.0, 1.0)) if want_yes else (lambda: lo - span * rng.uniform(0.1, 0.9) - 0.01),
        "<": (lambda: lo - span * rng.uniform(0.1, 0.9) - 0.01) if want_yes else (lambda: lo + span * rng.uniform(0.1, 1.0) + 0.01),
        "<=": (lambda: lo - span * rng.uniform(0.0, 0.9)) if want_yes else (lambda: lo + span * rng.uniform(0.1, 1.0) + 0.01),
    }[op]
    v = round(pick(), 3)
    if dim in ("Ω", "A", "V") and v < 0:
        v = round(max(lo, 0) * rng.uniform(0.0, 0.9), 3)   # these can't be negative: the low branch near zero
    if dim == "Ω" and ((op in (">", ">=") and want_yes) or (op in ("<", "<=") and not want_yes)) and lo >= 1e5:
        return "OL"                         # 'more than 1 MΩ' answered as an open circuit on the meter
    return f"{v:g} {UNIT_TEXT.get(dim, dim)}"


def _walk(steps, codes, rng, cant_p, car=None, cable="KKL"):
    """Simulated walk: (replies, path, expect, live_values, live_steps) for one variant's table. Steps that read
    a value off the scan tool get it from the simulated car when this car + cable can report it."""
    cur, path, replies, seen = min(steps), [], {}, set()
    lv, live_steps = {}, []
    res = _walk_inner(steps, codes, rng, cant_p, car, cable, cur, path, replies, seen, lv, live_steps)
    return res + (lv, live_steps)


def _walk_inner(steps, codes, rng, cant_p, car, cable, cur, path, replies, seen, lv, live_steps):
    while cur not in seen and len(path) < 40:
        seen.add(cur)
        s = steps[cur]
        if s["status"] != "ok":
            return replies, path, {"result": "unconfirmed"}
        ans = kb.scan_answers({cur: s}, codes).get(cur)
        trig = watch.until_trigger(s["step"], (car or {}).get("telemetry_db")) if car else None
        if trig and live.readable(car, cable, trig["key"]):     # the car's value ramps past the manual's mark
            x, unit = trig["value"], watch.T.CANON.get(trig["key"], (None, ""))[1]
            lo, hi = (x - abs(x) * 0.6 - 10, x + abs(x) * 0.1 + 2) if trig["op"] == "above" else \
                (x + abs(x) * 0.6 + 10, x - abs(x) * 0.1 - 2)
            n = rng.randint(6, 14)
            w = lv.setdefault("__watch__", {})
            w[trig["key"]] = {"unit": unit, "values": [round(lo + (hi - lo) * j / (n - 1), 2) for j in range(n)]}
            for k_, v_ in (("rpm", (750, "rpm")), ("speed", (0, "km/h"))):
                w.setdefault(k_, {"unit": v_[1], "values": [v_[0]], "hold": True})
            lv.setdefault("__watch_steps__", []).append(cur)
        if ans is None:
            if rng.random() < cant_p:
                replies[f"step:{cur}"] = rng.choice(["can't", "skip"])
                return replies, path, {"result": "unconfirmed"}
            cmp_ = units.comparison(s["check"])
            ans = rng.choice(["Yes", "No"])
            key = live.param_for(live.wanted(s["step"]), (car or {}).get("telemetry_db")) if cmp_ else None
            if key and live.readable(car, cable, key) and _canon_unit(car, key) == cmp_[3]:
                if key not in lv:
                    got = units.parse_reply(_reading(cmp_, ans == "Yes", rng), "number", cmp_[3])
                    if got["kind"] == "number" and math.isfinite(got["value"]):
                        lv[key] = [got["value"], cmp_[3]]
                if key in lv:
                    v = units.judge(s["check"], lv[key][0], cmp_[3])
                    if v:
                        ans = v[0]
                        live_steps.append(cur)
                        cmp_ = None
                        cell = s["yes"] if ans == "Yes" else s["no"]
                        path.append(cur)
                        nxt = kb.next_of(cell)
                        if nxt is None:
                            return replies, path, {"result": "diagnosis", "end": cell[:40]}
                        if nxt not in steps:
                            return replies, path, {"result": "unconfirmed"}
                        cur = nxt
                        continue
            if cmp_:
                r = _reading(cmp_, ans == "Yes", rng)
                got = units.parse_reply(r, "number", cmp_[3])
                v = units.judge(s["check"], got["value"], got["unit"])
                if v:
                    ans = v[0]                     # the reply is what counts: expect Python's own verdict on it
                    replies[f"step:{cur}"] = ["done", r] if rng.random() < 0.15 else r   # 'done' must be re-asked
                else:
                    replies[f"step:{cur}"] = ans.lower()
            else:
                replies[f"step:{cur}"] = ans.lower()
        cell = s["yes"] if ans == "Yes" else s["no"]
        path.append(cur)
        nxt = kb.next_of(cell)
        if nxt is None:
            return replies, path, {"result": "diagnosis", "end": cell[:40]}
        if nxt not in steps:
            return replies, path, {"result": "unconfirmed"}
        cur = nxt
    return replies, path, {"result": "unconfirmed"}


def _canon_unit(car, key):
    """The unit the deck reports key in (telemetry DB), as units.py names it."""
    try:
        with sqlite3.connect(f"file:{car.get('telemetry_db')}?mode=ro", uri=True) as c:
            row = c.execute("SELECT unit FROM telem_param WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    u = units.norm_unit(row[0]) if row and row[0] else None
    return u[0] if u else None


def _cars(db, fsm_only):
    q = ("SELECT DISTINCT v.model, v.year FROM vehicle v JOIN dtc_procedure d ON d.vehicle_id=v.id "
         "JOIN section s ON s.id=d.section_id JOIN doc ON doc.id=s.doc_id"
         + (" WHERE doc.kind!='charm'" if fsm_only else "") + " ORDER BY 1, 2")
    return db.execute(q).fetchall()


def _given(r, make, model, year, rng):
    engines = _engines(r, model, year)
    eng = rng.choice(engines) if len(engines) > 1 else None
    return ({"make": make, "model": model, "year": year},
            {"Which engine": re.escape(eng) + "$"} if eng else {}, eng)


def step_cases(rng, n_per_family=2):
    path = os.path.join(ROOT, "kb", "subaru.db")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cars = _cars(db, True)
    out = []
    fams = dict(FAMILIES, live_data=r"P\d{4}", watch=r"P\d{4}")   # charts that read / watch a live value
    tdb = live.telemetry_db("Subaru", path)
    for fam, rx in fams.items():
        pool = []
        for model, year in rng.sample(cars, len(cars)):
            engines, scope = kb.engine_choices(Retriever(path), model, year)
            eng = rng.choice(engines) if len(engines) > 1 else None   # the engine first: it scopes the charts
            r = Retriever(path, model=model, year=year)
            r.vehicle_ids = scope.get(eng) if eng else [i for ids in scope.values() for i in ids] or r.vehicle_ids
            # the same scope identify() gives the run (FSM engine-code rows join every engine)
            if not r.vehicle_ids:
                continue
            q = ",".join(map(str, r.vehicle_ids))
            codes = [c for (c,) in db.execute(f"SELECT DISTINCT code FROM dtc_procedure WHERE vehicle_id IN ({q})")
                     if re.fullmatch(rx, c)]
            for code in rng.sample(codes, len(codes)):
                p = kb.procedure(r, code, car_features=kb.engine_features(eng or (engines[0] if len(engines) == 1 else "")),
                                 car={"year": year})
                cands = [v for v in (p or {}).get("variants", []) if v["usable"]]
                good = [v for v in cands if v["usable"] >= 3 and v["steps"][min(v["steps"])]["status"] == "ok"]
                if fam == "live_data":
                    car_ = {"make": "Subaru", "model": model, "year": year, "engine": eng, "telemetry_db": tdb}
                    def _live_ok(st):
                        cmp_ = units.comparison(st["check"])
                        key = live.param_for(live.wanted(st["step"]), tdb) or ""
                        return (st["status"] == "ok" and cmp_ and live.readable(car_, "KKL", key)
                                and _canon_unit(car_, key) == cmp_[3])
                    good = [v for v in good if _live_ok(v["steps"][min(v["steps"])])]
                if fam == "watch":
                    car_ = {"make": "Subaru", "model": model, "year": year, "engine": eng, "telemetry_db": tdb}
                    good = [v for v in good if any(
                        st["status"] == "ok" and n_ <= 2 and (lambda tr: tr and live.readable(car_, "KKL", tr["key"]))(
                            watch.until_trigger(st["step"], tdb)) for n_, st in v["steps"].items())]
                if good:
                    pool.append((model, year, eng, code, p, cands, rng.choice(good)))
                    break
            if len(pool) >= n_per_family:
                break
        for model, year, eng, code, p, cands, v in pool:
            label_ix = next(i for i, c in enumerate(cands) if c["label"] == v["label"])
            v = cands[label_ix]                      # the fake the user picks by label: the first of equal labels
            eng_name = eng or (_engines(Retriever(path), model, year) or [None])[0]
            car = {"make": "Subaru", "model": model, "year": year, "engine": eng_name,
                   "telemetry_db": live.telemetry_db("Subaru", path)}
            replies, walk, exp, lv, live_steps = _walk(v["steps"], [code], rng,
                                                       cant_p=0 if fam in ("live_data", "watch") else 0.12,
                                                       car=car, cable="KKL")
            given = {"make": "Subaru", "model": model, "year": year}
            choose = {"Which engine": "^" + re.escape(eng) + "$"} if eng else {}
            use_field = rng.random() < 0.5
            out.append({"name": f"gen_steps_{fam}_{model}_{year}_{code}".lower(), "family": fam,
                        "field": use_field, "detour": rng.choice(["yes", "no"]),
                        "about": f"script-picked {fam} chart, random branches", "given": given, "choose": choose,
                        "codes": [code], "variant": "^" + re.escape(v["label"]) + "$" if len(cands) > 1 else None,
                        "replies": dict(replies, default="can't"),
                        "fake_car": {"codes": [code], "cable": "KKL", "watch": lv.pop("__watch__", {}),
                                     "live_values": {k: v for k, v in lv.items() if not k.startswith("__")}},
                        "expect": dict(exp, mode="steps", path=walk, rejections_max=0, live_steps=live_steps,
                                       watch_steps=lv.get("__watch_steps__", []))})
    return out


def page_cases(rng, n=8):
    out = []
    makes = [m for m in PAGE_MAKES if os.path.isfile(os.path.join(ROOT, "kb", f"{m}.db"))]
    fams = list(FAMILIES.items())
    for i in range(n):
        make = makes[i % len(makes)]
        fam, rx = fams[(i * 3) % len(fams)]
        path = os.path.join(ROOT, "kb", f"{make}.db")
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = db.execute("SELECT v.model, v.year, d.code FROM dtc_procedure d JOIN vehicle v ON v.id=d.vehicle_id "
                          "WHERE d.code LIKE 'P0%' ORDER BY random() LIMIT 400").fetchall()
        rows = [x for x in rows if re.fullmatch(rx, x[2])] or rows
        if not rows:
            continue
        model, year, code = rng.choice(rows)
        mk = {"chevrolet": "Chevrolet"}.get(make, make.title())
        given, choose, _ = _given(Retriever(path), mk, model, year, rng)
        out.append({"name": f"gen_pages_{make}_{fam}_{model}_{year}_{code}".lower().replace(" ", "_").replace("/", "_"),
                    "family": fam, "about": "script-picked CHARM case (page mode)", "given": given, "choose": choose,
                    "codes": [code], "variant": "none of these", "fake_car": {"codes": [code], "cable": "EX"},
                    "replies": {"page": rng.choice(COMMANDS), "default": "can't"},
                    "expect": {"result": "unconfirmed", "rejections_max": 0}})
    return out


def _chart_symptoms(r, rng, n=60):
    """(sid, symptom) pairs from this car's symptom charts, parsed by kb.chart_symptoms."""
    vf, args = r._veh_filter()
    rows = r.db.execute(f"SELECT s.id, s.path FROM section s JOIN doc d ON d.id=s.doc_id WHERE d.kind='fsm'{vf}",
                        list(args)).fetchall()
    rows = [x for x in rows if kb.SYMPTOM_PATH.search(x[1])]
    out = []
    for sid, _ in rng.sample(rows, min(len(rows), n)):
        out += [(sid, s["symptom"]) for s in kb.chart_symptoms(r, sid) if any(i["ref_sid"] or True for i in s["items"])]
    return out


def symptom_case(rng):
    path = os.path.join(ROOT, "kb", "subaru.db")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cars = _cars(db, True)
    for model, year in rng.sample(cars, min(40, len(cars))):
        r = Retriever(path, model=model, year=year)
        syms = _chart_symptoms(r, rng)
        if syms:
            break
    sym_sid, words = rng.choice(syms)
    given, choose, _ = _given(Retriever(path), "Subaru", model, year, rng)
    choose = dict(choose, **{"The manual's symptom chart": "^" + re.escape(words) + "$"})
    return {"name": f"gen_symptom_{model}_{year}".lower(), "family": "no_code", "about": "symptom from a parsed chart",
            "given": given, "choose": choose, "codes": [], "fake_car": {"codes": []},
            "ident_answers": {"No trouble codes": words},
            "replies": {"page": rng.choice(COMMANDS)},
            "expect": {"result": "unconfirmed", "rejections_max": 0, "mode": "symptom", "symptom": words}}


def ident_cases(rng):
    """ECU id -> romid.db -> kb model and engine (from the ECU list's own trim, never a random pick); the
    car reports no codes, so the user is asked for the symptom; and a car nothing identifies."""
    out = []
    path = os.path.join(ROOT, "kb", "subaru.db")
    r = Retriever(path)
    rom = sqlite3.connect(f"file:{os.path.join(ROOT, 'kb', 'romid.db')}?mode=ro", uri=True)
    rows = rom.execute("SELECT ecuid, year, model, submodel, market FROM rom WHERE year != '' ORDER BY random() "
                       "LIMIT 400").fetchall()
    picked = []
    for ecuid, year, model, sub, market in rows:
        m = _match_model(model, kb.models_for_year(r, int(year)))
        if m and m not in [p[2] for p in picked]:
            picked.append((ecuid, int(year), m, sub or "", market or ""))
        if len(picked) == 3:
            break
    for ecuid, year, m, sub, market in picked:
        engines = _engines(r, m, year)
        want = kb.engine_features(sub, decode=True)
        hits = [e for e in engines if want and kb.fits(kb.engine_features(e), want)]
        choose, eng = {}, None
        if len(engines) == 1:
            eng = engines[0]
        elif len(hits) == 1:
            eng = hits[0]                        # the ECU's trim names it: no question
        elif want and not hits:
            eng = None                           # the kb has no such engine: all sections, nothing asked
        else:
            eng = rng.choice(hits or engines)    # truly ambiguous: the user picks
            choose = {"Which engine": "^" + re.escape(eng) + "$"}
        kb_markets = {x for (x,) in r.db.execute("SELECT DISTINCT upper(market) FROM vehicle WHERE model=? AND year=?",
                                                 (m, year)) if x}
        rr = Retriever(path, model=m, year=year)
        syms = _chart_symptoms(rr, rng, 30)
        sym = rng.choice(syms)[1] if syms else ""
        exp = {"ident": {"make": "Subaru", "year": year, "model": m, "engine": eng}, "rejections_max": 0,
               "any_result": True, "market_note": bool(market and kb_markets and market.upper() not in kb_markets)}
        if sym:
            exp.update(mode="symptom", symptom=sym)
            choose["The manual's symptom chart"] = "^" + re.escape(sym) + "$"
        out.append({"name": f"gen_ident_ecu_{ecuid}".lower(), "family": "ident", "about": "ECU id only, no VIN, no codes",
                    "given": {}, "fake_car": {"ident": {"ecu": ecuid}, "codes": []}, "choose": choose,
                    "ident_answers": {"VIN": "", "No trouble codes": sym}, "codes": [],
                    "replies": {"default": rng.choice(COMMANDS), "page": rng.choice(COMMANDS)}, "expect": exp})
    out.append({"name": "gen_ident_none", "family": "ident", "about": "nothing identifies the car",
                "given": {}, "fake_car": {"ident": {}}, "ident_answers": {"VIN": "", "Make": ""},
                "codes": [], "replies": {}, "expect": {"ident_fail": True}})
    return out


def abuse_case(base, rng):
    """One generated step case plus injected bad calls: each must be rejected, the walk must still finish."""
    sc = json.loads(json.dumps(base))
    num = f"{rng.randint(101, 989) / 10:g}"
    sc["name"] = "gen_harness_" + base["name"][len("gen_steps_"):]
    sc["about"] = "injected uncited value, early diagnosis, uncited instruction, unknown tool"
    sc["inject"] = [{"after": 1, "text": f"Expect {num} V here."},
                    {"after": 2, "call": {"name": "finish", "arguments": {"result": "diagnosis", "summary": "done",
                                                                          "citations": ["M1"]}}},
                    {"after": 3, "call": {"name": "ask_human", "arguments": {"instruction": f"Read it; {num} V is normal.",
                                                                             "expect": "number", "unit": "V",
                                                                             "source_id": "M1"}}},
                    {"after": 4, "call": {"name": "send_command", "arguments": {}}}]
    sc["expect"].pop("rejections_max", None)
    sc["expect"]["rejections_min"] = 4
    return sc


def generate(seed):
    rng = random.Random(seed)
    steps = step_cases(rng)
    walked = [s for s in steps if len(s["expect"]["path"]) >= 2] or steps
    cases = steps + page_cases(rng) + [symptom_case(rng)] + ident_cases(rng) + [abuse_case(rng.choice(walked), rng)]
    for c in cases:
        c["seed"] = seed
        c.setdefault("fake_car", {})
    return cases


def write(seed, out_dir=None):
    out_dir = out_dir or os.path.join(HERE, "scenarios")
    os.makedirs(out_dir, exist_ok=True)
    for f in glob.glob(os.path.join(out_dir, "gen_*.json")):
        os.remove(f)                                   # only the generator's own files
    cases = generate(seed)
    for c in cases:
        with open(os.path.join(out_dir, c["name"] + ".json"), "w") as f:
            json.dump(c, f, indent=1, default=lambda o: None if isinstance(o, float) and math.isinf(o) else o)
    return cases
