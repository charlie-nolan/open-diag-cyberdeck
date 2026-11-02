"""The user's side of the loop: the terminal UI (works over ssh) and the scripted fake the user for tests.
Both count the time spent waiting on them (wait_s), which doesn't count toward the agent's time cap."""
import re
import shutil
import textwrap
import time

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"


class TerminalHuman:
    def __init__(self, color=True):
        self.wait_s = 0.0
        self.c = color

    def _w(self):
        return max(40, min(100, shutil.get_terminal_size((90, 20)).columns - 2))

    def _input(self, prompt):
        t0 = time.time()
        try:
            return input(prompt)
        except EOFError:
            return "stop"
        finally:
            self.wait_s += time.time() - t0

    def ask(self, header, instruction, check, expect, unit, choices, notes):
        w = self._w()
        b, o = (BOLD, OFF) if self.c else ("", "")
        print("\n" + "=" * w)
        print(f"{b}{header}{o}")
        for line in textwrap.wrap(instruction, w):
            print(f"{b}{line}{o}")
        if check:
            print("\n" + "\n".join(f"{b}{x}{o}" for x in textwrap.wrap(f"QUESTION: {check}", w)))
        for n in notes:
            print("\n".join(textwrap.wrap(f"! {n}", w)))
        want = {"number": f"a reading in {unit}" if unit else "a reading with its unit", "yesno": "yes or no",
                "choice": " / ".join(choices or []), "text": "what you see", "done": "done"}.get(expect, expect)
        print(f"{DIM if self.c else ''}Reply with {want}, or: done / can't / skip / back / stop{o}")
        return self._input("> ").strip()

    def ask_text(self, prompt):
        return self._input(f"\n{prompt}\n> ").strip()

    def strip(self, t, judged, trigger, met):
        """The live strip: one line per sample, Python's verdict per value."""
        vals = " | ".join(f"{j['key']} {j['value']:g} {j['unit']} ({j['status']})" for j in judged)
        hold = ""
        if trigger:
            hold = "  got it" if met else f"  hold it... ({trigger['key']} {trigger['op']} {trigger['value']:g})"
        print(f"\r{t:6.1f}s  {vals}{hold}", end="\n" if met else "", flush=True)

    def act(self, header, title, action, n, total):
        """One action of a step, in the manual's words. Enter = done; or can't / skip / back / stop."""
        b, o = (BOLD, OFF) if self.c else ("", "")
        if n == 1:
            print("\n" + "=" * self._w() + f"\n{b}{header}{'  ' + title if title else ''}{o}")
        for line in textwrap.wrap(f"{n}/{total}  {action}", self._w()):
            print(f"{b}{line}{o}")
        return self._input("[Enter = done] > ").strip()

    def confirm(self, text):
        print("\n" + "\n".join(textwrap.wrap(f"SAFETY: {text}", self._w())))
        return self._input("[y/n] > ").strip().lower().startswith("y")

    def choose(self, question, options):
        print("\n" + question)
        for i, o in enumerate(options, 1):
            print(f"  {i}. {o}")
        while True:
            a = self._input("number > ").strip()
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a) - 1

    def say(self, text):
        print("\n" + "\n".join(textwrap.wrap(f"Agent: {text}", self._w())))

    def show_figures(self, figs, src):
        print(f"Figures [{src}]: " + ", ".join(p or n for n, p in figs[:8]))


class ScriptedHuman:
    """Answers from a scenario file:
        "replies": {"step:3": "4.8 V", "step:5": ["0.2 V", "0.3 V"], "page": "...", "default": "can't"},
        "variant": "regex on the variant label", "deny_state": ["running"]
    A list reply is used in order (for steps asked again after 'back')."""

    def __init__(self, scenario):
        self.s = scenario
        self.wait_s = 0.0
        self.used = {}
        self.transcript = []

    def _reply(self, key):
        r = self.s.get("replies", {}).get(key)
        if isinstance(r, list):
            i = self.used.get(key, 0)
            self.used[key] = i + 1
            return r[i] if i < len(r) else r[-1]
        return r

    def ask(self, header, instruction, check, expect, unit, choices, notes):
        m = re.match(r"Step (\d+)", header)
        key = f"step:{m.group(1)}" if m else "page"
        reply = self._reply(key)
        if reply is None:
            reply = self.s.get("replies", {}).get("default", "can't")
        self.transcript.append({"ask": header, "instruction": instruction, "question": check, "expect": expect,
                                "unit": unit, "notes": notes, "reply": reply})
        return reply

    def strip(self, t, judged, trigger, met):
        self.transcript.append({"strip": t, "values": {j["key"]: j["value"] for j in judged}, "met": met})

    def act(self, header, title, action, n, total):
        """"action_replies": {"step:3": "can't"} answers that step's actions; default done."""
        m = re.match(r"Step (\d+)", header)
        r = self.s.get("action_replies", {}).get(f"step:{m.group(1)}" if m else "page", "")
        self.transcript.append({"action": action, "header": header, "n": n, "reply": r or "done"})
        return r

    def ask_text(self, prompt):
        """Intake questions: "ident_answers": {"VIN": "...", "Make": "...", "Model year": "..."} (prefix match)."""
        ans = next((v for k, v in self.s.get("ident_answers", {}).items() if prompt.startswith(k)), "")
        self.transcript.append({"intake": prompt, "reply": ans})
        return ans

    def confirm(self, text):
        if text.startswith("Detour?"):
            ok = self.s.get("detour", "no") == "yes"
            self.transcript.append({"confirm": text, "reply": ok})
            return ok
        deny = any(re.search(d, text, re.I) for d in self.s.get("deny_state", []))
        self.transcript.append({"confirm": text, "reply": not deny})
        return not deny

    def choose(self, question, options):
        rx = self.s.get("variant")
        for k, v in self.s.get("choose", {}).items():     # intake picks: {"Which engine": "regex"}
            if question.startswith(k):
                rx = v
        i = next((i for i, o in enumerate(options) if rx and re.search(rx, o, re.I)), None)
        if i is None:     # the scenario's pick isn't offered: take "none of these" so the test shows it
            i = len(options) - 1
        self.transcript.append({"choose": question, "options": options, "picked": i})
        return i

    def say(self, text):
        self.transcript.append({"say": text})

    def show_figures(self, figs, src):
        self.transcript.append({"figures": [n for n, _ in figs], "src": src})
