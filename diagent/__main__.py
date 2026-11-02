"""python -m diagent run|gui|test|show  (see diagent/README.md)."""
import argparse
import json
import os
import subprocess
import sys

from . import ROOT, config, harness, kb
from .car import FakeCar, NoCar, RealCar
from .case import Case, Store
from .human import TerminalHuman
from .identify import identify
from .policy import ScriptedModel
from .tools import Tools


def gpu_busy():
    """Why the PC's GPU must not be used now, or '' (tools/gpu_guard.sh; only checkable on the PC itself)."""
    guard = os.path.join(ROOT, "tools", "gpu_guard.sh")
    if not os.path.isfile(guard) or subprocess.call(["which", "nvidia-smi"], stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL):
        return ""
    r = subprocess.run(["bash", "-c", f"source '{guard}'; gpu_free"], capture_output=True, text=True)
    return "" if r.returncode == 0 else (r.stdout.strip() or "GPU busy")


def pick_model(cfg, want, case, tools, out=print):
    """(model, banner). want: auto | pc | off. Never the deck's NPU models: they're too weak for tool loops."""
    if want != "off":
        why = gpu_busy()
        if why:
            return ScriptedModel(case, tools), f"AGENT OFF ({why}). Plain manual walk, no model."
        name = cfg[cfg["agent_role"]]
        for url in (cfg["local_url"], cfg["home_url"]):
            have = harness.ollama_has(url, name)
            if have:
                return (harness.OllamaChat(url, name, cfg["num_ctx"], cfg["temperature"], cfg["think"]),
                        f"AGENT ON: {name} at {url}")
            if have is False:
                out(f"{url} is up but has no {name}")
        if want == "pc":
            sys.exit("PC Ollama not reachable (or model missing); use --agent off for the plain walk.")
        return ScriptedModel(case, tools), "AGENT OFF (PC not reachable). Plain manual walk, no model."
    return ScriptedModel(case, tools), "AGENT OFF (--agent off). Plain manual walk, no model."


def open_case(a, cfg, human, out=print):
    """Identify the car (or reopen a saved case), start the case and pick the model: the same start for the
    terminal `run` and the GUI. Returns (case, tools, model, banner, store); SystemExit says why nothing started."""
    store = Store(cfg["case_db"])
    if a.resume:
        case = store.load(a.resume)
        if case.status != "open":
            out(f"case {case.id} was {case.status}; reopening")
            case.status = "open"
        car = case.car
        r, path = kb.open_car(car, cfg["kb_dir"])
        reader = RealCar(car["make"], cfg) if a.car == "real" else NoCar()
    else:
        reader = (RealCar(a.make, cfg) if a.car == "real" else NoCar() if a.car == "none"
                  else FakeCar(json.load(open(a.car)).get("fake_car")))
        given = {"vin": a.vin, "make": a.make, "model": a.model, "year": a.year, "engine": a.engine}
        car, r, path, notes = identify(cfg, human, reader, store, given)   # always first: which car is this
        for n in notes:
            out(f"[car] {n}")
        if car is None:
            sys.exit("Car not identified, so no case was started. Give --vin, or --make/--model/--year.")
        if car.get("history"):
            out("[car] Earlier visits: " + "; ".join(car["history"]))
        case = Case(car=car, codes=[c.strip().upper() for c in (a.codes or "").split(",") if c.strip()],
                    symptoms=a.symptoms or "")
        store.new(case, label=f"{car['year']} {car['make']} {car['model']}")
    tools = Tools(case, r, path, human, reader, store, cfg)
    model, banner = pick_model(cfg, a.agent, case, tools, out)
    return case, tools, model, banner, store


def cmd_run(a, cfg):
    human = TerminalHuman(color=sys.stdout.isatty())
    case, tools, model, banner, store = open_case(a, cfg, human)
    car = case.car
    print(f"\n*** {banner} ***\nCase {case.id}: {car.get('year')} {car['make']} {car.get('model')}, "
          f"codes {', '.join(case.codes) or 'none (read_car or symptoms)'}. Log: {cfg['case_db']} "
          f"(python -m diagent show {case.id})")
    harness.run(case, tools, model, human, store, cfg)
    print(f"\nCase {case.id}: {case.status}. " + (case.result.get("summary", "") if case.result else ""))
    if case.result.get("settle_test"):
        print(f"Still needed: {case.result['settle_test']}")


def cmd_show(a, cfg):
    import json
    store = Store(cfg["case_db"])
    case = store.load(a.case_id)
    print(f"Case {case.id} {case.status}: {case.car} codes {case.codes}")
    print("Result:", json.dumps(case.result, ensure_ascii=False))
    for ts, turn, kind, tool, args, result, ids, ok in store.audit_rows(case.id):
        print(f"[{turn}] {kind:6} {tool:18} {'' if ok else 'X '}{args[:120]}\n        -> {result[:240]}")


def main():
    ap = argparse.ArgumentParser(prog="python -m diagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="work a case at the car (terminal UI)")
    r.add_argument("--make")
    r.add_argument("--model")
    r.add_argument("--year", type=int)
    r.add_argument("--engine")
    r.add_argument("--vin")
    r.add_argument("--codes", help="comma-separated, if not read from the car")
    r.add_argument("--symptoms")
    r.add_argument("--agent", choices=["auto", "pc", "off"], default="auto")
    r.add_argument("--car", default="none", help="none | real | scenario.json (simulated car)")
    r.add_argument("--resume", type=int)
    g = sub.add_parser("gui", help="the same as run, in a window (PyQt6)")
    for opt in ("--make", "--model", "--engine", "--vin", "--codes", "--symptoms"):
        g.add_argument(opt)
    g.add_argument("--year", type=int)
    g.add_argument("--agent", choices=["auto", "pc", "off"], default="auto")
    g.add_argument("--car", default="none", help="none | real | scenario.json (simulated car)")
    g.add_argument("--resume", type=int)
    g.add_argument("--play", metavar="NAME", help="play a test scenario in the window, answers filled in by script")
    g.add_argument("--install-icon", action="store_true", help="add diagent to the app list (no autostart)")
    t = sub.add_parser("test", help="scripted fake-the user scenarios")
    t.add_argument("names", nargs="*")
    t.add_argument("--agent", choices=["scripted", "pc"], default="scripted")
    gn = sub.add_parser("gen", help="write script-chosen scenarios to diagent/scenarios/gen_*.json")
    gn.add_argument("--seed", type=int, default=20261025)
    s = sub.add_parser("show", help="print a case and its audit log")
    s.add_argument("case_id", type=int)
    a = ap.parse_args()
    cfg = config.load()
    if a.cmd == "run":
        if not a.resume and not (a.codes or a.symptoms or a.car != "none"):
            ap.error("run needs --codes, --symptoms or --car real|scenario.json (or --resume ID)")
        cmd_run(a, cfg)
    elif a.cmd == "gui":
        from .gui import main as gui_main
        sys.exit(gui_main(a, cfg))
    elif a.cmd == "show":
        cmd_show(a, cfg)
    elif a.cmd == "gen":
        from .gen import write
        cases = write(a.seed)
        fams = {}
        for c in cases:
            fams[c["family"]] = fams.get(c["family"], 0) + 1
        print(f"{len(cases)} scenarios (seed {a.seed}): " + ", ".join(f"{k} {v}" for k, v in sorted(fams.items())))
    else:
        from .testing import run_all
        sys.exit(run_all(a.names, a.agent, cfg))


if __name__ == "__main__":
    main()
