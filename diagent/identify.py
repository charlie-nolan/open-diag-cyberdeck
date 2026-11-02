"""Car identification, before anything else (DIAG_AGENT_APP.md "Car identification first").

The same path as the main app, through app/vehicle_id.py (the app's own flow lives in its PyQt Intake tab, so
it can't be imported here; this calls the same functions in the same order):
  1. VIN from OBD-II mode 09, or typed by the user.
  2. Subaru ECU id from the SSM2 init reply -> kb/romid.db; a VIN typed once is bound to that ECU id.
  3. Decode: cache (kb/vin.db, NHTSA cache) -> local vPIC (PostgreSQL) -> NHTSA online (vehicle_id.nhtsa_decode).
  4. Load kb/<make>.db (vehicle_id.kb_path_for, Ram -> dodge.db), scoped to the model/year/engine the DB
     uses, plus the telemetry tables, the ECU's capability list and earlier visits for this VIN.
Nothing is defaulted: what can't be worked out is asked, and with no answer the case doesn't start."""
import json
import os
import re
import sqlite3

import vehicle_id as V

from . import ROOT, kb, live


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ecu_vins(cfg):
    """{ECU id: VIN}: the main app's bindings (read only) plus diagent's own (its own wins)."""
    out = {}
    for p in cfg["ecu_vin_files"]:
        out.update(_load(p))
    return out


def bind_ecu_vin(cfg, ecu, vin):
    path = cfg["ecu_vin_files"][-1]          # diagent's own file; the app's is never written from here
    known = _load(path)
    if known.get(ecu) != vin:
        known[ecu] = vin
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(known, f, indent=1)


def _match_model(name, models):
    """The kb's own model name for a decoded one: exact, then either one a prefix of the other."""
    low = (name or "").strip().lower()
    if not low:
        return None
    for m in models:
        if m.lower() == low:
            return m
    hits = [m for m in models if low.startswith(m.lower()) or m.lower().startswith(low)]
    return max(hits, key=len) if hits else None


def _engines(r, model, year):
    """One engine list per car: kb.engine_choices merges the FSM's code names into the descriptions."""
    return kb.engine_choices(r, model, year)[0]


def _match_engine(engines, vin, decoded):
    """One of the kb's engine strings from the VIN's engine code or the decoded description, else None."""
    if len(engines) == 1:
        return engines[0]
    want = kb.engine_features(decoded or "", decode=True)
    if want:
        hits = [e for e in engines if kb.fits(kb.engine_features(e), want)
                and all(kb.engine_features(e).get(k) == want[k] for k in ("disp",) if k in want)]
        if len(hits) == 1:
            return hits[0]
    if vin and len(vin) == 17:     # CHARM engine names carry the VIN engine code ('... VIN T')
        hits = [e for e in engines if re.search(rf"\bVIN {re.escape(vin[7])}\b", e)]
        if len(hits) == 1:
            return hits[0]
    disp = re.search(r"(\d\.\d)\s*L", decoded or "")
    if disp:
        hits = [e for e in engines if disp.group(1) in e]
        if len(hits) == 1:
            return hits[0]
    return None


def _visits(cfg, store, vin, ecu):
    """Earlier cases for this car: diagent's own (by VIN or ECU id) and the main app's visit table (read only)."""
    out = []
    for cid, label, status, state in store.db.execute("SELECT id, label, status, state FROM case_ ORDER BY id DESC"):
        try:
            car = json.loads(state).get("car", {})
        except ValueError:
            continue
        if (vin and car.get("vin") == vin) or (ecu and car.get("ecu") == ecu):
            out.append(f"diagent case {cid} ({status}): {label}")
    app_db = cfg.get("app_session_db")
    if vin and app_db and os.path.isfile(app_db):
        try:
            with sqlite3.connect(f"file:{app_db}?mode=ro", uri=True) as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='visit'").fetchone():
                    out += [f"app visit {i} {started}: {veh}" for i, started, veh in c.execute(
                        "SELECT id, started, vehicle FROM visit WHERE vin=? ORDER BY id DESC LIMIT 10", (vin,))]
        except sqlite3.Error:
            pass
    return out


def _telemetry(r, car):
    """What live data this car can give, from the kb's own tables (not a fixed list)."""
    out = []
    try:
        if r.db.execute("SELECT 1 FROM sqlite_master WHERE name='ssm_param'").fetchone():
            n = r.db.execute("SELECT count(*) FROM ssm_param WHERE ecu_id IN ('', ?)", (car.get("ecu") or "",)).fetchone()[0]
            if n:
                out.append(f"SSM2 parameters in the kb: {n}" + (f" (incl. ECU {car['ecu']})" if car.get("ecu") else ""))
    except sqlite3.Error:
        pass
    gm = os.path.join(ROOT, "kb", "gm_mode22_pids.json")
    if os.path.isfile(gm) and car.get("make") in ("Chevrolet", "GMC", "Buick", "Cadillac", "Pontiac", "Oldsmobile",
                                                   "Saturn", "Hummer"):
        out.append("GM mode 22 PIDs: kb/gm_mode22_pids.json")
    out.append("generic OBD-II mode 01")
    return out


def _caps(init_hex_path):
    """The ECU's own capability list from a saved SSM2 init reply (ssm2_capabilities.ECUCaps), or []."""
    try:
        with open(init_hex_path) as f:
            raw = bytes.fromhex(f.read().strip())
        from ssm2_capabilities import ECUCaps
        caps = ECUCaps(raw)
        return [f"engine {caps.engine}", f"measuring blocks {len(caps.measuring_blocks())}",
                f"switches {len(caps.switches())}", f"test mode {'yes' if caps.has_test_mode else 'no'}",
                f"actuator tests {'yes' if caps.has_actuator_tests else 'no'}", f"VIN stored {'yes' if caps.has_vin else 'no'}"]
    except Exception:
        return []


def identify(cfg, human, reader, store, given):
    """(car dict, Retriever, kb path, notes) or (None, None, None, notes) when the car can't be worked out.
    given: what the user typed on the command line (vin/make/model/year/engine), any of them empty."""
    notes = []
    car = {k: given.get(k) for k in ("vin", "make", "model", "year", "engine")}
    car["vin"] = V.clean_vin(car.get("vin") or "") or None
    from_car = reader.identify() if hasattr(reader, "identify") else {}
    for n in from_car.get("notes", []):
        notes.append(n)
    if from_car.get("vin") and not car["vin"]:
        car["vin"] = V.clean_vin(from_car["vin"])
        notes.append(f"VIN from the car (mode 09): {car['vin']}")
    ecu = from_car.get("ecu")
    car["ecu"] = ecu
    if ecu:
        known = ecu_vins(cfg).get(ecu)
        if known and not car["vin"]:
            car["vin"] = known
            notes.append(f"ECU {ecu} is bound to VIN {known} from an earlier visit")
    if not car["vin"] and (ecu or not car.get("make")):
        typed = human.ask_text("VIN (windshield or door jamb), or leave empty if you can't read it")
        if typed and typed.strip():
            car["vin"] = V.clean_vin(typed)
    decoded = {}
    if car["vin"]:
        d = V.decode_vin(car["vin"], cfg.get("vin_db"), cfg["nhtsa_cache"])
        if d.get("note"):
            notes.append(f"VIN: {d['note']}")
        if d.get("make"):
            decoded = d
            if not d.get("engine") or not d.get("model"):
                nh = V.nhtsa_decode(d["vin"], cfg["nhtsa_cache"])     # local vPIC, then NHTSA online; cached
                if nh:
                    decoded.update({k: v for k, v in nh.items() if v})
            notes.append("VIN decoded: " + " ".join(str(decoded.get(k) or "") for k in ("year", "make", "model", "engine",
                                                                                         "trans", "drive")).strip())
            if ecu:
                bind_ecu_vin(cfg, ecu, d["vin"])
                notes.append(f"VIN bound to ECU {ecu} for later scans")
    if ecu and not decoded:
        rom = V.car_from_rom(ecu, cfg.get("rom_db"))
        if rom:
            decoded = {"make": "Subaru", "year": rom["year"], "model": rom["model"],
                       "trans": rom.get("transmission", ""), "engine": rom.get("submodel", ""),
                       "market": rom.get("market", "")}
            notes.append(f"ECU {ecu}: built for a " + " ".join(str(x) for x in (rom["year"], "Subaru", rom["model"],
                                                                                rom["submodel"], rom["transmission"],
                                                                                rom["market"]) if x)
                         + " (RomRaider ECU list)")
        elif from_car.get("ssm"):
            decoded = {"make": V.make_from_ssm(ecu)}
            notes.append(f"ECU {ecu} answered SSM2: a Subaru; this ECU id isn't in romid.db")
    for k in ("make", "year", "model"):         # what was typed wins; a disagreement is asked, not guessed
        if decoded.get(k) and car.get(k) and str(decoded[k]).lower() != str(car[k]).lower():
            i = human.choose(f"The {k} you typed ({car[k]}) differs from the car's ({decoded[k]}). Which is right?",
                             [str(car[k]), str(decoded[k])])
            car[k] = [car[k], decoded[k]][i]
        elif decoded.get(k) and not car.get(k):
            car[k] = decoded[k]
    car["trans"], car["drive"] = decoded.get("trans", ""), decoded.get("drive", "")
    if not car.get("make"):
        car["make"] = V.canon_make(human.ask_text("Make (the car couldn't tell)") or "") or None
    if not car.get("make"):
        notes.append("Car not identified: no VIN, no ECU id, no make given. Nothing loaded.")
        return None, None, None, notes
    car["make"] = V.canon_make(car["make"])
    path, ok = V.kb_path_for(car["make"], cfg["kb_dir"])
    if not ok:
        notes.append(f"No knowledge DB for {car['make']} ({os.path.basename(path)} not in {cfg['kb_dir']}).")
        return None, None, None, notes
    r, _ = kb.open_kb(car["make"], kb_dir=cfg["kb_dir"])
    if not car.get("year"):
        y = human.ask_text("Model year")
        car["year"] = int(y) if y and y.strip().isdigit() else None
    if not car.get("year"):
        notes.append("Car not identified: model year unknown. Nothing loaded.")
        return None, None, None, notes
    car["year"] = int(car["year"])
    models = kb.models_for_year(r, car["year"])
    m = _match_model(car.get("model"), models)
    if not m:
        if not models:
            notes.append(f"{os.path.basename(path)} has no {car['year']} models.")
            return None, None, None, notes
        q = (f"'{car['model']}' isn't a model name in {os.path.basename(path)} for {car['year']}. Which one is it?"
             if car.get("model") else f"Which {car['year']} {car['make']} model?")
        i = human.choose(q, models + ["not listed"])
        if i >= len(models):
            notes.append("Model not in the kb for that year. Nothing loaded.")
            return None, None, None, notes
        m = models[i]
    car["model_names"] = list(dict.fromkeys(x for x in (m, car.get("model"), decoded.get("model")) if x))
    if car.get("model") and m != car["model"]:
        notes.append(f"Model '{car['model']}' is '{m}' in {os.path.basename(path)}")
    car["model"] = m
    engines = _engines(r, m, car["year"])
    said = " ".join(str(x) for x in (car.get("engine"), decoded.get("engine"), decoded.get("trim")) if x)
    eng = car.get("engine") if car.get("engine") in engines else _match_engine(engines, car.get("vin"), said)
    want = kb.engine_features(said, decode=True)
    if not eng and want and len(engines) > 1 and not any(
            kb.fits(kb.engine_features(e), want) and kb.engine_features(e).get("disp", want.get("disp")) == want.get("disp")
            for e in engines):
        notes.append(f"The car's engine ({said}) isn't among {os.path.basename(path)}'s engines for {car['year']} {m} "
                     f"({', '.join(engines)}). All engine sections are loaded; charts are filtered by the car's engine.")
        engines = []                               # nothing to ask: none of them is this engine
    elif not eng and len(engines) > 1:
        i = human.choose(f"Which engine? ({os.path.basename(path)} has {len(engines)} for this car)",
                         engines + ["not sure (use all of them)"])
        eng = engines[i] if i < len(engines) else None
    car["engine"] = eng
    choices, scope = kb.engine_choices(r, m, car["year"])
    car["vehicle_ids"] = scope.get(eng) if eng else [i for ids in scope.values() for i in ids]
    feats = kb.engine_features(eng or "")
    for k, v in kb.engine_features(said, decode=True).items():
        feats.setdefault(k, v)                     # the VIN/ECU decode fills what the kb's name doesn't say
    if feats.get("disp") and "cyl" not in feats:    # same model, same displacement in other years of this DB
        cyls = {kb.engine_features(e).get("cyl") for (e,) in r.db.execute(
            "SELECT DISTINCT engine FROM vehicle WHERE upper(model)=upper(?)", (m,))
            if kb.engine_features(e or "").get("disp") == feats["disp"]} - {None}
        if len(cyls) == 1:
            feats["cyl"] = cyls.pop()
    car["features"] = feats
    car["spec"] = sorted(kb.spec_tokens(said))
    mk = decoded.get("market")
    kb_markets = {x for (x,) in r.db.execute("SELECT DISTINCT upper(market) FROM vehicle WHERE upper(model)=upper(?) "
                                             "AND year=?", (m, car["year"])) if x}
    if mk and kb_markets and mk.upper() not in kb_markets:
        car["market_note"] = (f"The ECU is for the {mk} market; the manuals loaded are for {', '.join(sorted(kb_markets))}. "
                              "Specs and charts may differ for this market.")
        notes.append(car["market_note"])
    r, path = kb.open_car(car, cfg["kb_dir"])
    car["kb"] = path
    car["telemetry_db"] = live.telemetry_db(car["make"], path)
    car["cable"] = from_car.get("cable") or ("KKL" if ecu else "EX")
    car["telemetry"] = _telemetry(r, car)
    car["caps"] = _caps(from_car["init_hex"]) if from_car.get("init_hex") else []
    car["history"] = _visits(cfg, store, car.get("vin"), ecu)
    notes.append(f"Loaded {os.path.basename(path)}: {car['year']} {m}" + (f", {eng}" if eng else ", all engines"))
    return car, r, path, notes
