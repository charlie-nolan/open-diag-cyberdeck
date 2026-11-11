"""Team mode runner, no deck needed: one case through the local baseline and the cloud team, with a full trace.

    venv/bin/python team/run.py --case case.json [--stage final|brainstorm] [--question "..."] [--web on|off]
                                [--cloud-manual] [--no-baseline] [--out logs/team/<id>]

case.json is the main app's case: the Session keys (dtcs, telemetry, symptoms, ruled_out, suspect, history,
chat_notes, saved_debate, drive_log, ...) plus "vehicle_info" {make, model, year, engine}. A "vin" anywhere is
dropped before anything leaves the PC, and the run refuses to start if a 17-character VIN is still in the payload.

Final stage (handoff Part 2, steps 3-6):
  baseline   Home mode's own verdict (same case summary, manual pack and prompt as the app) on the PC's Ollama,
             skipped when Ollama can't answer or the GPU is booked
  diagnose   Gemini and GPT-OSS (agy, sandboxed) each write a full diagnosis with their own lookups, in parallel
  crosscheck each sees the others' diagnoses and the baseline: what it agrees with, what's wrong, which test first
  verdict    Claude (claude -p, read-only tools) weighs it all against the manual and writes the app's format
             plus TEAM NOTES
  checks     cited ids must exist in this run's evidence; faithfulness.check against the case + every looked-up item

Everything (prompts, replies, lookups, tokens, times) goes to <out>/: trace.jsonl, evidence.jsonl, lookups.jsonl,
verdict.txt, baseline.txt, report.json. Progress lines go to stdout (and <out>/progress.log) for the service to poll.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "app"), os.path.join(ROOT, "kb")]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import prompts  # noqa: E402

PY = os.path.join(ROOT, "venv", "bin", "python")
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
ID_RE = re.compile(r"\[((?:[MSCOWYPB]\d+(?: t=\d+s)?)(?:\s*,\s*[MSCOWYPB]\d+(?: t=\d+s)?)*)\]")  # Y3 t=95s: a video moment
MEMBERS = {"gemini": "gemini-4-argon-medium", "gptoss": "gpt-oss-120b-medium"}
LIMITS = {"ask_fast": 150, "final": 15 * 60, "brainstorm": 6 * 60, "agy_call": 7 * 60, "crosscheck": 4 * 60, "claude_call": 5 * 60,
          "baseline": 6 * 60}
OLLAMA = "http://127.0.0.1:11434"
CLOUD_KEYS = ("dtcs", "telemetry", "mode06", "symptoms", "ruled_out", "suspect", "history", "intake_summary",
              "intake_note", "chat_notes", "saved_debate", "saved_wildcard", "drive_log", "chart_reads",
              "maint_selected", "past_readings", "earlier_visits", "board")


class Run:
    def __init__(self, out):
        self.out = out = os.path.abspath(out)
        os.makedirs(out, exist_ok=True)
        self.t0 = time.time()
        self.usage = {}

    def progress(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(os.path.join(self.out, "progress.log"), "a") as f:
            f.write(line + "\n")

    def trace(self, event, **kw):
        with open(os.path.join(self.out, "trace.jsonl"), "a") as f:
            f.write(json.dumps(dict(t=round(time.time() - self.t0, 1), event=event, **kw)) + "\n")

    def add_usage(self, who, **kw):
        u = self.usage.setdefault(who, {"calls": 0, "seconds": 0.0})
        u["calls"] += 1
        for k, v in kw.items():
            if isinstance(v, (int, float)):
                u[k] = round(u.get(k, 0) + v, 4)


# ------------------------------------------------------------------ case and privacy
def scrub(case):
    """The case without VIN/plate/customer fields. Raises if a VIN pattern survives in any value."""
    c = {k: v for k, v in case.items() if k in CLOUD_KEYS and v}
    v = case.get("vehicle_info") or {}
    c["vehicle_info"] = {k: v.get(k) for k in ("make", "model", "year", "engine", "transmission") if v.get(k)}
    for k, val in list(c.items()):
        if isinstance(val, str):
            c[k] = VIN_RE.sub("[VIN removed]", val)
    if VIN_RE.search(json.dumps(c)):
        raise ValueError("a VIN-like value is still in the payload; refusing to send")
    return c


def case_text(case, kb_path):
    """The app's own case summary (cyberdeck_gui.Session.case_summary on a scratch session), in Home mode's room."""
    import cyberdeck_gui as G
    G.CFG["kb_db"] = kb_path
    G.ACTIVE_MGR = type("PC", (), {"home": True, "pc_why": ""})()   # Home mode budgets (PC context)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    s = G.Session(tmp)
    v = case["vehicle_info"]
    s.set("vehicle", " ".join(str(v[k]) for k in ("year", "make", "model", "engine") if v.get(k)))
    s.set("vehicle_info", json.dumps(v))
    for k, val in case.items():
        if k != "vehicle_info":
            s.set(k, val if isinstance(val, str) else json.dumps(val))
    text = s.case_summary()
    os.unlink(tmp)
    return text


def evidence_pack(case_summary, v, kb_path):
    """Home mode's manual pack for the verdict: case_context(r, case, 3500 x ctx_factor), as the app does."""
    import cyberdeck_gui as G
    cfg = dict(G.CFG, kb_db=kb_path, kb_model=v.get("model"), kb_year=v.get("year"), kb_engine=v.get("engine"),
               make=v.get("make"), home=True)
    r = G.make_retriever(cfg)
    return G.case_context(r, case_summary, int(3500 * G.ctx_factor()))


# ------------------------------------------------------------------ members
def start_kb(run, v, codes, cloud_manual, web):
    args = [PY, os.path.join(HERE, "kb_mcp.py"), "--run", run.out, "--make", v["make"], "--codes", " ".join(codes)]
    for k in ("model", "year", "engine"):
        if v.get(k):
            args += [f"--{k}", str(v[k])]
    if cloud_manual:
        args.append("--cloud-manual")
    if not web:
        args.append("--no-web")
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=open(os.path.join(run.out, "kb_mcp.err"), "w"),
                         text=True, start_new_session=True)
    line = p.stdout.readline()
    if not line.startswith("PORT "):
        raise RuntimeError("kb MCP server didn't start (see kb_mcp.err)")
    return p, int(line.split()[1])


def call_agy(run, who, prompt, port, web, timeout=LIMITS["agy_call"]):
    """One sandboxed agy call (see research/team_probe.md). Returns the reply text ('' on failure)."""
    work = os.path.join(run.out, "work", who)
    gcfg = os.path.join(run.out, "gcfg", who)
    os.makedirs(work, exist_ok=True)
    if not os.path.isdir(gcfg):     # agy's own config, minus Charlie's MCP list (it writes a projects/ dir here)
        shutil.copytree(os.path.expanduser("~/.gemini/config"), gcfg,
                        ignore=shutil.ignore_patterns("mcp_config.json", "projects"))
    with open(os.path.join(gcfg, "mcp_config.json"), "w") as f:
        json.dump({"mcpServers": {"teamkb": {"disabled": False,
                                             "serverUrl": f"http://127.0.0.1:{port}/mcp/cloud/{who}"}}}, f)
    allow = ["mcp(teamkb/*)", "command(*)", f"read_file({work})", f"write_file({work})"]
    deny = []
    if web:
        allow.append("url(*)")
    else:
        deny.append("url(*)")
    home = os.path.expanduser("~")
    cmd = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", home,
           "--tmpfs", "/mnt", "--tmpfs", "/run/media",
           "--ro-bind", f"{home}/.local/bin", f"{home}/.local/bin", "--bind", f"{home}/.gemini", f"{home}/.gemini",
           "--bind", gcfg, f"{home}/.gemini/config", "--bind", work, work, "--chdir", work, "--die-with-parent",
           "agy", "-p", "Read the file task.md in the current folder and do what it says. Reply with your answer only.",
           "--model", MEMBERS[who], "--settings", json.dumps({"permissions": {"allow": allow, "deny": deny}}),
           "--output-format", "json", "--print-timeout", f"{timeout - 20}s"]
    with open(os.path.join(work, "task.md"), "w") as f:      # big inputs go in a file (128 KB argument limit)
        f.write(prompt)
    t = time.time()
    for attempt in (1, 2):
        try:
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
            d = json.loads(p.stdout or "{}")
        except (subprocess.TimeoutExpired, ValueError) as e:
            d = {"status": "ERROR", "error": repr(e)[:300]}
        busy = "No capacity" in (p.stderr if "p" in locals() else "") or "UNAVAILABLE" in str(d.get("error", ""))
        if d.get("status") == "SUCCESS" or not busy or attempt == 2 or time.time() - t > timeout / 2:
            break
        run.trace("agy_retry", who=who, why="model busy (503)")
        time.sleep(60)
    u = d.get("usage") or {}
    run.add_usage(who, seconds=time.time() - t, input_tokens=u.get("input_tokens", 0),
                  output_tokens=u.get("output_tokens", 0), thinking_tokens=u.get("thinking_tokens", 0),
                  cache_read_tokens=u.get("cache_read_tokens", 0))
    text = d.get("response", "") or ""
    if text and d.get("status") != "SUCCESS":   # print timeout: keep what it wrote, marked
        text = "[PARTIAL: this member ran out of time]\n" + text
    run.trace("agy", who=who, model=MEMBERS[who], status=d.get("status"), seconds=round(time.time() - t, 1),
              usage=u, prompt=prompt, reply=text, error=d.get("error") or (p.stderr[-800:] if not text and
                                                                               'p' in locals() else ""))
    for name in os.listdir(work):              # scripts and notes a member left are kept in the trace, then deleted
        if name != "task.md":
            run.trace("agy_file", who=who, name=name)
    shutil.rmtree(work, ignore_errors=True)
    return text


def call_claude(run, prompt, port, web, model, timeout=LIMITS["claude_call"]):
    work = os.path.join(run.out, "work", "claude")
    os.makedirs(work, exist_ok=True)
    mcp = os.path.join(run.out, "claude_mcp.json")
    with open(mcp, "w") as f:
        json.dump({"mcpServers": {"teamkb": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp/full/claude"}}}, f)
    tools = ["WebSearch"] if web else []
    cmd = ["claude", "-p", "--mcp-config", mcp, "--strict-mcp-config", "--tools", ",".join(tools) or "",
           "--allowedTools", "mcp__teamkb__*", *tools, "--setting-sources", "", "--no-session-persistence",
           "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    t = time.time()
    try:
        p = subprocess.run(cmd, input=prompt, cwd=work, capture_output=True, text=True, timeout=timeout)
        d = json.loads(p.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError) as e:
        d = {"subtype": "error", "result": "", "error": repr(e)[:300]}
    u = d.get("usage") or {}
    run.add_usage("claude", seconds=time.time() - t, input_tokens=u.get("input_tokens", 0),
                  output_tokens=u.get("output_tokens", 0), cache_read_tokens=u.get("cache_read_input_tokens", 0),
                  cache_write_tokens=u.get("cache_creation_input_tokens", 0), cost_usd_equiv=d.get("total_cost_usd", 0))
    text = d.get("result", "") if d.get("subtype") == "success" else ""
    run.trace("claude", status=d.get("subtype"), seconds=round(time.time() - t, 1), usage=u, prompt=prompt,
              reply=text, turns=d.get("num_turns"), error=d.get("error", "") or (p.stderr[-800:] if not text and
                                                                                  "p" in locals() else ""))
    shutil.rmtree(work, ignore_errors=True)
    return text


def _mcp(port, who, method, params=None, timeout=120):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp/cloud/{who}", headers={"Content-Type": "application/json"},
                                 data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                  "params": params or {}}).encode())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def call_local(run, prompt, port, model, timeout=LIMITS["agy_call"], max_tools=10):
    """The local model as a team member: the same teamkb tools (cloud profile, same cap) through Ollama tool calls.
    '' when the GPU is booked or Ollama fails."""
    why = gpu_booked()
    if why:
        run.trace("local", status="skipped", error=why)
        return ""
    import cyberdeck_gui as G
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["inputSchema"]}}
             for t in _mcp(port, "local", "tools/list")["tools"] if t["name"] != "fetch_page"]
    msgs = [{"role": "system", "content": G.DEEP_SYSTEM}, {"role": "user", "content": prompt}]
    t, calls, text, u = time.time(), 0, "", {"input_tokens": 0, "output_tokens": 0}
    try:
        while time.time() - t < timeout:
            body = {"model": model, "stream": False, "think": False, "messages": msgs,
                    "tools": tools if calls < max_tools else [],
                    "options": {"temperature": 0.1, "num_ctx": G.CFG["home_context_len"], "num_predict": 2200}}
            req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=max(30, timeout - (time.time() - t))) as r:
                d = json.load(r)
            u["input_tokens"] += d.get("prompt_eval_count", 0)
            u["output_tokens"] += d.get("eval_count", 0)
            m = d.get("message") or {}
            msgs.append(m)
            if not m.get("tool_calls"):
                text = m.get("content", "")
                break
            for tc in m["tool_calls"]:
                f = tc.get("function", {})
                calls += 1
                res = _mcp(port, "local", "tools/call", {"name": f.get("name"), "arguments": f.get("arguments") or {}})
                msgs.append({"role": "tool", "content": res["content"][0]["text"][:12000], "tool_name": f.get("name")})
    except Exception as e:
        run.trace("local", status="error", error=repr(e)[:300])
    run.add_usage("local_member", seconds=time.time() - t, **u)
    run.trace("local", model=model, seconds=round(time.time() - t, 1), tool_calls=calls, usage=u, prompt=prompt,
              reply=text)
    return text


def lookup_log(run):
    """TEAM NOTES' lookup list, written by the deck from lookups.jsonl (not by a model)."""
    p = os.path.join(run.out, "lookups.jsonl")
    if not os.path.isfile(p):
        return ""
    lines = []
    for d in map(json.loads, open(p)):
        args = ", ".join(f"{k}={str(v)[:70]}" for k, v in d.get("args", {}).items())
        res = (f"refused: {d['refused']}" if d.get("refused") else f"error: {d['error'][:80]}" if d.get("error")
               else (", ".join(d.get("ids") or []) or "nothing found"))
        lines.append(f"- {d.get('who')}: {d.get('tool')}({args}) -> {res}")
    used = {}
    for it in evidence(run).values():
        if it["kind"] in "MSCO" and it.get("by") not in ("claude", "runner"):
            used[it["by"]] = used.get(it["by"], 0) + len(it["text"])
    cap = ", ".join(f"{w} {n // 1000}k" for w, n in sorted(used.items()))
    return ("LOOKUPS (logged by the deck, every member's every lookup):\n" + "\n".join(lines)
            + (f"\nDB text given out per member (cap about 40k each): {cap}" if cap else ""))


def mark_bad_ids(text, ev):
    """Cited ids the run never gave out are marked in place (never deleted), like the app's source check."""
    def fix(m):
        ids = re.split(r"\s*,\s*", m.group(1))
        return "[" + ", ".join(i if i.split()[0] in ev else f"{i}: NOT A SOURCE FROM THIS RUN" for i in ids) + "]"
    return ID_RE.sub(fix, text)


def gpu_booked():
    """Why the GPU may not be used now ('' when free): tools/gpu_guard.sh's gpu_free, except that a web browser's
    GPU process isn't a job (gpu_guard skips chrome/firefox by name but not brave, so Brave read as a busy GPU)."""
    script = (f"source {ROOT}/tools/gpu_guard.sh; "
              "gpu_jobs() { nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits "
              "2>/dev/null | awk -F', ' '$3 > 400 && $2 !~ /kwin|Xorg|claude|chrome|chromium|brave|firefox|ollama/ "
              "{print $1\" \"$2\" (\"$3\" MiB)\"}'; }; gpu_free")
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return "" if p.returncode == 0 else (p.stdout.strip()[:300] or "GPU busy")


def baseline(run, prompt, model):
    """Home mode's verdict on the PC's Ollama (deep role settings). '' plus why when it can't run."""
    why = gpu_booked()
    if why:
        return "", why
    import cyberdeck_gui as G
    body = {"model": model, "stream": False, "think": False,
            "messages": [{"role": "system", "content": G.DEEP_SYSTEM}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.1, "num_ctx": G.CFG["home_context_len"], "num_predict": 2200}}
    t = time.time()
    try:
        req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LIMITS["baseline"]) as r:
            d = json.load(r)
    except Exception as e:
        run.trace("baseline", error=repr(e)[:300])
        return "", f"Ollama didn't answer: {e}"
    text = (d.get("message") or {}).get("content", "")
    run.add_usage("local", seconds=time.time() - t, input_tokens=d.get("prompt_eval_count", 0),
                  output_tokens=d.get("eval_count", 0))
    run.trace("baseline", model=model, seconds=round(time.time() - t, 1), prompt=prompt, reply=text)
    return text, ""


# ------------------------------------------------------------------ checks
def evidence(run):
    p = os.path.join(run.out, "evidence.jsonl")
    if not os.path.isfile(p):
        return {}
    return {it["id"]: it for it in map(json.loads, open(p))}


def cited_text(ev, texts, skip=(), each=1500, total=24000):
    """The text behind every id the members cited (first come first), so the lead can check each claim."""
    ids = []
    for t in texts:
        for m in ID_RE.finditer(t or ""):
            ids += [i for i in (x.split()[0] for x in re.split(r"\s*,\s*", m.group(1)))
                    if i in ev and i not in skip and i not in ids]
    out, used = [], 0
    for i in ids:
        it = ev[i]
        block = f"[{i}] {it['source']}\n{it['text'][:each]}"
        if used + len(block) > total:
            out.append(f"(more cited ids not shown: {', '.join(x for x in ids[ids.index(i):])}; use read_evidence)")
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)


def check_ids(text, ev):
    """[(cited id, problem)] for every cited id this run never gave out."""
    bad = []
    for m in ID_RE.finditer(text):
        for i in (x.split()[0] for x in re.split(r"\s*,\s*", m.group(1))):
            if i not in ev:
                bad.append((i, "not an evidence id from this run"))
    return bad


def source_check(answer, case_summary, pack, ev):
    """faithfulness.check against what the team was given: the case, the manual pack and every looked-up item."""
    import faithfulness
    pages = [pack] + [f"[{it['id']}] {it['source']}\n{it['text']}" if not it["text"].startswith("[")
                      else f"[{it['id']}] {it['text']}" for it in ev.values()]
    prompt = f"CASE:\n{case_summary}\n\n" + "\n\n".join(p for p in pages if p)
    probs = faithfulness.check(answer, prompt)[0]
    return probs, faithfulness.report(answer, prompt)


# ------------------------------------------------------------------ stages
def final(run, case, a):
    v = case["vehicle_info"]
    import vehicle_id
    kb_path, ok = vehicle_id.kb_path_for(v["make"], os.path.join(ROOT, "kb"))
    if not ok:
        raise SystemExit(f"no knowledge DB for {v['make']}")
    deadline = run.t0 + LIMITS["final"]
    run.progress("building the case summary and the manual pack")
    summary = case_text(case, kb_path)
    pack = evidence_pack(summary, v, kb_path)
    if case.get("board"):       # the brainstorm so far is part of the case (handoff step 5); after the pack lookup
        summary += ("\nTEAM BRAINSTORM SO FAR (hypothesis board from earlier team rounds; ideas weighed, not "
                    "findings):\n" + case["board"][:6000])
    codes = re.findall(r"\b[PBCU]\d{4}\b", case.get("dtcs", ""))
    srv, port = start_kb(run, v, codes, a.cloud_manual, a.web)
    import kb_mcp
    store = kb_mcp.Store(run.out)
    pack_id = store.add("P", "[MANUAL PACK: the same manual text Home mode gets for this case]", pack, "runner")
    run.trace("case", summary=summary, pack_id=pack_id, pack_chars=len(pack), web=a.web, cloud_manual=a.cloud_manual)
    try:
        home_prompt = prompts.home_verdict(summary, pack)
        dx_prompt = prompts.diagnose(summary, pack_id if a.cloud_manual else None, a.web)

        def local_side():   # one GPU: Home's baseline, then the local member with tools
            b = baseline(run, home_prompt, a.local_model) if a.baseline else ("", "turned off for this run")
            loc = (call_local(run, prompts.diagnose(summary, pack_id if a.cloud_manual else None, False, local=True),
                              port, a.local_model) if a.local_member and a.cloud_manual else "")
            return b, loc

        with cf.ThreadPoolExecutor(3) as ex:
            fl = ex.submit(local_side)
            run.progress("team: Gemini and GPT-OSS diagnosing in parallel; local baseline, then local member")
            fd = {w: ex.submit(call_agy, run, w, dx_prompt, port, a.web) for w in MEMBERS}
            dx = {w: f.result() for w, f in fd.items()}
            (base, base_why), loc = fl.result()
        if a.local_member and a.cloud_manual:
            dx["local"] = loc
        run.progress("diagnoses in: " + ", ".join(f"{w} {'ok' if t else 'FAILED'}" for w, t in dx.items())
                     + f"; baseline {'ok' if base else 'skipped: ' + base_why}")
        if base:
            store.add("B", "[BASELINE: Home mode's own verdict, local model]", base, "runner")
        cross = {}
        if time.time() < deadline - 6 * 60 and sum(bool(t) for t in dx.values()) >= 1:
            run.progress("cross-check round")
            with cf.ThreadPoolExecutor(3) as ex:
                room = min(LIMITS["crosscheck"], int(deadline - time.time() - 4 * 60))
                fc = {w: ex.submit(call_agy, run, w, prompts.crosscheck(summary, w, dx, base), port, a.web, room)
                      for w in MEMBERS if dx.get(w)}
                if dx.get("local"):
                    fc["local"] = ex.submit(call_local, run, prompts.crosscheck(summary, "local", dx, base), port,
                                            a.local_model, room, 5)
                cross = {w: f.result() for w, f in fc.items()}
        else:
            run.progress("cross-check skipped (time or no diagnoses)")
        run.progress("Claude weighing it all and writing the verdict")
        verdict = call_claude(run, prompts.verdict(summary, pack_id, pack, dx, cross, base,
                                                   cited_text(evidence(run), list(dx.values()) + list(cross.values()),
                                                              skip={pack_id})), port, a.web,
                              a.claude_model, timeout=max(120, min(LIMITS["claude_call"], int(deadline - time.time()))))
    finally:
        os.killpg(srv.pid, signal.SIGTERM)
    ev = evidence(run)
    if verdict:
        verdict = mark_bad_ids(verdict, ev) + "\n\n" + lookup_log(run)
    out = {"case": a.case, "stage": "final", "web": a.web, "cloud_manual": a.cloud_manual,
           "seconds": round(time.time() - run.t0, 1), "usage": run.usage, "baseline_skipped": base_why or None,
           "members_failed": [w for w, t in dx.items() if not t] + ([] if verdict else ["claude"]),
           "lookups": sum(1 for _ in open(os.path.join(run.out, "lookups.jsonl"))) if os.path.isfile(
               os.path.join(run.out, "lookups.jsonl")) else 0}
    for name, text in (("verdict", verdict), ("baseline", base)):
        with open(os.path.join(run.out, f"{name}.txt"), "w") as f:
            f.write(text)
        if text:
            # Home is checked against what Home was given (case + pack), as the app does; Team against everything
            probs, rep = source_check(text, summary, pack, ev if name == "verdict" else {})
            out[name] = {"source_check": len(probs), "bad_ids": check_ids(text, ev) if name == "verdict" else [],
                         "issues": len(re.findall(r"(?m)^[\s#*_]*=+\s*\**\s*ISSUE\s*\d+", text)), "report": rep}
    with open(os.path.join(run.out, "report.json"), "w") as f:
        json.dump(out, f, indent=1)
    run.progress(f"done in {out['seconds']:.0f} s: verdict "
                 f"{'ok' if verdict else 'FAILED'}, source-check flags team "
                 f"{out.get('verdict', {}).get('source_check', '-')} / home {out.get('baseline', {}).get('source_check', '-')}")
    return out


def baseline_only(run, case, a):
    """Fill in Home's verdict for a finished run whose baseline was skipped; report.json gains its numbers."""
    v = case["vehicle_info"]
    import vehicle_id
    kb_path, _ = vehicle_id.kb_path_for(v["make"], os.path.join(ROOT, "kb"))
    summary = case_text(case, kb_path)
    pack = evidence_pack(summary, v, kb_path)
    if case.get("board"):       # the brainstorm so far is part of the case (handoff step 5); after the pack lookup
        summary += ("\nTEAM BRAINSTORM SO FAR (hypothesis board from earlier team rounds; ideas weighed, not "
                    "findings):\n" + case["board"][:6000])
    base, why = baseline(run, prompts.home_verdict(summary, pack), a.local_model)
    rp = os.path.join(run.out, "report.json")
    out = json.load(open(rp)) if os.path.isfile(rp) else {}
    if base:
        with open(os.path.join(run.out, "baseline.txt"), "w") as f:
            f.write(base)
        probs, rep = source_check(base, summary, pack, {})
        out["baseline"] = {"source_check": len(probs), "bad_ids": [],
                           "issues": len(re.findall(r"(?m)^[\s#*_]*=+\s*\**\s*ISSUE\s*\d+", base)), "report": rep}
        out["baseline_skipped"] = None
        out.setdefault("usage", {})["local"] = run.usage.get("local", {})
    with open(rp, "w") as f:
        json.dump(out, f, indent=1)
    run.progress(f"baseline {'ok' if base else 'skipped: ' + why}")


def brainstorm(run, case, a):
    """One team round on the mechanic's question: each member answers with its own lookups (in parallel; the local
    member when the GPU is free), Claude checks the cited ids and merges the hypothesis board."""
    v = case["vehicle_info"]
    import vehicle_id
    kb_path, _ = vehicle_id.kb_path_for(v["make"], os.path.join(ROOT, "kb"))
    run.progress("building the case summary")
    summary = case_text(case, kb_path)
    codes = re.findall(r"\b[PBCU]\d{4}\b", case.get("dtcs", ""))
    srv, port = start_kb(run, v, codes, a.cloud_manual, a.web)
    try:
        run.progress("round: Gemini and GPT-OSS answering" + ("; local member too" if a.local_member else ""))
        q = prompts.round_question(summary, a.question, case.get("board", ""), a.web, db=a.cloud_manual)
        with cf.ThreadPoolExecutor(3) as ex:
            fa = {w: ex.submit(call_agy, run, w, q, port, a.web, LIMITS["brainstorm"]) for w in MEMBERS}
            if a.local_member and a.cloud_manual:
                fa["local"] = ex.submit(call_local, run, prompts.round_question(
                    summary, a.question, case.get("board", ""), False, db=True, local=True), port, a.local_model,
                    LIMITS["brainstorm"], 6)
            ans = {w: f.result() for w, f in fa.items()}
        run.progress("answers in: " + ", ".join(f"{w} {'ok' if t else 'FAILED'}" for w, t in ans.items()))
        run.progress("Claude checking the ids and merging the board")
        board = call_claude(run, prompts.board(summary, a.question, case.get("board", ""), ans,
                                               cited_text(evidence(run), list(ans.values()))), port, a.web,
                            a.claude_model, timeout=LIMITS["claude_call"])
    finally:
        os.killpg(srv.pid, signal.SIGTERM)
    ev = evidence(run)
    if board:
        board = mark_bad_ids(board, ev) + "\n\n" + lookup_log(run)
    with open(os.path.join(run.out, "board.txt"), "w") as f:
        f.write(board)
    out = {"stage": "brainstorm", "seconds": round(time.time() - run.t0, 1), "usage": run.usage,
           "members_failed": [w for w, t in ans.items() if not t] + ([] if board else ["claude"]),
           "bad_ids": check_ids(board, ev)}
    if board:
        probs, rep = source_check(board, summary, "", ev)
        out["board"] = {"source_check": len(probs), "report": rep}
    with open(os.path.join(run.out, "report.json"), "w") as f:
        json.dump(out, f, indent=1)
    run.progress(f"round done in {out['seconds']:.0f} s" + ("" if board else ": board FAILED"))
    if not board:
        sys.exit(1)
    return out


def ask(run, case, a):
    """Any model call of the main app, answered by the team (teamdeck): the app's own prompt goes to every member
    with the DB tools, deep tasks get a cross-check round, and Claude writes the answer in the format the prompt
    asks for. answer.txt is the answer only (the app parses it); notes.txt and the lookup list are shown under it."""
    with open(a.prompt_file) as f:
        task = f.read()
    if VIN_RE.search(task):
        raise SystemExit("a VIN-like value is in the prompt; refusing to send")
    deep = a.role in ("deep", "think")
    v = case["vehicle_info"]
    codes = re.findall(r"\b[PBCU]\d{4}\b", case.get("dtcs", "") + " " + task)
    srv, port = start_kb(run, v, list(dict.fromkeys(codes))[:8], a.cloud_manual, a.web)
    budget = LIMITS["agy_call"] if deep else LIMITS["ask_fast"]
    try:
        run.progress(f"team ({'deep' if deep else 'quick'}): Gemini, GPT-OSS" + (", local" if a.local_member else "")
                     + " working on it")
        mp = prompts.ask_member(task, a.web, deep, db=a.cloud_manual)
        with cf.ThreadPoolExecutor(3) as ex:
            fa = {w: ex.submit(call_agy, run, w, mp, port, a.web, budget) for w in MEMBERS}
            if a.local_member and a.cloud_manual:
                fa["local"] = ex.submit(call_local, run, prompts.ask_member(task, False, deep, db=True, local=True),
                                        port, a.local_model, budget, 8 if deep else 4)
            ans = {w: f.result() for w, f in fa.items()}
        run.progress("answers in: " + ", ".join(f"{w} {'ok' if t else 'FAILED'}" for w, t in ans.items()))
        cross = {}
        if deep and any(ans.values()):
            run.progress("cross-check round")
            with cf.ThreadPoolExecutor(3) as ex:
                fc = {w: ex.submit(call_agy, run, w, prompts.crosscheck(task[:12000], w, ans, ""), port, a.web,
                                   LIMITS["crosscheck"]) for w in MEMBERS if ans.get(w)}
                if ans.get("local"):
                    fc["local"] = ex.submit(call_local, run, prompts.crosscheck(task[:12000], "local", ans, ""), port,
                                            a.local_model, LIMITS["crosscheck"], 4)
                cross = {w: f.result() for w, f in fc.items()}
        run.progress("Claude writing the answer")
        out = call_claude(run, prompts.ask_lead(task, ans, cross, cited_text(evidence(run), list(ans.values())
                                                                             + list(cross.values())), deep),
                          port, a.web, a.claude_model, timeout=LIMITS["claude_call"])
    finally:
        os.killpg(srv.pid, signal.SIGTERM)
    ev = evidence(run)
    answer, _, notes = out.partition("=== TEAM NOTES ===")
    answer = mark_bad_ids(answer.strip(), ev)
    for name, text in (("answer", answer), ("notes", notes.strip()), ("lookups", lookup_log(run)),
                       ("evidence", cited_text(ev, [answer], each=3000, total=60000))):
        with open(os.path.join(run.out, f"{name}.txt"), "w") as f:
            f.write(text)
    rep = {"stage": "ask", "role": a.role, "seconds": round(time.time() - run.t0, 1), "usage": run.usage,
           "members_failed": [w for w, t in ans.items() if not t] + ([] if answer else ["claude"]),
           "bad_ids": check_ids(answer, ev)}
    with open(os.path.join(run.out, "report.json"), "w") as f:
        json.dump(rep, f, indent=1)
    run.progress(f"done in {rep['seconds']:.0f} s" + ("" if answer else ": no answer"))
    if not answer:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--stage", default="final", choices=("final", "brainstorm", "ask"))
    ap.add_argument("--prompt-file", help="ask: the app's prompt")
    ap.add_argument("--role", default="fast", help="ask: the app's role (fast, deep, think)")
    ap.add_argument("--question", default="")
    ap.add_argument("--web", default="on", choices=("on", "off"))
    ap.add_argument("--no-cloud-manual", dest="cloud_manual", action="store_false",
                    help="team members get no DB text (default: on, capped per member, Charlie 2026-11-11)")
    ap.add_argument("--no-local-member", dest="local_member", action="store_false")
    ap.add_argument("--no-baseline", dest="baseline", action="store_false")
    ap.add_argument("--baseline-only", action="store_true", help="only the local Home verdict (fills a skipped one)")
    ap.add_argument("--local-model", default="qwen3-next:80b-a3b-instruct-q4_K_M")
    ap.add_argument("--claude-model", default="sonnet")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    a.web = a.web == "on"
    lock = open(os.path.join(ROOT, "logs", "team", ".run.lock"), "w")
    import fcntl
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)        # one Team run at a time
    except BlockingIOError:
        raise SystemExit("another Team run is going; one at a time")
    with open(a.case) as f:
        case = scrub(json.load(f))
    if not case["vehicle_info"].get("make"):
        raise SystemExit("case has no make")
    out = a.out or os.path.join(ROOT, "logs", "team", time.strftime("%Y%m%d_%H%M%S") + "_" + a.stage)
    run = Run(out)
    run.progress(f"Team {a.stage} run -> {out}")
    if a.baseline_only:
        baseline_only(run, case, a)
        return
    {"final": final, "brainstorm": brainstorm, "ask": ask}[a.stage](run, case, a)


if __name__ == "__main__":
    main()
