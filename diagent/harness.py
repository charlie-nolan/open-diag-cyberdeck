"""The agent loop: plain Python over Ollama's /api/chat with tools (no agent framework, easy to audit).

Each turn the model gets the case summary (not the chat so far) plus the current step and picks a tool call.
The harness runs it, records it, and repeats, up to max_calls_per_turn calls before it must ask the user
something or finish. Text the model writes (instructions, board entries, the final summary) is checked against
this run's tool results: a number with a unit, a step number, a DTC or a source id that no tool returned gets
the message rejected and the model asked again."""
import json
import re
import time
import urllib.error
import urllib.request

from . import tools as T
from .tools import ToolError

SYSTEM = """You guide a diagnosis of the car described in the case summary. The user is at the car and does
the hands-on work. The factory manual's diagnostic procedure is the plan.
- Work only on the CURRENT STEP in the case summary. Don't skip steps or make your own plan. Python answers
  the step's check from the user's reply and moves to the next step. Never compare or calculate numbers yourself.
- The walk loop: ask_human for the current step; the result names the next current step; repeat. Finish only
  when the summary says TREE ENDED (finish 'diagnosis', with the end step's id in citations) or WALK STOPPED,
  or after a step can't be done and another manual path was looked at (finish 'unconfirmed').
- Steps that ask which DTCs are set are answered by Python from the scan: call read_car, don't ask the user.
- With no scan codes, work from symptom_chart with the user's symptom; get_procedure only takes a code that was
  scanned or that a manual page of this case names.
- An instruction the user answered (done, can't, skip) is finished: go on to the next one, don't repeat it.
- Outside a step table, ask_human with expect yesno or number needs question= quoting the cited text's own
  question; if the text asks nothing, use expect text or done.
- To have the user do something, call ask_human with one short instruction in plain sentences, worded from the
  current step's own text (its connectors, terminals, ignition/engine state and meter setting). Pass the
  step's M id as source_id.
- Every value, spec, step number and claim you write must come from a tool result in this case, with that
  result's id in square brackets. Anything else is rejected.
- If a step can't be done, is skipped, broken or missing, look for another manual path (search_manual,
  read_section) or finish 'unconfirmed' naming the manual check still needed. Never guess a result.
- Candidate causes come from the manual or retrieved pages. Anything else needs general_knowledge=true.
- You can't write to the car. A step that needs an actuator test or a clear is done by the user from the main
  app's TESTS screen; ask them for the result.
- Reply with exactly one tool call each time."""

UNIT_RE = (r"(?:mV|V|kΩ|MΩ|Ω|k ?ohms?|M ?ohms?|ohms?|mA|A|kPa|psi|mmHg|inHg|%|rpm|°C|°F|N·m|Nm|ft-lbs?|in-lbs?|"
           r"mm|ms|Hz|g/s|hours?|h)")
CLAIM_NUM = re.compile(rf"((?<![\d.])[+-]?\d+(?:[.,]\d+)?)\s*{UNIT_RE}(?![A-Za-z])")   # '284-314 kPa' is a range
PRICE = re.compile(r"\$\s?(\d+(?:\.\d+)?)")
STEP_REF = re.compile(r"\bsteps? (\d+)", re.I)
CODE = re.compile(r"\b[PBCU][0-9A-F]{4}\b")
ID_REF = re.compile(r"\[([A-Z]{1,2}\d+)\]")
FLOAT = re.compile(r"(?<![\d.])[+-]?\d+(?:[.,]\d+)?")


def _floats(text):
    out = set()
    for m in FLOAT.finditer(text):
        try:
            out.add(round(float(m.group(0).replace(",", ".")), 6))
        except ValueError:
            pass
    return out


def check_claims(text, case, tools, store):
    """Problems with text, [] when every number+unit, step, code and id in it comes from this run."""
    if not text:
        return []
    corpus = "\n".join([store.audit_text(case.id), case.symptoms]
                       + [str(r["raw"]) for r in case.readings]
                       + [v["text"] for v in case.sources.values()]     # full text of every page/step/spec cited so far
                       + [" ".join((s["step"], s["check"], s["yes"], s["no"])) for s in tools.steps().values()])
    nums = _floats(corpus) | {round(float(r["value"]), 6) for r in case.readings
                              if isinstance(r["value"], float) and r["value"] != float("inf")}
    probs = []
    for m in list(CLAIM_NUM.finditer(text)) + list(PRICE.finditer(text)):
        v = round(float(m.group(1).replace(",", ".")), 6)
        if v not in nums:
            probs.append(f"'{m.group(0).strip()}' is not in any tool result or reading of this case")
    steps = set(tools.steps())
    for h in (case.proc or {}).get("history", []):
        steps |= {e["step"] for e in h.get("path", [])}
    quoted = {int(n) for n in STEP_REF.findall(corpus)}    # 'go to step 8' in the manual's own text counts
    for m in STEP_REF.finditer(text):
        if steps and int(m.group(1)) not in steps | quoted:
            probs.append(f"step {m.group(1)} is not a step of the procedure")
    for code in set(CODE.findall(text)):
        if code not in case.codes and code not in corpus:
            probs.append(f"{code} is neither a scanned code nor in a tool result")
    known = case.known_ids()
    for m in ID_REF.finditer(text):
        if m.group(1) not in known:
            probs.append(f"[{m.group(1)}] is not a source id from this case")
    return probs


CHECKED_ARGS = {"ask_human": ["instruction", "question"], "watch_live": ["instruction"], "finish": ["summary", "settle_test"],
                "update_hypothesis": ["cause"], "record_step": ["why"]}


def summary(case, tools):
    c, p = case, case.proc or {}
    car = c.car
    lines = [f"CASE {c.id}: {car.get('year') or ''} {car.get('make')} {car.get('model') or ''} "
             f"{car.get('engine') or ''}".strip(),
             f"Scan codes: {', '.join(c.codes) or 'none'}" + (f" | Symptoms (The user): {c.symptoms}" if c.symptoms else "")]
    if not p.get("code"):
        lines.append("PROCEDURE: none yet. " + ("Start with get_procedure for the scanned code." if c.codes else
                                                "No codes: symptom_chart with the user's symptom." if c.symptoms else
                                                "No codes: read_car."))
    else:
        if p.get("mode") == "symptom":
            lines.append(f"SYMPTOM CHART [{p['chart_src']}]: {p['symptom']}. Suspects in the manual's order: "
                         + "; ".join(f"{x['text']} (rank {x['rank'] or '?'}" + (f", inspection [{x['src']}])" if x["src"]
                                                                              else ", no inspection found)")
                                     for x in p["suspects"][:10]))
        if p.get("code"):
            lines.append(f"PROCEDURE: {p['code']}" + (f" (via {p['via']})" if p.get("via") else "")
                         + f", {'step table ' + p['variant'] if p['mode'] == 'steps' else 'manual pages only (no usable step table)'}")
        s = tools.current()
        if p.get("broken"):
            lines.append(f"WALK STOPPED: {p['broken']} [{p.get('broken_src')}]")
        elif p.get("ended"):
            lines.append(f"TREE ENDED: \"{p.get('end_text')}\" [{p.get('end_src')}]")
        elif s:
            lines.append(f"CURRENT STEP {s['n']} [{tools.step_src(s)}]: {s['step']}\n  Check: {s['check']}\n"
                         f"  Yes: {s['yes']}\n  No: {s['no']}")
        if p.get("path"):
            lines.append("PATH: " + " -> ".join(f"{e['step']} {e['answer']} ({e['how'][:40]}"
                                                 f"{', ' + ','.join(e['readings']) if e['readings'] else ''})"
                                                 for e in p["path"]))
        if p.get("unconfirmed"):
            lines.append("UNCONFIRMED: " + "; ".join(f"step {u['step']} [{u['src']}]: {u['why']}" for u in p["unconfirmed"]))
    if c.readings:
        lines.append("READINGS: " + "; ".join(f"[{r['id']}] step {r['step']}: {r['raw']}" for r in c.readings[-12:]))
    if c.live:
        lv = c.live[-1]
        lines.append(f"LIVE [{lv['id']}]: {lv['text']}\n  " + "\n  ".join(lv["meanings"]))
    if c.hypotheses:
        lines.append("BOARD: " + "; ".join(f"{h['cause']}{' (general knowledge, unverified)' if h['general'] else ''}: "
                                           f"{h['status']} [{', '.join(h['evidence'])}]" for h in c.hypotheses))
    recent = list(c.sources.items())[-14:]
    if recent:
        lines.append("SOURCES (latest): " + "; ".join(f"[{k}] {v['label'][:90]}" for k, v in recent))
    lines.append("Pick ONE tool call.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------- models
class OllamaChat:
    def __init__(self, url, model, num_ctx=16384, temperature=0.2, think=False, timeout=600):
        self.url, self.model, self.num_ctx, self.temperature, self.think, self.timeout = \
            url.rstrip("/"), model, num_ctx, temperature, think, timeout
        self.name = f"ollama:{model}"

    def chat(self, messages, tools):
        body = {"model": self.model, "messages": messages, "tools": tools, "stream": False, "think": self.think,
                "options": {"num_ctx": self.num_ctx, "temperature": self.temperature}}
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            msg = json.loads(r.read())["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            f = tc.get("function", {})
            args = f.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_unparsed": args}
            calls.append({"name": f.get("name"), "arguments": args})
        return {"content": msg.get("content") or "", "tool_calls": calls, "message": msg}


def ollama_has(url, model, timeout=3):
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as r:
            names = {m["name"] for m in json.loads(r.read()).get("models", [])}
        return model in names
    except (OSError, ValueError, urllib.error.URLError):
        return None   # unreachable


# ---------------------------------------------------------------------------- the loop
def run(case, tools, model, human, store, cfg, log=print):
    cap, wall = cfg["max_calls_per_turn"], cfg["agent_wall_s"]
    agent_s = 0.0
    while case.status == "open":
        case.turn += 1
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": summary(case, tools)}]
        calls = 0
        while case.status == "open":
            t0, w0 = time.time(), human.wait_s
            allowed = None if calls < cap else ["ask_human", "finish"] + (
                ["end_detour"] if (case.proc or {}).get("detour") else [])
            if calls == cap:
                msgs.append({"role": "user", "content": f"{cap} tool calls this turn: now either ask_human or finish"
                             + (" (or end_detour)." if (case.proc or {}).get("detour") else ".")})
            try:
                resp = model.chat(msgs, T.ollama_tools(allowed))
            except (OSError, urllib.error.URLError, ValueError) as e:
                store.audit(case, "error", "model", {}, f"model call failed: {e}", ok=False)
                case.status = "stopped"
                case.result = {"result": "stopped", "summary": f"model unreachable: {e}"}
                break
            calls += 1
            msgs.append(resp.get("message") or {"role": "assistant", "content": resp["content"],
                                                "tool_calls": [{"function": c} for c in resp["tool_calls"]]})
            end_turn = False
            if not resp["tool_calls"]:
                probs = check_claims(resp["content"], case, tools, store)
                if probs:
                    store.audit(case, "reject", "text", {"content": resp["content"]}, "; ".join(probs), ok=False)
                    msgs.append({"role": "user", "content": "REJECTED (uncited): " + "; ".join(probs)
                                 + ". Use a tool; cite ids from tool results."})
                else:
                    store.audit(case, "note", "text", {"content": resp["content"]}, resp["content"])
                    if resp["content"].strip():
                        human.say(resp["content"])
                    msgs.append({"role": "user", "content": "Reply with a tool call."})
            for call in resp["tool_calls"][:1]:     # one call per step; extras are dropped and said so
                name, args = call["name"], call["arguments"] or {}
                result, ok = execute(name, args, allowed, case, tools, store)
                log(f"  [{case.turn}.{calls}] {name}({_short(args)}) -> {'ok' if ok else 'REJECTED'}: {_short(result, 160)}")
                left = cap - calls
                if 0 < left <= 3:
                    result += f"\n({left} tool call{'s' if left > 1 else ''} left this turn; then ask_human or finish.)"
                if len(resp["tool_calls"]) > 1:
                    result += f"\n(Only the first of your {len(resp['tool_calls'])} calls ran: one call at a time.)"
                msgs.append({"role": "tool", "content": result, "tool_name": name})
                end_turn = ok and name in ("ask_human", "finish")
            store.save(case)
            agent_s += (time.time() - t0) - (human.wait_s - w0)
            if agent_s > wall:
                case.status = "timeout"
                case.result = {"result": "timeout", "summary": f"agent time cap {wall} s reached"}
            elif calls >= cap + 3:
                case.status = "stalled"
                case.result = {"result": "stalled", "summary": f"no ask_human/finish after {calls} calls"}
            if end_turn:
                break
        store.save(case)
    return case


def execute(name, args, allowed, case, tools, store):
    """(result text, ok). Unknown tools, bad arguments and uncited text are rejected, never run."""
    if name not in {s[0] for s in T.SCHEMAS} or (allowed and name not in allowed):
        msg = f"REJECTED: tool {name!r} isn't available now" + (f" (allowed: {', '.join(allowed)})" if allowed else "")
        store.audit(case, "reject", name, args, msg, ok=False)
        return msg, False
    probs = []
    for k in CHECKED_ARGS.get(name, []):
        probs += check_claims(str(args.get(k) or ""), case, tools, store)
    if name == "watch_live" and isinstance(args.get("trigger"), dict) and args["trigger"].get("value") is not None:
        try:                               # a trigger's value has to be one a tool result of this case gave
            v = round(float(args["trigger"]["value"]), 6)
            corpus = store.audit_text(case.id) + " " + " ".join(x["text"] for x in case.sources.values()) + " " + \
                " ".join(" ".join((s["step"], s["check"])) for s in tools.steps().values())
            if v not in _floats(corpus):
                probs.append(f"trigger value {v:g} is not in any tool result of this case")
        except (TypeError, ValueError):
            probs.append("trigger value isn't a number")
    if probs:
        msg = "REJECTED (uncited): " + "; ".join(probs) + ". Only use values, steps and ids from tool results."
        store.audit(case, "reject", name, args, msg, ok=False)
        return msg, False
    fn = getattr(tools, name)
    want = {s[0]: s[2] for s in T.SCHEMAS}[name]
    kwargs = {k: v for k, v in args.items() if k in want}
    req = {s[0]: s[3] for s in T.SCHEMAS}[name]
    missing = [k for k in req if kwargs.get(k) in (None, "", [])]
    if missing:
        msg = f"ERROR: {name} needs {', '.join(missing)} (required: {', '.join(req)})"
        store.audit(case, "reject", name, args, msg, ok=False)
        return msg, False
    try:
        result, ids = fn(**kwargs)
    except (ToolError, TypeError, ValueError, KeyError) as e:
        msg = f"ERROR: {e}"
        store.audit(case, "reject", name, args, msg, ok=False)
        return msg, False
    store.audit(case, "tool", name, args, result, ids, ok=True)
    return result, True


def _short(x, n=90):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
