"""The agent's tools. All read-only towards the car: there is no tool that sends a write, an actuator test or
a clear. Python owns the procedure walk: a reading or a yes/no answers the current step's check here, the
next step comes from the manual's Yes/No cell, and the scan answers "which DTCs are set" steps.

Every result names source ids (M = manual page/step, SP = spec, L = labor, N = NHTSA/TSB counts,
SC = scan, F = figure, R = the user's reading, LV = live snapshot). Claims may only cite those."""
import json
import re
import time

from . import field, kb, live, units, watch

STATE_LABEL = {"key_off": "ignition OFF", "key_on": "ignition ON, engine OFF", "running": "engine running",
               "test_connector": "test mode connector connected"}
STATE_RX = [("key_off", re.compile(r"ignition switch to off|ignition (switch )?off|turn (the )?ignition off|key off", re.I)),
            ("key_on", re.compile(r"ignition switch to on\b|ignition (switch )?on\b|key on\b", re.I)),
            ("running", re.compile(r"start (and idle )?the engine|idle the engine|warm(-| )?up the engine|"
                                   r"engine (is )?running|run the engine|drive the vehicle", re.I)),
            ("test_connector", re.compile(r"test mode connector", re.I))]
# manual steps that need a write to the car: done only from the main app's TESTS screen, with its own guards
WRITE_RX = re.compile(r"clear memory|operation check mode|compulsory|\bmode 04\b|clear (the )?(dtcs?|codes?|trouble codes?)\b|"
                      r"\boperate (the )?[^.;]{0,60}(using|with) (the )?(subaru )?select monitor|"
                      r"\b(activate|drive) (the )?[^.;]{0,40}(using|with) (the )?(subaru )?select monitor|actuator test", re.I)
FIG_RX = re.compile(r"\[fig\]|figure|\([A-Z]\d{1,3}\)|connector", re.I)
NOT_SIMPLE_YESNO = re.compile(r"^\s*(is|are|does|do|did|was|were|has|have|can)\b", re.I)


def states_in(text):
    """Required car states in the order the step mentions them."""
    hits = []
    for name, rx in STATE_RX:
        for m in rx.finditer(text or ""):
            hits.append((m.start(), name))
    return [n for _, n in sorted(hits)]


# notes in a step that are about operating the dealer scan tool, not about the car: dropped from the card
BOILER = re.compile(r"(select monitor|general scan tool|scan tool).{0,80}(operation|operating|procedure|manual|"
                    r"instruction)|for detailed operation|refer to .{0,40}(operation manual|instruction manual)", re.I)


TOOL_ONLY = re.compile(r"^(note:?\s*)?[●•*-]?\s*((subaru )?select monitor|(obd-ii )?general scan tool|scan tool)\.?$", re.I)


def actions_of(step_cell):
    """(title, [action, ...]) from a step cell: the manual's own words, one numbered action per prompt,
    connector/terminal lines kept with the action they belong to, scan-tool boilerplate dropped."""
    parts = [x.strip() for x in re.split(r";|(?<=\.)\s+(?=\d\)\s?)", step_cell or "") if x.strip()]
    if not parts:
        return "", []
    title, rest = (parts[0], parts[1:]) if parts[0].isupper() or len(parts) > 1 else ("", parts)
    out, skip_note = [], False
    for x in rest:
        if re.fullmatch(r"NOTE:?", x, re.I):
            skip_note = True
            continue
        if BOILER.search(x) or TOOL_ONLY.match(x):
            continue
        if skip_note and not re.match(r"^\d\)", x):
            out.append("Note: " + x)
            skip_note = False
            continue
        skip_note = False
        x = re.sub(r"^(\d)\)\s*", r"\1) ", x)
        if out and (re.match(r"^[(\[]?[A-Z]\d{1,3}[)\]]|^No\. \d|^Terminals?\b|^Connector (&|and) terminal", x)
                    or out[-1].endswith(":") or out[-1].endswith("terminal")):
            out[-1] = f"{out[-1]} {x}"      # 'Connector & terminal:' + '(B135) No. 9 (+) — Chassis ground (-):'
        else:
            out.append(x)
    return title, out


SCHEMAS = [
    ("get_procedure", "The factory manual's diagnostic procedure (step table) for a scanned DTC (or one a manual "
     "page of this case names), scoped to this car. Sets it as the procedure being walked. 'refer to DTC X' pages "
     "are followed. With no codes, use symptom_chart instead.",
     {"code": ("string", "the DTC"), "variant": ("string", "optional: variant key from an earlier result")},
     ["code"]),
    ("answer_scan_steps", "Answer the procedure's 'does the scan tool show DTC X' steps from the scan, in Python.", {}, []),
    ("ask_human", "Give the user one instruction for the current step and wait for their reply. Python checks the "
     "safety state, parses and unit-checks the reply, and answers the step's check when it can.",
     {"instruction": ("string", "short plain sentences from the step's own text"),
      "expect": ("string", "number | yesno | choice | text | done"),
      "unit": ("string", "for number: the unit the step's check uses"),
      "choices": ("array", "for choice: the options"),
      "source_id": ("string", "the M id of the manual text this instruction comes from"),
      "question": ("string", "the manual's question for this instruction, worded from the cited text "
                   "(filled in by Python on a step table)")},
     ["instruction", "expect", "source_id"]),
    ("page_steps", "Split a manual page into its instructions, in order, each with its own id, so they can be "
     "given to the user one at a time (pages with no step table).",
     {"section_id": ("string", "M id or section number")}, ["section_id"]),
    ("record_step", "Answer the current step's check when Python could not (The user replied in words). Must cite "
     "the reading id (R..) of his reply at this step.",
     {"answer": ("string", "Yes or No"), "evidence_ids": ("array", "reading ids"), "why": ("string", "one line")},
     ["answer", "evidence_ids", "why"]),
    ("search_manual", "Search this car's factory manual (and CHARM) pages. scope 'symptom' searches only the "
     "symptom/trouble charts, matched to the manual's terms.",
     {"query": ("string", "search words"), "k": ("integer", "results, default 4"),
      "scope": ("string", "all | symptom")}, ["query"]),
    ("read_section", "The text of one manual page by its M id or section number.",
     {"section_id": ("string", "M id or section number")}, ["section_id"]),
    ("get_spec", "Torque/spec values printed in the manual for a part. Empty means none found: never guess one.",
     {"term": ("string", "part name")}, ["term"]),
    ("read_car", "Scan codes and one live data snapshot from the car (read-only).", {}, []),
    ("watch_live", "Watch live values while the user does what the instruction says. Python judges every sample "
     "against this car's zones and evaluates the trigger, and returns the summary (min/max/mean, change, in/out "
     "of zone with its source). Read-only. The trigger value must come from a cited manual step.",
     {"params": ("array", "the values to watch, as the manual names them"),
      "seconds": ("integer", "longest time to watch"),
      "instruction": ("string", "what the user does meanwhile, from the cited manual text"),
      "source_id": ("string", "M id of the manual text"),
      "trigger": ("object", "optional: {param, op: above|below|change, value, for_s}")},
     ["params", "seconds", "instruction", "source_id"]),
    ("pathways", "Where this car's manual sends you when a live value is out of zone: DTC procedures and "
     "inspections, each with the manual text that links them.",
     {"param": ("string", "the value, as the manual names it"), "direction": ("string", "low | high | unstable")},
     ["param", "direction"]),
    ("update_hypothesis", "Add or move a candidate cause on the board. Candidates come from the manual tree or "
     "retrieved sources; general knowledge only with general_knowledge=true (shown as unverified).",
     {"cause": ("string", "the cause as the source words it"),
      "status": ("string", "open | supported | ruled_out"),
      "evidence_ids": ("array", "source/reading ids behind it"),
      "general_knowledge": ("boolean", "true when no retrieved source names it")},
     ["cause", "status", "evidence_ids"]),
    ("common_issues", "NHTSA complaint/recall/TSB/forum counts for this model and year.", {}, []),
    ("field_reports", "Second opinion from field knowledge for a code or symptom on this car: TSBs/recalls, "
     "baked cards, forum fixes, Reddit and forum threads, owner complaints, each with its source kind, how many "
     "separate reports agree and the car match. It may suggest an order or an extra manual test, never a step "
     "or a spec.", {"query": ("string", "the code or the symptom")}, ["query"]),
    ("offer_detour", "Offer the user one detour suggested by field reports: the manual's inspection of a component. "
     "Python finds (or checks) the manual inspection section; if there is none, nothing is offered. The user says "
     "yes or no; the tree position is kept.",
     {"component": ("string", "the component, as the field report names it"),
      "field_ids": ("array", "FR ids behind the suggestion"),
      "manual_section_id": ("string", "optional: M id of the manual inspection for that component")},
     ["component", "field_ids"]),
    ("symptom_chart", "The manual's symptom chart for what the user describes: Python matches their words to the "
     "chart's symptoms, the user confirms the one that fits, and its suspect items come back in the manual's order "
     "with the likelihood rank and each item's inspection section.",
     {"query": ("string", "the symptom in the user's words")}, ["query"]),
    ("end_detour", "Return from a detour to the manual tree where it was left, recording what was found.",
     {"found": ("string", "one line, citing reading ids")}, ["found"]),
    ("show_figure", "Show the user the figures of a manual page.",
     {"section_id": ("string", "M id or section number")}, ["section_id"]),
    ("labor", "Book labor hours (CHARM) for an operation.",
     {"term": ("string", "the operation")}, ["term"]),
    ("finish", "End the run. 'diagnosis' only when the manual path reached its end on real answers; otherwise "
     "'unconfirmed' with the manual check still needed.",
     {"result": ("string", "diagnosis | unconfirmed"), "summary": ("string", "what was found, citing ids"),
      "citations": ("array", "ids backing the summary"),
      "settle_test": ("string", "for unconfirmed: the manual check still needed, with its id")},
     ["result", "summary", "citations"]),
]


def ollama_tools(names=None):
    out = []
    for name, desc, props, req in SCHEMAS:
        if names and name not in names:
            continue
        p = {}
        for k, (typ, d) in props.items():
            p[k] = {"type": typ, "description": d}
            if typ == "array":
                p[k]["items"] = {"type": "string"}
        out.append({"type": "function", "function": {"name": name, "description": desc,
                                                     "parameters": {"type": "object", "properties": p, "required": req}}})
    return out


class ToolError(Exception):
    pass


class Tools:
    def __init__(self, case, r, kb_path, human, car, store, cfg):
        self.case, self.r, self.kb_path, self.human, self.car, self.store, self.cfg = \
            case, r, kb_path, human, car, store, cfg
        self._procs = {}

    # ------------------------------------------------------------------ procedure state
    def _proc(self, code):
        if code not in self._procs:
            self._procs[code] = kb.procedure(self.r, code, car_features=self.case.car.get("features") or None)
        return self._procs[code]

    def steps(self):
        p = self.case.proc
        if not p or p.get("mode") != "steps":
            return {}
        proc = self._proc(p["code"])
        v = next(v for v in proc["variants"] if v["key"] == p["variant"])
        return v["steps"]

    def step_src(self, s):
        page = f" p.{s['page']}" if s["page"] else ""
        kind = "FSM" if not str(s["path"]).startswith("Repair and Diagnosis") else "CHARM"
        return self.case.add_source("step", f"{kind}{page}, {s['path']}, step {s['n']}",
                                    text=f"{s['step']} | {s['check']} | Yes: {s['yes']} | No: {s['no']}",
                                    sid=s["sid"], page=s["page"])

    def current(self):
        p = self.case.proc
        st = self.steps()
        return st.get(p.get("current")) if p and p.get("current") is not None else None

    def _advance(self, answer, how, reading_ids=(), why=""):
        """Record the current step's answer and move on along the manual's Yes/No cell."""
        p, steps = self.case.proc, self.steps()
        s = steps[p["current"]]
        cell = s["yes"] if answer == "Yes" else s["no"]
        nxt = kb.next_of(cell)
        p["path"].append({"step": s["n"], "answer": answer, "next": nxt, "how": how, "readings": list(reading_ids),
                          "why": why, "src": self.step_src(s)})
        p["unconfirmed"] = [u for u in p.get("unconfirmed", []) if u.get("step") != s["n"]]   # answered now
        p["declined"] = [d for d in p.get("declined", []) if d != f"step:{s['n']}"]
        msg = [f"Step {s['n']} [{self.step_src(s)}]: {answer} ({how}{': ' + why if why else ''})."]
        if nxt is None:
            p.update(current=None, ended=True, end_text=cell, end_src=self.step_src(s))
            refer = re.search(r"\bDTC ([PBCU]\d{4})", cell)
            msg.append(f"The manual's tree ends here: \"{cell}\" [{self.step_src(s)}]."
                       + (f" It points to DTC {refer.group(1)}: get_procedure that code to go on." if refer else
                          " Put the result on the hypothesis board, then finish."))
            return " ".join(msg)
        if nxt not in steps:
            p.update(current=None, broken=f"the manual says go to step {nxt}, which isn't in this table "
                     f"(the PDF conversion lost it)", broken_src=self.step_src(s))
            msg.append(f"Step {nxt} is missing from the parsed table. Show the user the page text "
                       f"(read_section {self.step_src(s)}) and ask them to read step {nxt} from the manual, or "
                       f"finish unconfirmed.")
            return " ".join(msg)
        p["current"] = nxt
        msg.append(self._enter())
        return " ".join(msg)

    def _enter(self):
        """Arriving at the current step: scan steps answer themselves, broken ones stop the walk."""
        p, steps = self.case.proc, self.steps()
        s = steps[p["current"]]
        if s["status"] != "ok":
            src = self.step_src(s)
            why = ("the converter mangled this row" if s["status"] == "broken" else
                   "this row's page doesn't name this code's component (likely another procedure's page)")
            p.update(broken=f"step {s['n']} is {s['status']}: {why}", broken_src=src)
            return (f"Now at step {s['n']} [{src}], but it is {s['status']} ({why}). Don't guess it: show the user "
                    f"the page text (read_section {src}) and let them read the step from the manual, or finish "
                    f"unconfirmed.")
        ans = kb.scan_answers({s["n"]: s}, self.case.codes)
        if s["n"] in ans and self.case.scan_ts:
            return self._advance(ans[s["n"]], "answered from this case's scan: codes " + (", ".join(self.case.codes) or "none"))
        if s["n"] in ans:
            return (f"Current step {s['n']} [{self.step_src(s)}] asks which DTCs are set, and this case has no scan "
                    f"yet (codes were typed). read_car, or ask the user: {s['check']}")
        return f"Current step {s['n']} [{self.step_src(s)}]: {s['step']} | Check: {s['check']}"

    # ------------------------------------------------------------------ tools
    def get_procedure(self, code, variant=None):
        code = code.strip().upper()
        if not re.fullmatch(r"[PBCU][0-9A-F]{4}", code):
            raise ToolError(f"{code!r} is not a DTC")
        c = self.case
        known = code in c.codes or any(code in v["text"] or code in v["label"] for v in c.sources.values()) \
            or code in self.store.audit_text(c.id)
        if not known:
            raise ToolError(f"{code} is neither a scanned code nor named in a manual page of this case")
        old = c.proc or {}
        if old.get("code") == code and old.get("mode") == "steps" and not variant and not old.get("ended"):
            s = self.current()                       # already walking it: keep the position, don't restart
            return (f"Already walking {code} (variant {old['variant']}); the tree is at "
                    + (f"step {s['n']} [{self.step_src(s)}]: {s['check']}" if s else "its stop point")
                    + ". Carry on from there."), []
        proc = self._proc(code)
        if not proc:
            self.case.proc = {"code": code, "via": None, "mode": "pages", "variant": None, "current": None,
                              "path": [], "history": []}
            return f"No manual procedure is linked to {code} for this car. Use search_manual.", []
        cands = [v for v in proc["variants"] if v["usable"]]     # already filtered to the car's engine (kb.fits)
        chosen = None
        if variant:
            chosen = next((v for v in proc["variants"] if v["key"] == variant), None)
            if not chosen:
                raise ToolError(f"no variant {variant}; have {', '.join(v['key'] for v in proc['variants'])}")
        elif len(cands) == 1:
            chosen = cands[0]
        elif len(cands) > 1:
            i = self.human.choose(f"The manual has {len(cands)} versions of the {code} chart. Which fits this car?",
                                  [v["label"] for v in cands] + ["none of these / not sure"])
            self.store.audit(self.case, "human", "choose_variant", {"code": code},
                             f"picked {i}: {cands[i]['label'] if i < len(cands) else 'none'}")
            chosen = cands[i] if i < len(cands) else None
        old = self.case.proc
        hist = (old.get("history", []) + [{k: old[k] for k in ("code", "variant", "path", "end_text") if k in old}]
                if old and old.get("code") else [])
        ids = []
        lines = [f"{proc['title']}" + (f" (the manual's page for {proc['via']} sends you here)" if proc["via"] else "")]
        if chosen is None:
            self.case.proc = {"code": code, "via": proc["via"], "mode": "pages", "variant": None, "current": None,
                              "path": [], "history": hist}
            lines.append("No usable step table for this car" + (" (versions found: " + "; ".join(
                v["label"] for v in proc["variants"]) + ")" if proc["variants"] else "")
                + ". Work from the manual pages below: read_section them, instruct the user from their text, cite the M id.")
        else:
            first = min(chosen["steps"])
            self.case.proc = {"code": code, "via": proc["via"], "mode": "steps", "variant": chosen["key"],
                              "current": first, "path": [], "ended": False, "history": hist}
            lines.append(f"Variant {chosen['key']}: {chosen['label']}")
            if proc.get("excluded"):
                lines.append("Not for this car's engine: " + "; ".join(proc["excluded"]))
            for n, s in sorted(chosen["steps"].items()):
                sid = self.step_src(s)
                ids.append(sid)
                flag = "" if s["status"] == "ok" else f" [{s['status'].upper()}: don't walk, show page text]"
                lines.append(f"{n} [{sid}]{flag} {units_cut(s['step'], 260)} | Check: {units_cut(s['check'], 200)} | "
                             f"Yes: {units_cut(s['yes'], 120)} | No: {units_cut(s['no'], 120)}")
        for pg in proc["pages"][:8]:
            sid = self.case.add_source("section", f"{'CHARM' if pg['kind'] == 'charm' else 'FSM'}"
                                       f"{' p.' + str(pg['page']) if pg['page'] else ''}, {pg['path']}",
                                       sid=pg["sid"], page=pg["page"])
            ids.append(sid)
            lines.append(f"page [{sid}] {pg['path'][:140]} ({pg['chars']} chars)")
        if chosen is not None:
            lines.append(self._enter())
        return "\n".join(lines), ids

    def answer_scan_steps(self):
        p, steps = self.case.proc, self.steps()
        if not steps:
            raise ToolError("no step table is being walked")
        ans = kb.scan_answers(steps, self.case.codes)
        if not ans:
            return f"No step of this table asks which DTCs are set (scan codes: {', '.join(self.case.codes) or 'none'}).", []
        done = [e["step"] for e in p["path"] if e["how"].startswith("answered from the scan")]
        return (f"Scan-answered steps in this table: " + ", ".join(f"step {n}: {a}" for n, a in sorted(ans.items()))
                + f". Applied automatically when reached (already applied: {done or 'none'})."), []

    def ask_human(self, instruction, expect, source_id, unit=None, choices=None, question=None):
        c, p = self.case, self.case.proc
        if source_id not in c.known_ids():
            scans = [k for k in c.sources if k.startswith("SC")]
            raise ToolError(f"source_id {source_id} is not from this run"
                            + (f" (a question that isn't from the manual, like the symptom, cites the scan "
                               f"[{scans[-1]}])" if scans else ""))
        declined = (p or {}).get("declined", [])
        s_now = self.current() if p and p.get("mode") == "steps" and not p.get("broken") and not p.get("detour") else None
        intake = source_id.startswith("SC")          # the symptom question cites whichever scan is latest
        key = f"step:{s_now['n']}" if s_now else ("intake" if intake else f"src:{source_id}")
        if key in declined:            # The user said can't / skip / done: asking again is not another manual path
            raise ToolError(f"{'step ' + str(s_now['n']) if s_now else 'that question' if intake else '[' + source_id + ']'}"
                            " was already answered or declined by the user: give the next instruction, look for another "
                            "manual path (search_manual / read_section / page_steps), or finish unconfirmed")
        s = self.current()
        step_mode = s is not None and not p.get("broken") and not p.get("detour")
        if p.get("detour"):
            dsid = p["detour"]["sid"]
            if self.case.sources.get(source_id, {}).get("sid") != dsid:
                raise ToolError(f"during the detour, instructions come from its manual section ({p['detour']['src']}) "
                                "or its page_steps; end_detour to go back to the tree")
        elif p and p.get("mode") == "steps" and not step_mode and not p.get("broken") and not p.get("ended"):
            raise ToolError("no current step")
        notes = []
        check = s["check"] if step_mode else (question or "")
        dim = units.expected_unit(check) if step_mode else None
        if dim:
            if expect != "number" or (unit and (units.norm_unit(unit) or ("",))[0] != dim):
                notes.append(f"Python set the reply to a number in {dim} (the step's check is a {dim} comparison).")
            expect, unit = "number", dim
        elif step_mode and NOT_SIMPLE_YESNO.match(check):
            if expect != "yesno":       # a yes/no question with no single value to compare: the user answers yes/no
                notes.append("Python set the reply to yes/no (the step's check is a yes/no question).")
            expect, unit = "yesno", None
        if step_mode and kb.scan_answers({s["n"]: s}, c.codes) and not c.scan_ts and getattr(self.car, "can_scan", True):
            raise ToolError("this step asks which DTCs are set: call read_car first, the scan answers it in Python")
        if not step_mode and not check and expect in ("yesno", "number"):
            q = re.search(r"[^.;?]*\b(If|Does|Do|Is|Are|Was|Were|Did|Has|Have|Can)\b[^?]*\?",
                          c.sources.get(source_id, {}).get("text", ""))
            if q:                        # the cited text's own question
                check = q.group(0).strip()
                notes.append(f"Question taken from [{source_id}].")
            else:                        # the cited text asks nothing: the user reports, no yes/no branch is taken
                expect = "text"
                notes.append(f"[{source_id}] has no question of its own: the user's reply is recorded as a report.")
        if not step_mode and not check:
            check = "Say done when it's done, or tell me what you found."   # what to report when the page asks nothing
        text = (s["step"] + " " + check) if step_mode else instruction
        need = states_in(text) or states_in(instruction)
        if need and need[0] != c.state_ok:
            ok = self.human.confirm(f"Before this step: {STATE_LABEL[need[0]]}"
                                    + (" (the TEST MODE switch stays OFF)" if need[0] == "test_connector" else "")
                                    + ". Is the car in that state?")
            self.store.audit(c, "human", "confirm_state", {"state": need[0]}, "yes" if ok else "no")
            if not ok:
                return self._cant(s if step_mode else None, f"car not in the required state ({STATE_LABEL[need[0]]})")
        if need:
            c.state_ok = need[-1]
        if WRITE_RX.search(text):
            notes.append("This step needs a write to the car (actuator test or clear). Do it from the main app's "
                         "TESTS screen with its own guards, then report the result here. This app never sends it.")
        if step_mode and FIG_RX.search(s["step"]):
            figs = kb.figures(self.r, s["sid"], self.cfg["image_dirs"])
            if figs:
                self.human.show_figures(figs, self.step_src(s))
        header = f"Step {s['n']} [{self.step_src(s)}]" if step_mode else f"[{source_id}]"
        title, acts = actions_of(s["step"]) if step_mode else ("", [instruction])
        watched = ""
        if step_mode:
            watched = self._auto_watch(s)                 # "until <value> reaches <mark>": Python watches it
            got = self._live(s, check, dim, header)       # the deck reads the value itself when it can
            if got:
                return (watched + got[0], got[1]) if watched else got
        for i, act in enumerate(acts if step_mode and len(acts) > 1 else []):    # one action per prompt
            r = self.human.act(header, title, act, i + 1, len(acts))
            self.store.audit(c, "human", "action", {"header": header, "n": i + 1, "action": act}, r)
            cmd = units.parse_reply(r or "done", "done").get("command") if r else None
            if cmd in ("cant", "skip"):
                return self._cant(s, f"action {i + 1} ({act[:60]}): {'skipped' if cmd == 'skip' else r}", skip=cmd == "skip")
            if cmd == "stop":
                c.status = "stopped"
                return "The user said stop. The case is saved; the run ends.", []
            if cmd == "back":
                return self._back()
        shown = instruction if not step_mode or len(acts) <= 1 else (title or instruction)
        reply, parsed = None, None
        for attempt in range(4):
            reply = self.human.ask(header, shown, check, expect, unit, choices, notes)
            parsed = units.parse_reply(reply, expect, unit)
            if parsed["kind"] == "command" and parsed["command"] == "done" and expect in ("number", "yesno", "choice"):
                parsed = {"kind": "error", "why": "this step needs " + (f"a reading in {unit}" if expect == "number" and unit
                                                                        else "a reading" if expect == "number" else expect)}
            if parsed["kind"] != "error":
                break
            self.human.say(f"Didn't take that: {parsed['why']}. Try again (or say can't / skip / back / stop).")
        self.store.audit(c, "human", "reply", {"header": header, "question": check}, reply)
        if parsed["kind"] == "error":
            return self._cant(s if step_mode else None, f"reply not understood ({parsed['why']})")
        if parsed["kind"] == "command":
            cmd = parsed["command"]
            if cmd == "stop":
                c.status = "stopped"
                return "The user said stop. The case is saved; the run ends.", []
            if cmd == "back":
                return self._back()
            if cmd == "done":
                if not step_mode and p is not None:
                    p.setdefault("declined", []).append(key)    # done: that instruction is finished, move on
                return f"The user: done with [{source_id}]. Give the next instruction (or finish).", []
            return self._cant(s if step_mode else None, parsed.get("note") or ("skipped" if cmd == "skip" else "can't do it"),
                              skip=cmd == "skip", source_id=source_id)
        step_n = s["n"] if step_mode else None
        if parsed["kind"] == "number":
            rid = c.add_reading(step_n, parsed["value"], parsed["unit"], reply, "measured by the user",
                                parsed["assumed_unit"])
            shown_v = units.fmt(parsed["value"], parsed["unit"]) + (" (unit assumed from the step)" if parsed["assumed_unit"] else "")
            out = f"The user's reading [{rid}]: {shown_v}."
            v = units.judge(check, parsed["value"], parsed["unit"]) if step_mode else None
            if v:
                return watched + out + " Python: " + v[1] + ". " + self._advance(v[0], "Python compared the reading", [rid], v[1]), [rid]
            return watched + out + (" Python can't answer this step's check from one number: use record_step with this id."
                                    if step_mode else ""), [rid]
        if parsed["kind"] == "yesno":
            rid = c.add_reading(step_n, parsed["answer"], "", reply, "The user's answer")
            out = f"The user [{rid}]: {parsed['answer']}."
            if step_mode and not dim:
                return watched + out + " " + self._advance(parsed["answer"], "The user answered the check", [rid]), [rid]
            if step_mode:
                return out + (" He answered yes/no to a measured check; ask for the reading itself unless he "
                              "read it off the meter (then record_step with this id)."), [rid]
            return out, [rid]
        rid = c.add_reading(step_n, parsed.get("text", reply), "", reply, "The user's words")
        return (f"The user [{rid}]: \"{reply}\"." + (" If this answers the step's check, record_step with this id; "
                                                   "otherwise ask again." if step_mode else "")), [rid]

    def _auto_watch(self, s):
        """A step that waits for a live value to reach the manual's mark: watch it (once per step) when this car
        and cable can report it. Returns the summary line, or ''."""
        car, p = self.case.car, self.case.proc
        trig = watch.until_trigger(s["step"], car.get("telemetry_db"))
        cable = getattr(self.car, "cable", "") or car.get("cable") or ""
        if not trig or not hasattr(self.car, "sample") or s["n"] in p.setdefault("watched", []) \
                or not live.readable(car, cable, trig["key"]):
            return ""
        p["watched"].append(s["n"])
        self.human.say(f"Step {s['n']} [{self.step_src(s)}]: {trig['quote']}. Watching {trig['key']}...")
        samples, summary = watch.run(car, self.car, [trig["key"]], 900, {k: trig[k] for k in ("key", "op", "value", "for_s")},
                                     self.human)
        wid = watch.store(self.store.db, self.case.id, s["n"], [trig["key"]], samples, summary)
        src = self.case.add_source("watch", f"watch session {wid}: {trig['key']}", text=watch.text(summary))
        return f"[{src}] Python watched {trig['key']} ({trig['quote']}): {watch.text(summary)}\n"

    def _live(self, s, check, dim, header):
        """A step that reads a value off the scan tool: when this car + cable can report it, the deck reads it,
        shows it, and Python answers the check. None = can't, so the user is asked as usual."""
        car = self.case.car
        phrase = live.wanted(s["step"])
        if not phrase or not dim or not hasattr(self.car, "read_live"):
            return None
        key = live.param_for(phrase, car.get("telemetry_db"))
        cable = getattr(self.car, "cable", "") or car.get("cable") or ""
        if not key or not live.readable(car, cable, key):
            return None
        got = self.car.read_live(key)
        if not got:
            return None
        value, unit = got
        u = units.norm_unit(unit)
        if not u or u[0] != dim:
            return None                           # can't compare it with this check's unit: ask the user instead
        value = value * u[1]
        lid = f"LV{len(self.case.live) + 1}"
        self.case.live.append({"id": lid, "ts": time.time(), "text": f"{key} {value:g} {dim}", "meanings": []})
        rid = self.case.add_reading(s["n"], value, dim, f"{value:g} {dim} (deck, {cable})", f"read live by the deck ({cable})")
        self.human.say(f"{header}: the deck read {phrase} = {units.fmt(value, dim)} [{lid}].")
        self.store.audit(self.case, "live", "read_live", {"key": key, "cable": cable}, f"{value:g} {dim}", [lid])
        v = units.judge(check, value, dim)
        if not v:
            return None
        return (f"The deck read {phrase} live [{rid}, {lid}]: {units.fmt(value, dim)}. Python: {v[1]}. "
                + self._advance(v[0], "Python compared the live reading", [rid, lid], v[1])), [rid, lid]

    def _cant(self, s, why, skip=False, source_id=None):
        c, p = self.case, self.case.proc
        if not p:
            p = self.case.proc = {"code": None, "via": None, "mode": "pages", "variant": None, "current": None,
                                  "path": [], "history": []}
        if p is not None and (s is not None or source_id):
            p.setdefault("declined", []).append(f"step:{s['n']}" if s is not None else
                                                "intake" if source_id.startswith("SC") else f"src:{source_id}")
        if p and p.get("detour"):        # a detour that can't be done doesn't touch the tree
            p["detour"].setdefault("cant", []).append(why)
            return (f"The user {'skipped' if skip else 'could not do'} that detour instruction: {why}. "
                    "end_detour to go back to the tree."), []
        if s is not None:
            p.setdefault("unconfirmed", []).append({"step": s["n"], "why": why, "src": self.step_src(s), "alt": False})
            return (f"Step {s['n']} [{self.step_src(s)}] {'skipped' if skip else 'could not be done'}: {why}. "
                    f"Don't guess its result. Look for another manual path (search_manual / read_section) "
                    f"or finish unconfirmed with this step as the settling test."), []
        if p is not None:
            p.setdefault("unconfirmed", []).append({"step": None, "why": why, "src": source_id, "alt": False})
        return (f"The user {'skipped' if skip else 'could not do'} that: {why}. Look for another manual path "
                f"(search_manual / read_section / page_steps) before finishing."), []

    def _back(self):
        p = self.case.proc
        if not p or not p.get("path"):
            return "Nothing to go back to.", []
        last = p["path"].pop()
        p.update(current=last["step"], ended=False, broken=None)
        p.pop("end_text", None)
        s = self.steps()[last["step"]]
        return f"Back to step {last['step']} [{self.step_src(s)}]: {s['step']} | Check: {s['check']}", []

    def record_step(self, answer, evidence_ids, why):
        s = self.current()
        if s is None or self.case.proc.get("broken"):
            raise ToolError("no walkable current step")
        answer = answer.strip().capitalize()
        if answer not in ("Yes", "No"):
            raise ToolError("answer must be Yes or No")
        here = {r["id"]: r for r in self.case.readings if r["step"] == s["n"]}
        ev = [e for e in evidence_ids if e in here]
        if not ev:
            raise ToolError(f"cite a reading the user gave at step {s['n']} (have: {', '.join(here) or 'none'}); "
                            "ask_human first")
        for e in ev:   # a number Python can judge overrides the model's answer
            r = here[e]
            if isinstance(r["value"], float):
                v = units.judge(s["check"], r["value"], r["unit"])
                if v and v[0] != answer:
                    return self._advance(v[0], "Python compared the reading (model's answer overruled)", ev, v[1]), ev
        return self._advance(answer, "model read the user's reply", ev, why), ev

    def _alt_tried(self):
        """An alternative manual path was looked at after a step couldn't be done."""
        for u in (self.case.proc or {}).get("unconfirmed", []):
            u["alt"] = True

    def search_manual(self, query, k=4, scope="all"):
        k = max(1, min(int(k or 4), 8))
        self._alt_tried()
        out, ids, seen = [], [], set()
        if (scope or "").lower() == "symptom":
            for sid, path, page, row, sc in kb.symptom_hits(self.r, query, k):
                src = self.case.add_source("section", f"{'CHARM' if path.startswith('Repair and Diagnosis') else 'FSM'}"
                                           f"{' p.' + str(page) if page else ''}, {path}", text=row, sid=sid, page=page)
                ids.append(src)
                out.append(f"[{src}] {path[:160]}\n  chart row: {row}")
            return ("\n\n".join(out) or f"No symptom chart row in this car's manual matches {query!r}."), ids
        for sid in self.r.search_ids(query, k * 3):
            row = kb.section_row(self.r, sid)
            if not row:
                continue
            path, page, body, kind, _ = row
            base = path.split(" — ")[0]
            if base in seen:
                continue
            seen.add(base)
            src = self.case.add_source("section", f"{'CHARM' if kind == 'charm' else 'FSM'}"
                                       f"{' p.' + str(page) if page else ''}, {path}", sid=sid, page=page)
            ids.append(src)
            out.append(f"[{src}] {path[:160]}\n{units_cut(body, 500)}")
            if len(out) == k:
                break
        return ("\n\n".join(out) or f"Nothing in this car's manual for {query!r}."), ids

    def _sid(self, ref):
        ref = str(ref).strip().strip("[]")
        if ref in self.case.sources and self.case.sources[ref].get("sid"):
            return int(self.case.sources[ref]["sid"])
        if ref.isdigit():
            return int(ref)
        raise ToolError(f"{ref!r} is not a manual page id from this run")

    def read_section(self, section_id):
        sid = self._sid(section_id)
        row = kb.section_row(self.r, sid)
        if not row:
            raise ToolError(f"no section {sid}")
        if not kb.section_in_scope(self.r, sid):
            raise ToolError(f"section {sid} isn't in this car's manual scope")
        path, page, body, kind, _ = row
        self._alt_tried()
        if (self.case.proc or {}).get("broken_src") and self.case.sources.get(self.case.proc["broken_src"], {}).get("sid") == sid:
            self.case.proc["broken_shown"] = True
        src = self.case.add_source("section", f"{'CHARM' if kind == 'charm' else 'FSM'}"
                                   f"{' p.' + str(page) if page else ''}, {path}", text=body, sid=sid, page=page)
        return f"[{src}] {path}\n{units_cut(body, 3500)}", [src]

    def get_spec(self, term):
        rows = self.r.specs(term)
        if not rows:
            return f"No spec in the manual matches {term!r}. Show none.", []
        ids = [self.case.add_source("spec", r, text=r) for r in rows]
        return "\n".join(f"[{i}] {r}" for i, r in zip(ids, rows)), ids

    def labor(self, term):
        rows = self.r.labor(term)
        if not rows:
            return f"No CHARM labor row for {term!r}.", []
        ids = [self.case.add_source("labor", r, text=r) for r in rows]
        return "\n".join(f"[{i}] {r}" for i, r in zip(ids, rows)), ids

    def field_reports(self, query):
        rows = field.reports(self.r, self.case.car, query, self.kb_path and __import__("os").path.basename(
            self.kb_path)[:-3])
        if not rows:
            return f"No field reports for {query!r} on this car.", []
        ids, lines = [], []
        for x in rows:
            src = self.case.add_source("field", f"{x['kind']}: {x['label']} ({x['count']} {'report' if x['count'] == 1 else 'reports'}, "
                                       f"{x['match']})", text=x["text"] + " " + " ".join(x["refs"]))
            ids.append(src)
            lines.append(f"[{src}] {x['kind']} | {x['label']} | {x['count']} separate "
                         f"{'report' if x['count'] == 1 else 'reports'} | car match: {x['match']}"
                         + (f" | {x.get('confirmed')} confirmed fixed" if x.get("confirmed") else ""))
        lines.append("Field knowledge is a second opinion: it may set the order of checks or suggest a manual "
                     "inspection (offer_detour); the manual's steps and specs stay the source.")
        return "\n".join(lines), ids

    def offer_detour(self, component, field_ids, manual_section_id=None):
        c, p = self.case, self.case.proc or {}
        if p.get("detour"):
            raise ToolError("a detour is already open: end_detour first")
        bad = [f for f in field_ids if not f.startswith("FR") or f not in c.sources]
        if not field_ids or bad:
            raise ToolError(f"field_ids must be FR ids from field_reports (bad: {bad})")
        off = [f for f in field_ids if "doesn't match the code's system" in c.sources[f]["label"]]
        if off:
            raise ToolError(f"{', '.join(off)}: the manual never links that part to this code; not a detour")
        if not manual_section_id:
            sid = kb.inspection_for(self.r, component, c.car.get("features") or None)
            if sid is None:
                self.store.audit(c, "detour", "not_offered", {"component": component}, "no manual inspection")
                return (f"No manual inspection for {component} on this car: no detour offered. Carry on with the "
                        "tree."), []
            manual_section_id = str(sid)
        sid = self._sid(manual_section_id)
        row = kb.section_row(self.r, sid)
        if not row:
            raise ToolError(f"no section {sid}")
        path, page, body, kind, _ = row
        words = [w for w in re.findall(r"[a-z0-9]{3,}", component.lower()) if w not in kb._STOPWORDS]
        is_test = re.search(r"inspect|inspection|test|check|diagnos", path, re.I)
        where = (path + " " + body[:1500]).lower()
        names_it = words and all(w in where for w in words)     # every word of the component, not just one
        if not (is_test and names_it):
            raise ToolError("that manual section isn't an inspection of this component: search_manual for the "
                            "component's inspection first (a field report never supplies the test itself)")
        src = self.case.add_source("section", f"{'CHARM' if kind == 'charm' else 'FSM'}"
                                   f"{' p.' + str(page) if page else ''}, {path}", text=body, sid=sid, page=page)
        why = "; ".join(c.sources[f]["label"] for f in field_ids)
        ok = self.human.confirm(f"Detour? {why} -> manual test: {path} [{src}]. Do it now? "
                                f"(The tree stays at step {p.get('current')}.)")
        self.store.audit(c, "detour", "offer", {"component": component, "field_ids": field_ids, "section": src},
                         "yes" if ok else "no")
        if not ok:
            return f"The user said no to the detour ({component}). Carry on with the tree.", [src]
        p["detour"] = {"component": component, "sid": sid, "src": src, "from": p.get("current"),
                       "field_ids": list(field_ids)}
        return (f"Detour accepted: {component}, manual test [{src}]. Use page_steps on it and ask_human from its "
                f"instructions; end_detour when done. The tree waits at step {p.get('current')}."), [src]

    def end_detour(self, found):
        p = self.case.proc or {}
        d = p.get("detour")
        if not d:
            raise ToolError("no detour is open")
        p.setdefault("detours", []).append(dict(d, found=found))
        p["detour"] = None
        self.store.audit(self.case, "detour", "end", {"component": d["component"]}, found)
        s = self.current()
        back = f"Back at step {s['n']} [{self.step_src(s)}]: {s['check']}" if s else "Back where the tree was left."
        return f"Detour on {d['component']} recorded: {found}. {back}", [d["src"]]

    def symptom_chart(self, query):
        c = self.case
        p = c.proc or {}
        if p.get("mode") == "symptom":               # already on a chart: keep it, don't start over
            return (f"Already on the chart [{p['chart_src']}] for \"{p['symptom']}\". Suspects in the manual's order: "
                    + "; ".join(f"{x['text']}" + (f" [{x['src']}]" if x["src"] else "") for x in p["suspects"][:8])
                    + ". Carry on with the next one."), []
        hits = kb.symptom_hits(self.r, query, k=6)
        stems = {kb._stem(w) for w in re.findall(r"[a-z0-9]{3,}", query.lower()) if w not in kb._STOPWORDS}
        cands = []
        for sid, path, page, row, sc in hits:
            for sym in kb.chart_symptoms(self.r, sid):
                words = {kb._stem(w) for w in re.findall(r"[a-z0-9]{3,}", sym["symptom"].lower())}
                score = len(words & stems) / max(1, len(stems))
                if score > 0 and all(sym["symptom"] != x[1]["symptom"] for x in cands):
                    cands.append((score, sym, sid, path, page))
        cands.sort(key=lambda x: -x[0])
        if not cands:
            return f"No symptom chart in this car's manual lists {query!r}. search_manual it instead.", []
        top = cands[:4]
        i = self.human.choose(f"The manual's symptom chart for \"{query}\": which one is it?",
                              [x[1]["symptom"] for x in top] + ["none of these"])
        self.store.audit(c, "human", "choose_symptom", {"query": query}, f"picked {i}")
        if i >= len(top):
            return "The user: none of the chart's symptoms fits. search_manual for another chart.", []
        _, sym, sid, path, page = top[i]
        chart = c.add_source("section", f"FSM{' p.' + str(page) if page else ''}, {path}, symptom {sym['n']}",
                             text=sym["symptom"] + ": " + "; ".join(x["text"] for x in sym["items"]), sid=sid, page=page)
        lines, ids, suspects = [f"[{chart}] {sym['symptom']} (rank A = very often, B = sometimes, C = rarely)"], [chart], []
        for it in sym["items"]:
            insp = it["ref_sid"] or kb.inspection_for(self.r, it["text"], c.car.get("features") or None)
            src = None
            if insp:
                row = kb.section_row(self.r, insp)
                if row:
                    src = c.add_source("section", f"{'CHARM' if row[3] == 'charm' else 'FSM'}"
                                       f"{' p.' + str(row[1]) if row[1] else ''}, {row[0]}", sid=insp, page=row[1])
                    ids.append(src)
            suspects.append({"text": it["text"], "rank": it["rank"], "src": src})
            lines.append(f"- {it['text']} [rank {it['rank'] or '?'}]" + (f" -> inspection [{src}]" if src else
                                                                        " -> no inspection section found"))
        c.proc = {"code": None, "via": None, "mode": "symptom", "variant": None, "current": None, "path": [],
                  "history": [], "symptom": sym["symptom"], "chart_src": chart, "suspects": suspects}
        lines.append("Walk the suspects in the chart's order: page_steps each inspection and give its instructions.")
        return "\n".join(lines), ids

    def _key(self, name):
        """A canonical telemetry key for a name the manual uses (or the key itself)."""
        name = (name or "").strip()
        tdb = self.case.car.get("telemetry_db")
        try:
            import sqlite3
            with sqlite3.connect(f"file:{tdb}?mode=ro", uri=True) as c:
                if c.execute("SELECT 1 FROM telem_param WHERE key=?", (name,)).fetchone():
                    return name
        except Exception:
            pass
        return live.param_for(name, tdb)

    def watch_live(self, params, seconds, instruction, source_id, trigger=None):
        c, p = self.case, self.case.proc or {}
        if source_id not in c.known_ids():
            raise ToolError(f"source_id {source_id} is not from this run")
        if not hasattr(self.car, "sample"):
            raise ToolError("no live connection in this run: ask the user instead")
        cable = getattr(self.car, "cable", "") or c.car.get("cable") or ""
        keys, dropped = [], []
        for name in params:
            k = self._key(name)
            (keys if k and live.readable(c.car, cable, k) else dropped).append(k or name)
        if not keys:
            raise ToolError(f"this car can't report {', '.join(map(str, dropped))} on the {cable or 'connected'} cable: "
                            "ask the user for the reading instead")
        trig = None
        if trigger:
            tk = self._key(trigger.get("param") or trigger.get("key"))
            if tk not in keys:
                raise ToolError("the trigger's value must be one of the watched values")
            trig = {"key": tk, "op": trigger.get("op"), "value": float(trigger.get("value")),
                    "for_s": float(trigger.get("for_s") or 0)}
        need = states_in(instruction)
        if need and need[0] != c.state_ok:
            ok = self.human.confirm(f"Before this: {STATE_LABEL[need[0]]}. Is the car in that state?")
            self.store.audit(c, "human", "confirm_state", {"state": need[0]}, "yes" if ok else "no")
            if not ok:
                return self._cant(self.current(), f"car not in the required state ({STATE_LABEL[need[0]]})")
            c.state_ok = need[-1]
        self.human.say(f"[{source_id}] {instruction}")
        samples, summary = watch.run(c.car, self.car, keys, float(seconds), trig, self.human)
        s = self.current()
        if s and p is not None:
            p.setdefault("watched", []).append(s["n"])     # this step's watch is done: no second automatic one
        wid = watch.store(self.store.db, c.id, s["n"] if s else None, keys, samples, summary)
        src = c.add_source("watch", f"watch session {wid}: {', '.join(keys)}", text=watch.text(summary))
        ids, lines = [src], [f"[{src}] watched {', '.join(keys)} for {samples[-1]['t'] if samples else 0:g} s "
                                 f"({len(samples)} samples)" + (f"; not readable here: {', '.join(map(str, dropped))}"
                                                                if dropped else ""), watch.text(summary)]
        for k, st in summary["keys"].items():
            if not st.get("n"):
                continue
            rid = c.add_reading(s["n"] if s else None, float(st["last"]), st["unit"],
                                f"{st['last']:g} {st['unit']} (watch {wid}, last of {st['n']})", f"watch_live {src}")
            ids.append(rid)
            z = st["zone"]
            if z["status"] in ("low", "high"):
                pl, pids = self.pathways(k, z["status"])
                lines.append(f"{k} {z['status']}: " + pl)
                ids += pids
        return "\n".join(lines), ids

    def pathways(self, param, direction):
        import telemetry_ref
        k = self._key(param)
        if not k:
            raise ToolError(f"no telemetry value named {param!r} for this car")
        rows = telemetry_ref.pathways(k, direction.strip().lower(), watch.tcar(self.case.car), k=4)
        if not rows:
            return f"No manual pathway stored for {k} {direction} on this car.", []
        ids, lines = [], []
        for x in rows:
            row = kb.section_row(self.r, x["section_id"])
            src = self.case.add_source("section", x["citation"], text=(x.get("evidence") or "")[:1000],
                                       sid=x["section_id"], page=row[1] if row else None)
            ids.append(src)
            lines.append(f"[{src}] {x['title']}" + (f" step {x['step']}" if x.get("step") else "")
                         + f" ({x['kind']}): \"{(x.get('evidence') or '')[:160]}\"")
        return "\n".join(lines), ids

    def common_issues(self):
        import common_issues
        car = self.case.car
        res = common_issues.gather(self.kb_path, car.get("model"), car.get("year"))
        if res.get("error"):
            return res["error"], []
        lines = res.get("lines", [])
        src = self.case.add_source("nhtsa", f"NHTSA/TSB/forum counts, {res.get('scope', car.get('model'))}",
                                   text="\n".join(lines))
        return f"[{src}] " + "\n".join(lines[:30]), [src]

    def show_figure(self, section_id):
        sid = self._sid(section_id)
        figs = kb.figures(self.r, sid, self.cfg["image_dirs"])
        if not figs:
            return f"Section {sid} has no figures in the DB.", []
        src = self.case.add_source("figure", f"figures of section {sid}", text=", ".join(n for n, _ in figs), sid=sid)
        self.human.show_figures(figs, src)
        return f"[{src}] shown to the user: " + ", ".join(n for n, _ in figs[:8]), [src]

    def page_steps(self, section_id):
        sid = self._sid(section_id)
        row = kb.section_row(self.r, sid)
        if not row:
            raise ToolError(f"no section {sid}")
        path, page, body, kind, _ = row
        self._alt_tried()
        units_ = kb.page_units(body)
        if not units_:
            return f"Section {sid} has no instructions to give one at a time (description only).", []
        base = f"{'CHARM' if kind == 'charm' else 'FSM'}{' p.' + str(page) if page else ''}, {path}"
        ids, lines = [], []
        for u in units_:
            src = self.case.add_source("section", f"{base}, instruction {u['n']}",
                                       text=u["text"] + (" " + u["question"] if u["question"] else ""), sid=sid, page=page)
            ids.append(src)
            lines.append(f"[{src}] {u['text']}" + (f" | question: {u['question']}" if u["question"] else ""))
        return "\n".join(lines), ids

    def read_car(self):
        res = self.car.read()
        c = self.case
        if res.get("codes") is not None:
            c.codes = list(dict.fromkeys(res["codes"]))     # scans replace codes, never append
            c.scan_ts = time.time()
        ids = [c.add_source("scan", f"scan {time.strftime('%H:%M:%S')}: codes {', '.join(c.codes) or 'none'}; "
                            f"{res.get('note', '')}", text=json.dumps(res))]
        out = [f"[{ids[0]}] Codes: {', '.join(c.codes) or 'none'}. {res.get('note', '')}"]
        if res.get("live"):
            import live_data
            lid = f"LV{len(c.live) + 1}"
            meaning = live_data.explain(res["live"])
            c.live.append({"id": lid, "ts": time.time(), "text": res["live"], "meanings": meaning})
            ids.append(lid)
            out.append(f"[{lid}] Live: {res['live']}")
            out += [f"  - {m}" for m in meaning]
        if c.scan_ts and not c.codes and not c.symptoms:
            said = self.human.ask_text("No trouble codes are stored. What does the car do, and when "
                                       "(cold or warm, idle or driving, all the time or now and then)?")
            self.store.audit(c, "human", "symptom", {}, said or "")
            if said and said.strip():
                c.symptoms = said.strip()
                out.append(f"No codes. The user's symptom: \"{c.symptoms}\". Use symptom_chart with it.")
            else:
                out.append(f"No codes and no symptom given. To ask the user again, ask_human expect text with "
                           f"source_id {ids[0]}.")
        p = c.proc or {}
        if c.scan_ts and p.get("mode") == "steps" and p.get("current") is not None and not p.get("broken"):
            out.append(self._enter())               # a waiting "which DTCs are set" step answers itself now
        return "\n".join(out), ids

    def update_hypothesis(self, cause, status, evidence_ids, general_knowledge=False):
        status = status.strip().lower().replace(" ", "_").replace("-", "_")
        if status not in ("open", "supported", "ruled_out"):
            raise ToolError("status must be open, supported or ruled_out")
        known = self.case.known_ids()
        bad = [e for e in evidence_ids if e not in known]
        if bad:
            raise ToolError(f"unknown evidence ids {bad}")
        if not evidence_ids and not general_knowledge:
            raise ToolError("give evidence ids, or set general_knowledge=true (shown as unverified)")
        if status != "open" and not any(e.startswith(("R", "LV", "SC")) for e in evidence_ids):
            raise ToolError("supported / ruled_out needs a reading or scan id among the evidence")
        for h in self.case.hypotheses:
            if h["cause"].lower() == cause.strip().lower():
                h.update(status=status, evidence=list(evidence_ids), general=bool(general_knowledge), ts=time.time())
                break
        else:
            self.case.hypotheses.append({"cause": cause.strip(), "status": status, "evidence": list(evidence_ids),
                                         "general": bool(general_knowledge), "ts": time.time()})
        fr = [self.case.sources[e]["label"] for e in evidence_ids if e.startswith("FR")]
        if fr:
            for h in self.case.hypotheses:
                if h["cause"].lower() == cause.strip().lower():
                    h["field"] = fr
        tag = (" (general knowledge, unverified)" if general_knowledge else "") + (f" (field: {'; '.join(fr)})" if fr else "")
        return f"Board: {cause.strip()}{tag} -> {status} [{', '.join(evidence_ids)}]", list(evidence_ids)

    def finish(self, result, summary, citations, settle_test=""):
        c, p = self.case, self.case.proc
        result = result.strip().lower()
        if result not in ("diagnosis", "unconfirmed"):
            raise ToolError("result must be diagnosis or unconfirmed")
        known = c.known_ids()
        bad = [x for x in citations if x not in known]
        nothing_found = result == "unconfirmed" and not citations and not known
        if (not citations and not nothing_found) or bad:
            raise ToolError(f"citations must be ids from this run (unknown: {bad})" if bad else "cite at least one id")
        if result == "diagnosis":
            walked = p.get("mode") == "steps" and p.get("ended") and not p.get("unconfirmed") and p.get("path")
            backed = any(h["status"] == "supported" and not h["general"]
                         and any(e.startswith("R") for e in h["evidence"])
                         and any(e.startswith("M") for e in h["evidence"]) for h in c.hypotheses)
            if not (walked or (p.get("mode") == "pages" and backed)):
                s = self.current() if p.get("mode") == "steps" and not p.get("broken") else None
                if s and not p.get("unconfirmed"):
                    raise ToolError(f"the tree hasn't ended yet: it is at step {s['n']} [{self.step_src(s)}]; ask_human for it")
                raise ToolError("not a confirmed diagnosis: the manual path didn't reach its end on real answers"
                                + (" (unconfirmed steps: " + ", ".join(str(u["step"]) for u in p.get("unconfirmed", []))
                                   + ")" if p.get("unconfirmed") else "") + ". Finish 'unconfirmed' with settle_test.")
            if walked and p.get("end_src") not in citations:
                raise ToolError(f"cite the step where the manual's tree ended: add {p.get('end_src')} to citations")
        elif not settle_test.strip():
            raise ToolError("unconfirmed needs settle_test: the manual check still needed")
        elif p.get("mode") == "steps" and not p.get("ended") and not p.get("broken") and self.current() \
                and not p.get("unconfirmed"):
            s = self.current()
            raise ToolError(f"the tree is still open at step {s['n']} [{self.step_src(s)}]: ask_human for it "
                            "(finish when it ends, or when a step can't be done)")
        else:
            if any(not u.get("alt") for u in p.get("unconfirmed", [])):
                raise ToolError("a step couldn't be done and no other manual path was looked at yet: try "
                                "search_manual / read_section / page_steps first")
            if p.get("broken") and not p.get("broken_shown"):
                raise ToolError(f"the walk stopped on a broken/missing step: show its page first "
                                f"(read_section {p.get('broken_src')})")
        c.status = "finished"
        c.result = {"result": result, "summary": summary, "citations": list(citations), "settle_test": settle_test,
                    "end_text": p.get("end_text") if p else None, "ts": time.time()}
        return f"Finished: {result}.", list(citations)


def units_cut(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"
