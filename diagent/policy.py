"""A deterministic stand-in for the model: walks the manual exactly as the harness expects, through the same
tool-call path as Ollama. Used (1) as the plain procedure walk when the PC can't be reached ("agent off") and
(2) in the scenario tests, with optional injected bad calls to prove the harness rejects them.
It never words anything itself beyond the manual's own step text."""
import json
import re

from . import live, units, watch


class ScriptedModel:
    name = "scripted"

    def __init__(self, case, tools, inject=None, use_field=False):
        self.case, self.tools, self.use_field = case, tools, use_field
        self.inject = list(inject or [])   # [{"after": n calls, "call": {...}} | {"after": n, "text": "..."}]
        self.n = 0
        self.did = set()

    def _call(self, name, **args):
        return {"content": "", "tool_calls": [{"name": name, "arguments": args}],
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"function": {"name": name, "arguments": args}}]}}

    def chat(self, messages, tools):
        self.n += 1
        for inj in self.inject:
            if inj.get("after") == self.n - 1 and not inj.get("used"):
                inj["used"] = True
                if "text" in inj:
                    return {"content": inj["text"], "tool_calls": [],
                            "message": {"role": "assistant", "content": inj["text"]}}
                return self._call(inj["call"]["name"], **inj["call"].get("arguments", {}))
        allowed = {t["function"]["name"] for t in tools}
        return self._next(allowed)

    def _once(self, key):
        if key in self.did:
            return False
        self.did.add(key)
        return True

    def _next(self, allowed):
        c, t = self.case, self.tools
        p = c.proc or {}
        if not c.scan_ts and self._once("read_car"):
            return self._call("read_car")                 # codes come from a scan of this car when one is possible
        if p.get("mode") == "symptom":
            return self._suspects(p)
        if not p.get("code"):
            if c.codes:
                return self._call("get_procedure", code=c.codes[0])
            if c.symptoms and self._once("chart"):
                return self._call("symptom_chart", query=c.symptoms)
            if c.symptoms and self._once("search"):
                return self._call("search_manual", query=c.symptoms, scope="symptom")
            return self._pages()
        if p.get("detour"):
            return self._detour(p["detour"], allowed)
        if p.get("mode") == "steps" and self.use_field and t.current() is not None and not p.get("detours"):
            got = self._field(p)
            if got:
                return got
        if p.get("mode") == "steps":
            if p.get("ended"):
                ev = [p["end_src"]] + [r for e in p["path"] for r in e["readings"]]
                if not any(h["cause"] == p["end_text"] for h in c.hypotheses) and "update_hypothesis" in allowed:
                    reading = [r for r in ev if r.startswith(("R", "LV"))] or [x for x in c.sources if x.startswith("SC")]
                    if reading:
                        return self._call("update_hypothesis", cause=p["end_text"], status="supported",
                                          evidence_ids=[p["end_src"]] + reading[-1:])
                cites = list(dict.fromkeys(ev))
                if p.get("unconfirmed"):
                    return self._after_cant(cites)
                path = " -> ".join(f"{e['step']} {e['answer']}" for e in p["path"])
                return self._call("finish", result="diagnosis",
                                  summary=f"Manual path {path} ended at [{p['end_src']}]: {p['end_text']}",
                                  citations=cites)
            if p.get("broken"):
                src = p.get("broken_src")
                if src and self._once(f"read:{src}"):
                    return self._call("read_section", section_id=src)
                return self._finish_unconfirmed([src] if src else list(c.sources)[:1],
                                                f"The manual table can't be walked here ({p['broken']}): read the "
                                                f"step from the page [{src}]")
            s = t.current()
            if s is None:
                return self._finish_unconfirmed(list(c.sources)[:1], "no current step")
            src = t.step_src(s)
            if any(u["step"] == s["n"] for u in p.get("unconfirmed", [])):
                return self._after_cant([src], s)
            trig = watch.until_trigger(s["step"], c.car.get("telemetry_db"))
            cable = getattr(t.car, "cable", "") or c.car.get("cable") or ""
            if trig and hasattr(t.car, "sample") and live.readable(c.car, cable, trig["key"]) \
                    and self._once(f"watch:{s['n']}"):
                return self._call("watch_live", params=[trig["key"]], seconds=900, source_id=src,
                                  instruction=trig["quote"].capitalize() + ".",
                                  trigger={"param": trig["key"], "op": trig["op"], "value": trig["value"]})
            dim = units.expected_unit(s["check"])
            title = s["step"].split(";")[0].strip()
            return self._call("ask_human", instruction=title, expect="number" if dim else "yesno",
                              unit=dim or "", source_id=src)
        return self._pages()

    def _field(self, p):
        """Ask the field once; offer one detour when a forum fix names a component the manual has an inspection for."""
        c = self.case
        if self._once("field"):
            return self._call("field_reports", query=p["code"])
        frs = [k for k, v in c.sources.items() if v["kind"] == "field"
               and v["label"].startswith(("forum fix", "baked card")) and "doesn't match" not in v["label"]]
        if not frs:
            return None
        lab = c.sources[frs[0]]["label"].split(": ", 1)[1]
        comp = re.sub(r"\s*\(\d+ reports?.*$", "", lab.split(" card: ")[-1]).strip()
        if self._once("field_offer"):          # Python finds the manual inspection, or offers nothing
            return self._call("offer_detour", component=comp, field_ids=frs[:1])
        return None

    def _suspects(self, p):
        """Symptom chart: each suspect in the manual's order, its inspection's instructions one at a time."""
        c = self.case
        for i, sp in enumerate(p.get("suspects", [])[:3]):
            if not sp["src"]:
                continue
            if self._once(f"sus:{i}"):
                return self._call("page_steps", section_id=sp["src"])
            sid = c.sources[sp["src"]]["sid"]
            units_ = [k for k, v in c.sources.items() if v.get("sid") == sid and ", instruction " in v["label"]]
            if units_ and self._once(f"susask:{i}"):
                u = c.sources[units_[0]]["text"]
                q = re.search(r"\b(If|Does|Is|Are|Do|Did|Was|Were|Has|Have)\b.*", u)
                return self._call("ask_human", instruction=u[:q.start()].strip() if q and q.start() else u,
                                  expect="text" if q else "done", question=q.group(0) if q else "", source_id=units_[0])
        if any(not u.get("alt") for u in p.get("unconfirmed", [])) and self._once(f"alt:sym{len(c.sources)}"):
            return self._call("search_manual", query=p["symptom"], scope="symptom")
        walked = [s_["src"] for s_ in p.get("suspects", [])[:3] if s_["src"]]
        rest = [s_["text"] for s_ in p.get("suspects", [])[3:6]]
        return self._finish_unconfirmed([p["chart_src"]] + walked,
                                        f"symptom chart [{p['chart_src']}]: next suspects in the manual's order: "
                                        + ("; ".join(rest) or "none left"))

    def _detour(self, d, allowed):
        c = self.case
        if "page_steps" not in allowed:
            return self._call("end_detour", found="Detour not done in this turn.")
        if self._once(f"dsteps:{d['src']}"):
            return self._call("page_steps", section_id=d["src"])
        units_ = [k for k, v in c.sources.items() if v.get("sid") == d["sid"] and ", instruction " in v["label"]]
        if units_ and self._once(f"dask:{units_[0]}"):
            return self._call("ask_human", instruction=c.sources[units_[0]]["text"], expect="done", source_id=units_[0])
        got = [r["id"] for r in c.readings[-1:]]
        return self._call("end_detour", found="Detour instruction given" + (f" [{got[0]}]" if got else "") + ".")

    def _after_cant(self, cites, s=None):
        """A step couldn't be done: look for another manual path first, then finish unconfirmed."""
        c = self.case
        if s is not None and self._once(f"alt:{s['n']}"):
            return self._call("search_manual", query=s["step"].split(";")[0].strip().rstrip("."))
        pend = [u for u in (c.proc or {}).get("unconfirmed", []) if not u.get("alt")]
        if pend and self._once(f"alt:pending{len(c.sources)}"):
            return self._call("search_manual", query=(c.proc or {}).get("code") or c.symptoms or "diagnosis")
        u = (c.proc or {}).get("unconfirmed", [{}])[-1]
        alts = [k for k, v in c.sources.items() if v["kind"] == "section"][-2:]
        return self._finish_unconfirmed(list(dict.fromkeys(cites + alts)),
                                        f"step {u.get('step')} [{u.get('src')}]" + (f"; other manual pages looked at: "
                                        + ", ".join(f"[{a}]" for a in alts) if alts else ""))

    def _pages(self):
        """No step table: split the manual page into its instructions and give them one at a time."""
        c, p = self.case, self.case.proc or {}
        pages = [k for k, v in c.sources.items() if v["kind"] == "section" and ", instruction " not in v["label"]]
        if not pages and p.get("code") and self._once("search_code"):
            return self._call("search_manual", query=p["code"])
        units_ = [k for k, v in c.sources.items() if ", instruction " in v["label"]]
        for pg in pages[:3]:
            if not units_ and self._once(f"units:{pg}"):
                return self._call("page_steps", section_id=pg)
        done = self.did
        for u in units_[:6]:
            if f"ask:{u}" not in done:
                done.add(f"ask:{u}")
                text = c.sources[u]["text"]
                q = re.search(r"\b(If|Does|Is|Are|Do|Did|Was|Were|Has|Have)\b.*", text)
                if q:
                    return self._call("ask_human", instruction=text[:q.start()].strip() or text, expect="text",
                                      question=q.group(0), source_id=u)
                return self._call("ask_human", instruction=text, expect="done", source_id=u)
        if p.get("unconfirmed") and not all(x.get("alt") for x in p["unconfirmed"]) and self._once("alt:pages"):
            return self._call("search_manual", query=(p.get("code") or c.symptoms or "diagnosis"))
        cites = units_[:6] or pages[:1] or list(c.sources)[:1]
        return self._finish_unconfirmed(cites, "No step table for this car: the manual page's instructions "
                                        + (f"[{cites[0]}]" if cites else "") + " were given in order; the manual has no "
                                        "Yes/No branches here for Python to walk")

    def _finish_unconfirmed(self, cites, settle):
        cites = [x for x in cites if x] or list(self.case.known_ids())[:1]
        return self._call("finish", result="unconfirmed",
                          summary="Not confirmed: the manual path could not be completed in this run.",
                          citations=cites, settle_test=settle)


def dump(x):
    return json.dumps(x, ensure_ascii=False)
