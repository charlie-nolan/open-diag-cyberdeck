"""Handoff step 4: replay saved cases through Home (local baseline) and Team, with web on and off, and write the report.

    venv/bin/python team/replay.py cases            # write team/replay/cases/*.json (see below)
    venv/bin/python team/replay.py run [--only NAME] [--web on|off|both]   # one case at a time, logs/team/replay/
    venv/bin/python team/replay.py report           # logs/team/replay/REPORT.md + grade sheet

Cases: the diagent generator's script-picked cars and codes across makes (no hand-picking, rule 2), plus the
deck's last saved case on this PC (session_active.db prev_case). Real deck visits (records/<VIN>.db) live on the
deck; copy their case_json here to add them (any VIN is dropped by run.py before anything leaves the PC).
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CASES = os.path.join(HERE, "replay", "cases")
OUT = os.environ.get("TEAM_REPLAY_OUT") or os.path.join(ROOT, "logs", "team", "replay")
PY = os.path.join(ROOT, "venv", "bin", "python")
# one per make outside Subaru, then Subaru spread over models, years and code families
PICK = ["gen_pages_chevrolet_misfire_express_1999_p0306", "gen_pages_dodge_fuel_trim_charger_2006_p0174",
        "gen_pages_ford_evap_escape_2010_p0453", "gen_pages_honda_idle_passport_2001_p0502",
        "gen_pages_hyundai_sensor_circuit_sonata_2008_p0119", "gen_pages_mazda_misfire_626_2000_p0302",
        "gen_pages_nissan_egr_sentra_1997_p0400", "gen_pages_toyota_o2_af_sienna_2011_p0031",
        "gen_steps_sensor_circuit_legacy_2008_p0335", "gen_steps_evap_outback_2009_p0453",
        "gen_steps_live_data_forester_1999_p0107"]


def make_cases():
    os.makedirs(CASES, exist_ok=True)
    n = 0
    for name in PICK:
        d = json.load(open(os.path.join(ROOT, "diagent", "scenarios", name + ".json")))
        g = d["given"]
        eng = next(iter(d.get("choose", {}).values()), "")
        eng = re.sub(r"\\(.)", r"\1", eng).strip("^$") if eng else ""
        case = {"vehicle_info": {"make": g["make"], "model": g["model"], "year": g["year"],
                                 **({"engine": eng} if eng else {})},
                "dtcs": ", ".join(d["codes"]), "source": f"diagent scenario {name} (script-picked)"}
        json.dump(case, open(os.path.join(CASES, name.replace("gen_pages_", "").replace("gen_steps_", "") + ".json"),
                             "w"), indent=1)
        n += 1
    db = os.path.join(ROOT, "session_active.db")
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        prev = json.loads(c.execute("SELECT value FROM session_state WHERE key='prev_case'").fetchone()[0])
        veh = json.loads((c.execute("SELECT value FROM session_state WHERE key='vehicle_info'").fetchone()
                          or ["{}"])[0])
    except Exception:
        prev, veh = None, {}
    if prev and prev.get("dtcs"):
        keep = {k: v for k, v in prev.items() if v and k not in ("final_verdict", "visit_id", "drive_id")}
        if not veh.get("make"):     # no car saved with it: the case file needs its vehicle_info filled by hand
            out = os.path.join(CASES, "saved_prev_case.json")
            if os.path.isfile(out) and json.load(open(out)).get("vehicle_info", {}).get("make"):
                print("saved_prev_case.json kept (vehicle filled by hand)")
                print(f"{n + 1} cases in {CASES}")
                return
            print("prev_case has no vehicle saved: fill vehicle_info in saved_prev_case.json by hand")
        keep["vehicle_info"] = {k: veh.get(k) for k in ("make", "model", "year", "engine") if veh.get(k)}
        keep["source"] = "deck's last saved case on this PC (prev_case)"
        json.dump(keep, open(os.path.join(CASES, "saved_prev_case.json"), "w"), indent=1)
        n += 1
    print(f"{n} cases in {CASES}")


def run(only=None, web="both"):
    os.makedirs(OUT, exist_ok=True)
    for path in sorted(glob.glob(os.path.join(CASES, "*.json"))):
        name = os.path.basename(path)[:-5]
        if only and only not in name:
            continue
        for w in (("on", "off") if web == "both" else (web,)):
            out = os.path.join(OUT, f"{name}__web_{w}")
            if os.path.isfile(os.path.join(out, "report.json")):
                print(f"skip {name} web {w} (done)")
                continue
            # the local baseline runs once per case (it never uses the web)
            args = [PY, os.path.join(HERE, "run.py"), "--case", path, "--web", w, "--out", out]
            if w == "off" and os.path.isfile(os.path.join(OUT, f"{name}__web_on", "baseline.txt")):
                args.append("--no-baseline")
            print(f"== {name} web {w}", flush=True)
            subprocess.run(args)


def baselines():
    """Home's verdict for every case whose runs skipped it (GPU was booked), into the web-on run's folder."""
    for path in sorted(glob.glob(os.path.join(CASES, "*.json"))):
        name = os.path.basename(path)[:-5]
        dirs = [d for d in glob.glob(os.path.join(OUT, f"{name}__web_*")) if os.path.isfile(os.path.join(d, "report.json"))]
        if not dirs or any(os.path.getsize(os.path.join(d, "baseline.txt")) for d in dirs
                           if os.path.isfile(os.path.join(d, "baseline.txt"))):
            continue
        d = next((x for x in dirs if x.endswith("__web_on")), dirs[0])
        print(f"== baseline {name}", flush=True)
        subprocess.run([PY, os.path.join(HERE, "run.py"), "--case", path, "--baseline-only", "--out", d])


def _short(text, n=180):
    t = " ".join((text or "").split())
    return t[:n] + ("..." if len(t) > n else "")


def report():
    rows, by_case = [], {}
    for rp in sorted(glob.glob(os.path.join(OUT, "*", "report.json"))):
        d = json.load(open(rp))
        run_dir = os.path.dirname(rp)
        name, w = os.path.basename(run_dir).rsplit("__web_", 1)
        u = d.get("usage", {})
        agy = {m: u.get(m, {}) for m in ("gemini", "gptoss")}
        r = {"case": name, "web": w, "seconds": d["seconds"], "failed": d.get("members_failed") or [],
             "baseline_skipped": d.get("baseline_skipped"), "lookups": d.get("lookups"),
             "team_flags": (d.get("verdict") or {}).get("source_check"),
             "team_bad_ids": len((d.get("verdict") or {}).get("bad_ids") or []),
             "team_issues": (d.get("verdict") or {}).get("issues"),
             "home_flags": (d.get("baseline") or {}).get("source_check"),
             "home_issues": (d.get("baseline") or {}).get("issues"),
             "agy_calls": sum(a.get("calls", 0) for a in agy.values()),
             "agy_tokens_k": round(sum(a.get("input_tokens", 0) + a.get("output_tokens", 0) + a.get("thinking_tokens", 0)
                                       for a in agy.values()) / 1000, 1),
             "agy_cache_k": round(sum(a.get("cache_read_tokens", 0) for a in agy.values()) / 1000, 1),
             "claude_tokens_k": round(sum(u.get("claude", {}).get(k, 0) for k in
                                          ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
                                      / 1000, 1),
             "local_s": round(u.get("local", {}).get("seconds", 0)),
             "dir": run_dir}
        ev = os.path.join(run_dir, "evidence.jsonl")
        kinds = {}
        if os.path.isfile(ev):
            for ln in open(ev):
                it = json.loads(ln)
                kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
        r["evidence"] = kinds
        vt = open(os.path.join(run_dir, "verdict.txt")).read() if os.path.isfile(os.path.join(run_dir, "verdict.txt")) else ""
        r["team_manual_cites"] = len(re.findall(r"\[(?:[^\]]*\b)?[MSP]\d+", vt))
        r["team_own_judgement"] = vt.count("own judgement")
        rows.append(r)
        by_case.setdefault(name, {})[w] = r
    json.dump(rows, open(os.path.join(OUT, "summary.json"), "w"), indent=1)

    def avg(key, rs):
        v = [x[key] for x in rs if isinstance(x.get(key), (int, float))]
        return round(sum(v) / len(v), 1) if v else "-"
    L = ["# Team mode replay (handoff step 4)", "",
         f"{len(by_case)} cases, {len(rows)} Team runs. Home = the app's Final Diagnosis on the PC's Ollama "
         "(same case summary, manual pack and prompt). Team = Gemini + GPT-OSS diagnose, cross-check, Claude writes "
         "the verdict. Team members' DB text: " + ("on (capped about 40k each), local Qwen a member with tools"
                                            if any(json.load(open(os.path.join(r["dir"], "report.json"))).get("cloud_manual")
                                                   for r in rows) else "off") + ".", "",
         "| | web on | web off |", "|---|---|---|"]
    for key, label in (("seconds", "time per case, s"), ("team_flags", "Team [source check] flags"),
                       ("team_bad_ids", "Team ids that don't exist"), ("team_manual_cites", "Team manual citations"),
                       ("team_issues", "Team issues"), ("agy_calls", "agy calls"), ("agy_tokens_k", "agy tokens (k)"),
                       ("agy_cache_k", "agy cache-read tokens (k)"), ("claude_tokens_k", "Claude tokens incl. cache (k)"),
                       ("lookups", "lookups logged")):
        L.append(f"| {label} | {avg(key, [r for r in rows if r['web'] == 'on'])} | "
                 f"{avg(key, [r for r in rows if r['web'] == 'off'])} |")
    homes = [r for r in rows if r["home_flags"] is not None]
    L += ["", f"Home baseline: {len(homes)} runs, [source check] flags avg {avg('home_flags', homes)}, issues avg "
          f"{avg('home_issues', homes)}, ~{avg('local_s', homes)} s each.", "",
          "| case | web | s | fails | Team flags / bad ids / manual cites | Home flags | agy k tok | Claude k tok |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['case']} | {r['web']} | {r['seconds']:.0f} | {', '.join(r['failed']) or '-'} | "
                 f"{r['team_flags']} / {r['team_bad_ids']} / {r['team_manual_cites']} | {r['home_flags'] if r['home_flags'] is not None else 'skipped'} | "
                 f"{r['agy_tokens_k']} | {r['claude_tokens_k']} |")
    open(os.path.join(OUT, "REPORT.md"), "w").write("\n".join(L) + "\n")

    G = ["# Grade sheet: which verdict would you act on?", "",
         "For each case write HOME, TEAM-WEB, TEAM-NOWEB or NONE on the GRADE line (more than one is fine). "
         "Full traces are in logs/team/replay/<case>__web_<on|off>/.", ""]
    for name, ws in by_case.items():
        case = json.load(open(os.path.join(CASES, name + ".json")))
        v = case.get("vehicle_info", {})
        G += [f"## {name}", "",
              f"**Case:** {v.get('year')} {v.get('make')} {v.get('model')} {v.get('engine', '')}, codes "
              f"{case.get('dtcs') or 'none'}" + (f", symptoms: {case['symptoms']}" if case.get("symptoms") else "")
              + (f", ruled out: {case['ruled_out']}" if case.get("ruled_out") else ""), ""]
        base = next((os.path.join(r["dir"], "baseline.txt") for r in ws.values()
                     if os.path.isfile(os.path.join(r["dir"], "baseline.txt"))
                     and os.path.getsize(os.path.join(r["dir"], "baseline.txt"))), None)
        G += ["### HOME", "", open(base).read().strip() if base else "(baseline didn't run)", ""]
        for w in ("on", "off"):
            if w in ws:
                p = os.path.join(ws[w]["dir"], "verdict.txt")
                G += [f"### TEAM-{'WEB' if w == 'on' else 'NOWEB'}", "",
                      (open(p).read().strip() if os.path.isfile(p) else "") or "(no verdict)", ""]
        G += ["**GRADE:** ", "", "---", ""]
    open(os.path.join(OUT, "GRADE.md"), "w").write("\n".join(G))
    print("\n".join(L))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("cases", "run", "baselines", "report"))
    ap.add_argument("--only")
    ap.add_argument("--web", default="both", choices=("on", "off", "both"))
    a = ap.parse_args()
    {"cases": make_cases, "run": lambda: run(a.only, a.web), "baselines": baselines,
     "report": report}[a.cmd]()


if __name__ == "__main__":
    main()
