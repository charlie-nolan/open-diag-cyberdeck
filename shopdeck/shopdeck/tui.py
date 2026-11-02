"""Keyboard screens (curses). Works on the deck's own monitor in a terminal and over ssh from anywhere.

Every screen: arrows / PgUp / PgDn move, Enter picks, Esc or q goes back. Keys for each screen are on the
bottom line. Number keys 1-8 jump between screens from the menu.
Trackball/mouse (optional): wheel scrolls, left click picks a line or presses a key named on the bottom line,
right click goes back.
"""
import curses
import os
import re
import textwrap
import time

from . import chart as C
from . import link as L
from . import repair as R
from .case import Case, INTERVIEW

KIND_LABEL = {"history": "Recent work", "reused": "Reused part", "hypothesis": "Your theory", "note": "Note"}


def render(body, cells=" | "):
    """Manual section text for the screen: no html, figures marked, table rows as plain lines."""
    out = []
    for line in body.splitlines():
        s = line.strip()
        if not s or C.SEP_RE.match(s) or s.startswith("(continued"):
            continue
        if s == "[fig]":
            out.append("   [figure in the manual]")
            continue
        if s.startswith("|"):
            parts = [C.clean(x) for x in s.strip("|").split("|")]
            s = cells.join(p for p in parts if p)
        else:
            s = C.clean(s.lstrip("#").strip())
        if s:
            out.append(s)
    return out


def short(sec):
    """One list line for a manual section: where it is, and which book."""
    if sec.kind == "charm":
        return " > ".join(x.strip() for x in sec.path.split(">")[-3:]) + "   (CHARM)"
    return f"{sec.path}   (factory manual p.{sec.page})" if sec.page else f"{sec.path}   ({sec.source})"


class UI:
    def __init__(self, scr, desk):
        self.scr = scr
        self.desk = desk
        self.flash = ""
        curses.curs_set(0)
        self.scr.keypad(True)
        self.c = {}
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            pairs = {"head": (curses.COLOR_BLACK, curses.COLOR_CYAN), "good": (curses.COLOR_GREEN, bg),
                     "warn": (curses.COLOR_YELLOW, bg), "bad": (curses.COLOR_RED, bg),
                     "acc": (curses.COLOR_CYAN, bg), "dim": (curses.COLOR_BLUE, bg),
                     "sel": (curses.COLOR_BLACK, curses.COLOR_WHITE)}
            for i, (k, (fg, b)) in enumerate(pairs.items(), 1):
                curses.init_pair(i, fg, b)
                self.c[k] = curses.color_pair(i)
        for k, alt in (("head", curses.A_REVERSE), ("sel", curses.A_REVERSE), ("good", 0), ("warn", curses.A_BOLD),
                       ("bad", curses.A_BOLD), ("acc", 0), ("dim", curses.A_DIM)):
            self.c.setdefault(k, alt)
        self.B = curses.A_BOLD
        self.hint_spans = []
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass

    # ------------------------------------------------------------------ mouse
    WHEEL_UP = getattr(curses, "BUTTON4_PRESSED", 0)
    WHEEL_DOWN = getattr(curses, "BUTTON5_PRESSED", 0x200000)
    CLICK = curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_RELEASED
    RIGHT = curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED

    def mouse(self):
        """('up'|'down'|'back'|'click'|None, y, x) for the mouse event just read."""
        try:
            _, x, y, _, b = curses.getmouse()
        except curses.error:
            return None, 0, 0
        if b & self.WHEEL_UP:
            return "up", y, x
        if b & self.WHEEL_DOWN:
            return "down", y, x
        if b & self.RIGHT:
            return "back", y, x
        if b & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            h, _ = self.size()
            if y == h - 1:
                k = self.hint_key(x)
                return ("key", k, x) if k is not None else (None, y, x)
            return "click", y, x
        return None, y, x

    def hint_key(self, x):
        """The key named by the bottom-line word under column x ('y yes', 'Enter open', 'Esc back')."""
        for a, b, word in self.hint_spans:
            if a <= x < b:
                first = word.split()[0]
                if first == "Enter":
                    return 10
                if first == "Esc":
                    return 27
                if len(first) == 1:
                    return ord(first)
                if re.fullmatch(r"\d-\d", first):
                    return None
        return None

    # ------------------------------------------------------------------ drawing basics
    def size(self):
        h, w = self.scr.getmaxyx()
        return max(h, 8), max(w, 30)

    def put(self, y, x, text, attr=0):
        h, w = self.size()
        if 0 <= y < h and x < w:
            try:
                self.scr.addnstr(y, x, text, max(0, w - x - (1 if y == h - 1 else 0)), attr)
            except curses.error:
                pass

    def frame(self, title, hint):
        self.scr.erase()
        h, w = self.size()
        case = self.desk.case
        top = f" SHOPDECK  {title}"
        right = f"{case.car}  {' '.join(case.codes[:4])}{'…' if len(case.codes) > 4 else ''} "
        self.put(0, 0, top.ljust(w), self.c["head"] | self.B)
        if len(top) + len(right) < w:
            self.put(0, w - len(right) - 1, right, self.c["head"])
        foot = self.flash or hint
        self.put(h - 1, 0, foot[:w - 1].ljust(w - 1), self.c["warn"] if self.flash else self.c["dim"])
        self.hint_spans = [] if self.flash else [(m.start(), m.end(), m.group(0))
                                                 for m in re.finditer(r"\S+(?: \S+)*", foot)]
        self.flash = ""

    def wrap(self, lines, width):
        """[(text, attr)] -> wrapped [(text, attr)], keeping leading indents."""
        out = []
        for item in lines:
            text, attr = item if isinstance(item, tuple) else (item, 0)
            if text == "":
                out.append(("", 0))
                continue
            ind = len(text) - len(text.lstrip(" "))
            for i, part in enumerate(textwrap.wrap(text.strip(), max(10, width - ind - 1)) or [""]):
                out.append((" " * ind + part, attr))
        return out

    # ------------------------------------------------------------------ widgets
    def view(self, title, lines, hint="↑↓ PgUp PgDn scroll   q back", keys="", top=0):
        """Scrollable text. Returns a key from `keys` when pressed (with the scroll position), else None."""
        while True:
            h, w = self.size()
            body = self.wrap(lines, w - 2)
            self.frame(title, hint)
            rows = h - 2
            top = max(0, min(top, max(0, len(body) - rows)))
            for i, (t, a) in enumerate(body[top:top + rows]):
                self.put(1 + i, 1, t, a)
            if len(body) > rows:
                self.put(h - 2, w - 12, f" {min(100, int((top + rows) * 100 / len(body)))}% ", self.c["dim"])
            k = self.scr.getch()
            if k == curses.KEY_MOUSE:
                ev, a, _ = self.mouse()
                if ev == "up":
                    top -= 3
                elif ev == "down":
                    top += 3
                elif ev == "back":
                    return None
                elif ev == "key" and a is not None:
                    k = a
                else:
                    continue
                if ev != "key":
                    continue
            if 0 < k < 256 and chr(k) in keys:      # screen keys win over the j/k/space scroll keys
                self.last_top = top
                return chr(k)
            if k in (curses.KEY_DOWN, ord("j")):
                top += 1
            elif k in (curses.KEY_UP, ord("k")):
                top -= 1
            elif k in (curses.KEY_NPAGE, ord(" ")):
                top += rows - 1
            elif k == curses.KEY_PPAGE:
                top -= rows - 1
            elif k == curses.KEY_HOME:
                top = 0
            elif k == curses.KEY_END:
                top = len(body)
            elif k in (27, ord("q")):
                return None
            elif k == curses.KEY_RESIZE:
                continue

    def choose(self, title, items, hint=None, sel=0, keys="", filtering=True):
        """Pick from a list. Returns the index (or (key, index) when one of `keys` is pressed), None on Esc.
        With filtering, typing narrows the list; without it, letters are only the screen's keys."""
        if hint is None:
            hint = "↑↓ move   Enter pick   " + ("type to filter   " if filtering else "") + "Esc/q back"
        filt = ""
        while True:
            h, w = self.size()
            shown = [(i, it) for i, it in enumerate(items)
                     if not filt or filt.lower() in (it[0] if isinstance(it, tuple) else it).lower()]
            self.frame(title, (f"filter: {filt}   " if filt else "") + hint)
            rows = h - 2
            sel = max(0, min(sel, len(shown) - 1))
            top = max(0, sel - rows + 1)
            for r, (i, it) in enumerate(shown[top:top + rows]):
                text, attr = it if isinstance(it, tuple) else (it, 0)
                if top + r == sel:
                    attr = self.c["sel"]
                self.put(1 + r, 0, (" " + text)[:w - 1].ljust(w - 1), attr)
            if not shown:
                self.put(2, 2, "(nothing matches)" if filt else "(empty)", self.c["dim"])
            k = self.scr.getch()
            if k == curses.KEY_MOUSE:
                ev, a, _ = self.mouse()
                if ev == "up":
                    sel -= 1
                elif ev == "down":
                    sel += 1
                elif ev == "back":
                    return None
                elif ev == "click" and 1 <= a <= rows and top + a - 1 < len(shown):
                    return shown[top + a - 1][0]
                elif ev == "key" and a is not None:
                    k = a
                if ev != "key":
                    continue
            if k == curses.KEY_DOWN:
                sel += 1
            elif k == curses.KEY_UP:
                sel -= 1
            elif k == curses.KEY_NPAGE:
                sel += rows - 1
            elif k == curses.KEY_PPAGE:
                sel -= rows - 1
            elif k in (10, 13, curses.KEY_ENTER):
                if shown:
                    return shown[sel][0]
            elif k == 27:
                if filt:
                    filt = ""
                else:
                    return None
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                filt = filt[:-1]
            elif 0 < k < 256 and chr(k) in keys and not filt:
                return chr(k), (shown[sel][0] if shown else None)
            elif k == ord("q") and not filt:
                return None
            elif 32 <= k < 127 and filtering:
                filt += chr(k)
                sel = 0

    def ask(self, prompt, default=""):
        """One line of text at the bottom. Enter accepts, Esc cancels (None)."""
        buf, pos = list(default), len(default)
        curses.curs_set(1)
        try:
            while True:
                h, w = self.size()
                line = f"{prompt}: " + "".join(buf)
                start = max(0, len(prompt) + 2 + pos - (w - 2))
                self.put(h - 1, 0, line[start:start + w - 1].ljust(w - 1), self.c["acc"] | self.B)
                try:
                    self.scr.move(h - 1, min(w - 2, len(prompt) + 2 + pos - start))
                except curses.error:
                    pass
                k = self.scr.get_wch()
                if k == curses.KEY_MOUSE:
                    if self.mouse()[0] == "back":
                        return None
                    continue
                if k in ("\n", "\r", curses.KEY_ENTER):
                    return "".join(buf).strip()
                if k == "\x1b":
                    return None
                if k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    if pos:
                        buf.pop(pos - 1)
                        pos -= 1
                elif k == curses.KEY_DC:
                    if pos < len(buf):
                        buf.pop(pos)
                elif k == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                elif k == curses.KEY_RIGHT:
                    pos = min(len(buf), pos + 1)
                elif k == curses.KEY_HOME:
                    pos = 0
                elif k == curses.KEY_END:
                    pos = len(buf)
                elif isinstance(k, str) and k.isprintable():
                    buf.insert(pos, k)
                    pos += 1
        finally:
            curses.curs_set(0)

    def confirm(self, prompt):
        a = self.ask(prompt + " (y/n)")
        return bool(a) and a.lower().startswith("y")

    def busy(self, text):
        h, w = self.size()
        self.put(h - 1, 0, text[:w - 1].ljust(w - 1), self.c["warn"] | self.B)
        self.scr.refresh()

    def number(self, prompt, default=""):
        while True:
            a = self.ask(prompt, str(default))
            if a is None or a == "":
                return None
            try:
                return float(a.replace("$", "").replace(",", ""))
            except ValueError:
                self.flash = "that's not a number"

    # ------------------------------------------------------------------ the menu
    MENU = [("1", "Ticket & car"), ("2", "Customer interview"), ("3", "Codes & live data"), ("4", "Diagnose"),
            ("5", "Your notes & theories"), ("6", "Jobs, parts & quote"), ("7", "Search the manual"),
            ("8", "Work order (print)")]

    def run(self):
        sel = 0
        while True:
            items = [f"{k}  {name}" + self.status(k) for k, name in self.MENU]
            r = self.choose("menu", items, "1-8 or Enter open   n new ticket   o open ticket   s settings   Q quit",
                            sel=sel, keys="12345678noQs", filtering=False)
            if r is None:
                continue
            key = r[0] if isinstance(r, tuple) else self.MENU[r][0]
            if isinstance(r, int):
                sel = r
            if self.open(key):
                return

    def open(self, key):
        """Open a menu entry by its key. True means quit."""
        if key == "Q":
            self.desk.save()
            return True
        if key == "n":
            if self.confirm("Start a new ticket? The current one is saved"):
                self.desk.save()
                self.desk.new_case()
                self.screen_car()
        elif key == "o":
            self.screen_tickets()
        elif key == "s":
            self.screen_settings()
        else:
            getattr(self, "screen_" + {"1": "car", "2": "interview", "3": "codes", "4": "diagnose", "5": "notes",
                                       "6": "jobs", "7": "search", "8": "print"}[key])()
        return False

    def status(self, key):
        d, c = self.desk, self.desk.case
        if key == "1":
            return f"   [{c.car}]" if c.model else "   [pick the car first]"
        if key == "2":
            n = sum(1 for k, _ in INTERVIEW if c.interview.get(k))
            return f"   [{n} answered]" if n else ""
        if key == "3":
            return f"   [{', '.join(c.codes) or 'no codes'}]"
        if key == "4":
            done = sum(1 for w in c.walks.values() if w.get("result"))
            return f"   [{done} chart(s) finished]" if c.walks else ""
        if key == "5":
            return f"   [{len(c.notes)}]" if c.notes else ""
        if key == "6":
            if not c.jobs:
                return ""
            q = d.quote()
            return f"   [{len(c.jobs)} job(s), ${q.total:,.2f}" + (", prices missing]" if q.missing else "]")
        return ""

    # ------------------------------------------------------------------ 1. ticket and car
    def screen_car(self):
        d, c = self.desk, self.desk.case
        while True:
            items = [f"Car:       {c.car}", f"Customer:  {c.customer or '-'}", f"VIN:       {c.vin or '-'}",
                     f"Mileage:   {c.mileage or '-'}", "What the database has for this car"]
            r = self.choose(f"ticket {c.id}", items, "Enter change   Esc back", filtering=False)
            if r is None:
                return
            if r == 0:
                self.pick_car()
            elif r == 1:
                v = self.ask("Customer name / phone", c.customer)
                if v is not None:
                    c.customer = v
            elif r == 2:
                v = self.ask("VIN (17 characters)", c.vin)
                if v is not None:
                    v = v.upper().replace(" ", "")
                    if v and not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", v):
                        self.flash = "a VIN is 17 letters/numbers without I, O or Q: kept anyway"
                    c.vin = v
            elif r == 3:
                v = self.ask("Mileage", c.mileage)
                if v is not None:
                    c.mileage = v
            elif r == 4:
                self.show_coverage()
            d.save()

    def pick_car(self):
        d = self.desk
        models = d.kb.models()
        m = self.choose("model", models)
        if m is None:
            return
        years = d.kb.years(models[m])
        y = self.choose(f"{models[m]}: year", [str(x) for x in years], sel=len(years) - 1)
        if y is None:
            return
        vehs = d.kb.vehicles(models[m], years[y])
        e = 0 if len(vehs) == 1 else self.choose(f"{years[y]} {models[m]}: engine", [v.engine for v in vehs])
        if e is None:
            return
        d.set_car(vehs[e])

    def show_coverage(self):
        d = self.desk
        if not d.veh:
            self.flash = "pick the car first"
            return
        self.busy("checking every factory chart for this car...")
        lines = [(d.veh.label, self.B), ""]
        for r in d.kb.coverage(d.veh):
            lines.append(f"{'Factory manual' if r['kind'] == 'fsm' else 'CHARM'}: {r['title']}  ({r['n']} pages/sections)")
        q = ",".join("?" * len(d.veh.ids))
        codes = [x[0] for x in d.kb.db.execute(f"SELECT DISTINCT code FROM dtc_procedure WHERE vehicle_id IN ({q})",
                                               d.veh.ids)]
        ok = part = bad = charm = 0
        for code in codes:
            ch = C.load(d.kb, d.veh, code)
            if not ch:
                continue
            if ch.sections[0].kind == "charm":
                charm += 1
            elif ch.ok and any(st.broken for st in ch.steps.values()):
                part += 1
            elif ch.ok:
                ok += 1
            else:
                bad += 1
        lines += ["", f"Trouble-code charts: {ok} walkable step by step, {part} walkable with a step or two to read "
                      f"from the page, {bad} damaged in conversion (shown as manual text with the page number), "
                      f"{charm} only in CHARM (text and bulletins)."]
        ok += part
        if bad > ok:
            lines.append(("Most charts for this car didn't convert cleanly. Walk them from the manual pages; "
                          "a cleaner conversion of this manual would make them walkable.", self.c["warn"]))
        self.view("coverage", lines)

    def screen_tickets(self):
        tickets = Case.recent(40)
        if not tickets:
            self.flash = "no saved tickets yet"
            return
        items = [f"{t.id}  {t.car:28.28}  {t.customer[:20]:20}  {' '.join(t.codes[:3])}" for t in tickets]
        r = self.choose("open a ticket", items)
        if r is not None:
            self.desk.save()
            self.desk.open_case(tickets[r])

    def screen_settings(self):
        d, c = self.desk, self.desk.case
        while True:
            items = [f"Labor rate:     ${c.labor_rate:g}/h", f"Tax on parts:   {c.tax_rate * 100:g}%",
                     f"Shop supplies:  {c.supplies_rate * 100:g}% of labor", "Save these as the default for new tickets"]
            r = self.choose("settings (this ticket)", items, filtering=False)
            if r is None:
                return
            if r == 0:
                v = self.number("Labor rate $/h", c.labor_rate)
                if v is not None:
                    c.labor_rate = v
            elif r == 1:
                v = self.number("Tax on parts %", c.tax_rate * 100)
                if v is not None:
                    c.tax_rate = v / 100
            elif r == 2:
                v = self.number("Shop supplies % of labor", c.supplies_rate * 100)
                if v is not None:
                    c.supplies_rate = v / 100
            elif r == 3:
                from .desk import save_config
                d.cfg.update(labor_rate=c.labor_rate, tax_rate=c.tax_rate, supplies_rate=c.supplies_rate)
                save_config(d.cfg)
                self.flash = "saved as defaults"
            d.save()

    # ------------------------------------------------------------------ 2. interview
    def screen_interview(self):
        c = self.desk.case
        sel = 0
        while True:
            items = [f"{label}:  " + (c.interview.get(k) or "-") for k, label in INTERVIEW]
            items.append("Show the manual's interview check list")
            r = self.choose("customer interview (from the factory check list)", items, sel=sel, filtering=False)
            if r is None:
                return
            sel = r
            if r == len(INTERVIEW):
                secs = [s for s in self.desk.kb.search(self.desk.veh, "Check List for Interview", kind="fsm", limit=12)
                        if "interview" in s.path.lower()] if self.desk.veh else []
                if not secs:
                    self.flash = "no interview check list in this car's manual"
                    continue
                secs.sort(key=lambda x: (not x.path.upper().startswith("ENGINE"), x.page or 0, x.id))
                lines, last = [], None
                for s in secs[:8]:
                    if s.path != last:
                        lines += ["", (s.path, self.B)]
                        last = s.path
                    lines += [(s.source, self.c["dim"])] + [l for l in render(s.body) if l.strip() != s.title.strip()]
                self.view("interview check list", lines)
                continue
            k, label = INTERVIEW[r]
            v = self.ask(label, c.interview.get(k, ""))
            if v is not None:
                c.interview[k] = v
                self.desk.save()
                if v and k in ("recent_work", "non_genuine") and self.confirm(
                        "Add it to your notes and check what the manual says about that work"):
                    self.desk.add_note(v, "history")
                    self.open_note(c.notes[-1])

    # ------------------------------------------------------------------ 3. codes and live data
    def screen_codes(self):
        d, c = self.desk, self.desk.case
        while True:
            lines = [("Stored trouble codes", self.B)]
            for code in c.codes:
                lines.append(f"  {code}  {d.kb.dtc_name(code)}")
            if not c.codes:
                lines.append(("  none", self.c["dim"]))
            if c.pending:
                lines += ["", ("Pending (seen once, not confirmed yet)", self.B)]
                lines += [f"  {code}  {d.kb.dtc_name(code)}" for code in c.pending]
            if c.code_source:
                lines.append((f"  from: {c.code_source}", self.c["dim"]))
            if c.live:
                lines += ["", ("Live data (last read)", self.B)]
                lines += [f"  {k:28} {v:>9g} {u}" for k, (v, u) in c.live.items()]
                notes = L.explain(c.live)
                if notes:
                    lines += ["", ("What that suggests (general rules of thumb, not the manual's numbers):", self.c["dim"])]
                    lines += [f"  {n}" for n in notes]
            k = self.view("codes & live data", lines, "r read car   t type codes   p pending → stored   x clear   q back",
                          keys="rtpx")
            if k is None:
                return
            if k == "r":
                self.read_car()
            elif k == "t":
                v = self.ask("Codes, separated by spaces", " ".join(c.codes))
                if v is not None:
                    d.set_codes(re.split(r"[\s,]+", v), "typed")
            elif k == "p":
                d.set_codes(c.codes + c.pending, c.code_source + " + pending")
            elif k == "x" and self.confirm("Clear the codes on this ticket (not on the car)"):
                c.pending = []
                d.set_codes([], "")

    def read_car(self):
        d, c = self.desk, self.desk.case
        self.busy("reading the car (key on)... this can take 20 seconds")
        res = L.read_car(d.kb)
        if res["codes"] or res["pending"] or res["via"]:
            d.set_codes(res["codes"], f"{res['via']} at {time.strftime('%m-%d %H:%M')}")
            c.pending = [x for x in res["pending"] if x not in res["codes"]]
            c.live = res["live"]
            if res["vin"] and not c.vin:
                c.vin = res["vin"]
            d.save()
        self.view("read car", [(x, 0) for x in res["log"]])

    # ------------------------------------------------------------------ 4. diagnose
    def screen_diagnose(self):
        d, c = self.desk, self.desk.case
        if not d.veh:
            self.flash = "pick the car first (1)"
            return
        while True:
            groups = d.chart_groups()
            items, acts = [], []
            for ch, codes in groups:
                if ch is None:
                    items.append((f"{', '.join(codes)}: no procedure for this code in the manuals for this car", self.c["dim"]))
                    acts.append(("none", codes))
                    continue
                w = c.walks.get(d.key(ch), {})
                state = ("finished: " + w["result"][:50]) if w.get("result") else (
                    f"at step {w['trail'][-1]['step']}" if w.get("trail") else "not started")
                tag = "" if ch.ok else ("  [manual text only]" if ch.sections[0].kind == "fsm" else "  [CHARM]")
                items.append((f"{', '.join(codes)}: {ch.title[:60]}{tag}  ({state})",
                              self.c["good"] if w.get("result") else 0))
                acts.append(("chart", ch))
            items.append(("Symptoms without codes: the manual's general diagnostic table", 0))
            acts.append(("symptoms", None))
            for code in c.codes:
                if d.kb.bulletins(d.veh, code):
                    items.append((f"Subaru service bulletins for {code}", 0))
                    acts.append(("tsb", code))
            r = self.choose("diagnose", items, "Enter open   Esc back", filtering=False)
            if r is None:
                return
            kind, what = acts[r]
            if kind == "chart":
                self.walk_chart(what)
            elif kind == "symptoms":
                self.show_symptoms()
            elif kind == "tsb":
                self.show_bulletins(what)

    def walk_chart(self, ch):
        d = self.desk
        if not ch.ok:
            self.chart_text(ch)
            return
        w = d.walk(ch)
        while True:
            lines = self.walk_lines(ch, w)
            st = w.step()
            hint = ("y yes  n no  type a reading + Enter  b back  g go to step  m manual pages  i about  q back"
                    if st else "g go to the step the page says  b back  m manual pages  q back" if w.broken_step()
                    else "j make this a job  b back a step  m manual pages  q back")
            k = self.view(f"{ch.code if not ch.via else ch.via} chart", lines, hint,
                          keys="ynbgmijq0123456789.-", top=10 ** 6)
            if k is None or k == "q":
                d.save_walk(ch, w)
                return
            if k in "yn" and st:
                w.answer("Yes" if k == "y" else "No")
            elif (k == "#" or k in "0123456789.-") and st:   # "#": the window's Enter-a-reading button
                if not st.limit:
                    self.flash = "this check has no limit to compare a reading with: answer y or n"
                    continue
                v = self.ask(f"Reading ({st.limit.unit or 'value'}; several separated by spaces)", "" if k == "#" else k)
                if v:
                    ans, why = w.measure(v)
                    self.flash = f"{ans}: {why}" if ans else why
            elif k == "b":
                if not w.undo():
                    self.flash = "at the start"
            elif k == "g":
                v = self.ask("Go to step number (your call; it's noted on the ticket)")
                if v and v.isdigit() and not w.jump(int(v)):
                    self.flash = f"there's no step {v}"
            elif k == "m":
                self.chart_text(ch)
            elif k == "i":
                self.view("about this code", [(ch.title, self.B), (ch.source, self.c["dim"]), ""] + ch.head)
            elif k == "j" and w.result:
                if w.result.kind == "other_code":
                    self.flash = "the manual says to diagnose the other code(s) first: open them from the list"
                    continue
                job = d.job_from_walk(ch, w)
                d.save_walk(ch, w)
                d.add_job(job)
                self.flash = f"job added: {job.title}"
                self.edit_job(job)
            d.save_walk(ch, w)

    def walk_lines(self, ch, w):
        B, dim = self.B, self.c["dim"]
        lines = [(ch.title + (f"   (the manual's page for {ch.via} says to use this chart)" if ch.via else ""), B),
                 (ch.source, dim)]
        if ch.head and ch.head[0] == C.ORDER_NOTE:
            lines.append(("YES/NO taken from the printed order (this manual prints them as pictures).", dim))
        lines.append("")
        if w.trail:
            lines.append(("So far", B))
        for a in w.trail:
            st = ch.steps.get(a.step)
            if a.answer == "jump":
                lines.append((f"  {a.step:>2}  → {a.detail} (your call)", self.c["warn"]))
                continue
            how = {"scan": "from the scan", "measured": "measured", "mechanic": "you"}[a.how]
            lines.append((f"  {a.step:>2}  {st.title[:60]}  →  {a.answer}  ({how}{': ' + a.detail if a.detail else ''})",
                          self.c["good"] if a.how != "mechanic" else 0))
        lines.append("")
        st = w.step()
        if st:
            lines += [(f"STEP {st.n}   {st.title}", B | self.c["acc"])]
            lines += [f"    {x}" for x in st.instructions]
            lines += ["", (f"  ?  {st.question}", B)]
            if st.limit:
                lines.append((f"     limit: {st.limit.describe()}. Type the reading and the deck decides.", self.c["acc"]))
            lines += ["", (f"  Yes →  {st.yes.text}", 0), (f"  No  →  {st.no.text}", 0)]
        elif w.broken_step():
            bs = w.broken_step()
            lines += [(f"STEP {bs.n}   (didn't convert cleanly)", B | self.c["warn"])]
            lines += [f"    {x}" for x in bs.raw]
            lines += ["", ("This step's question or its YES/NO answers were lost in conversion, so the deck won't "
                           f"answer it. Read step {bs.n} on the manual page ({ch.source}), do the check, then press g "
                           "and type the step number the manual sends you to (or press m for the full text).",
                           self.c["warn"])]
        elif w.result:
            kind = w.result.kind
            color = {"action": self.c["warn"], "other_code": self.c["acc"], "intermittent": self.c["acc"]}.get(kind, 0)
            head = {"action": "THE MANUAL SAYS", "other_code": "THE MANUAL SAYS: CHECK THE OTHER CODE(S) FIRST",
                    "intermittent": "NOTHING FAULTY RIGHT NOW (intermittent)", "end": "END OF CHART"}[kind]
            lines += [(head, B | color), f"    {w.result.text}"]
            if kind == "action":
                lines += ["", ("Press j to turn this into a job (procedure, book time, parts, quote).", self.c["acc"])]
            if w.result.step:
                lines += [("Then the chart continues at step %d: press b and answer again after the repair." %
                           w.result.step, dim)]
        return lines

    def chart_text(self, ch):
        lines = [(ch.title, self.B), (ch.source, self.c["dim"])]
        if ch.problems:
            lines += [("This chart didn't convert cleanly, so it can't be walked step by step here: "
                       + "; ".join(ch.problems[:4]) + ". Follow it from the manual text below or the printed page.",
                       self.c["warn"])]
        for s in ch.sections:
            lines += ["", (f"— {s.source} —", self.c["dim"])] + render(s.body)
        k = self.view("manual pages", lines, "j add a job from this   q back", keys="j")
        if k == "j":
            t = self.ask("What's the repair (your words)")
            if t:
                job = self.desk.job_from_text(t, f"{ch.source}: {ch.title} (read from the manual text)")
                self.desk.add_job(job)
                self.attach_procedure(job)
                self.edit_job(job)

    def show_symptoms(self):
        d = self.desk
        secs = d.kb.symptom_tables(d.veh)
        more = d.kb.search(d.veh, "Engine Trouble in General", kind="fsm", limit=6)
        if not secs and not more:
            self.flash = "no general diagnostic table in this car's manual"
            return
        lines = [("The manual's symptom tables: parts to check, in the manual's order", self.B), ""]
        for s in secs:
            lines += [(s.source, self.c["dim"])] + render(s.body, cells=" ") + [""]
        for s in more:
            if "trouble in general" in s.path.lower():
                lines += [(s.path, self.B), (s.source, self.c["dim"])] + render(s.body) + [""]
        self.view("symptoms", lines)

    def show_bulletins(self, code):
        lines = []
        for s in self.desk.kb.bulletins(self.desk.veh, code):
            lines += [(s.path.split(">")[-1].strip(), self.B), (s.source, self.c["dim"])] + render(s.body) + [""]
        self.view(f"service bulletins: {code}", lines)

    # ------------------------------------------------------------------ 5. notes and theories
    def screen_notes(self):
        d, c = self.desk, self.desk.case
        while True:
            items = [f"{KIND_LABEL.get(n.kind, n.kind):12} {n.text}" for n in c.notes]
            r = self.choose("your notes & theories", items,
                            "a add   Enter open   d delete   Esc back", keys="ad", filtering=False)
            if r is None:
                return
            if isinstance(r, tuple):
                key, idx = r
                if key == "a":
                    self.add_note()
                elif key == "d" and idx is not None and self.confirm("Delete this note"):
                    c.notes.pop(idx)
                    d.save()
                continue
            self.open_note(c.notes[r])

    def add_note(self):
        kinds = ["history", "reused", "hypothesis", "note"]
        r = self.choose("what kind", ["Recent work done on the car", "A part that was reused (not replaced)",
                                      "Your theory of the cause", "Anything else"], filtering=False)
        if r is None:
            return
        t = self.ask({"history": "What was done", "reused": "Which part was reused (e.g. exhaust gasket)",
                      "hypothesis": "Your theory (e.g. exhaust leak at the head)", "note": "Note"}[kinds[r]])
        if t:
            self.desk.add_note(t, kinds[r])
            self.open_note(self.desk.case.notes[-1])

    def open_note(self, n):
        d = self.desk
        lines = [(f"{KIND_LABEL.get(n.kind, n.kind)}: {n.text}", self.B), ""]
        if n.kind in ("reused", "history"):
            self.busy("looking for what the manual says about it...")
            hits = d.reuse_check(n.text)
            if hits:
                lines.append(("Where the manual says to use a new one:", self.c["warn"] | self.B))
                for sec, said in hits:
                    lines.append(f"  {R.topic_name(sec)}  ({sec.source})")
                    lines += [f"     \"{x}\"" for x in said]
                lines += ["", ("The manual only says to replace it; it doesn't say what happens if you don't. "
                               "If you suspect it, press t to test it as your theory.", self.c["dim"])]
            else:
                lines.append(("The manual's procedures for that don't mention replacing it with a new one "
                              "(try other words, like the manual's part name).", self.c["dim"]))
        if n.kind == "hypothesis":
            lines += [("Your call, not the manual's. To check it, use the manual's inspection for the part, "
                       "and keep the factory chart open: its own steps may already cover it.", self.c["dim"]), "",
                      ("j  make it a job (manual procedure, book time, parts)", self.c["acc"])]
        k = self.view("note", lines, "t test as theory   j make a job   q back", keys="tj")
        if k == "t":
            t = self.ask("Your theory", f"{n.text} ({KIND_LABEL.get(n.kind, n.kind).lower()}) is causing the fault")
            if t:
                d.add_note(t, "hypothesis")
                self.flash = "added as your theory"
        elif k == "j":
            job = d.job_from_text(n.text, f"your call: {KIND_LABEL.get(n.kind, n.kind).lower()} '{n.text}'")
            d.add_job(job)
            self.attach_procedure(job)
            self.edit_job(job)

    # ------------------------------------------------------------------ 6. jobs and quote
    def screen_jobs(self):
        d, c = self.desk, self.desk.case
        while True:
            q = d.quote()
            items = []
            for j in c.jobs:
                hrs = sum(l.hours for l in j.labor)
                items.append(f"{j.title[:50]:50}  {hrs:4.1f} h  {len(j.parts)} part(s)")
            items.append((f"QUOTE  labor ${q.labor:,.2f}  parts ${q.parts:,.2f}  tax ${q.tax:,.2f}  "
                          f"total ${q.total:,.2f}" + ("  (some prices missing)" if q.missing else ""), self.B))
            r = self.choose("jobs, parts & quote", items, "Enter open   a add a job   d delete   Esc back", keys="ad", filtering=False)
            if r is None:
                return
            if isinstance(r, tuple):
                key, idx = r
                if key == "a":
                    t = self.ask("Job (e.g. replace spark plugs)")
                    if t:
                        job = d.add_job(d.job_from_text(t, "added by you"))
                        self.attach_procedure(job)
                        self.edit_job(job)
                elif key == "d" and idx is not None and idx < len(c.jobs) and self.confirm("Delete this job"):
                    c.jobs.pop(idx)
                    d.save()
                continue
            if r < len(c.jobs):
                self.edit_job(c.jobs[r])
            else:
                self.show_quote()

    def attach_procedure(self, job):
        """Let the mechanic pick the manual procedure for a job (search with the job's words)."""
        d = self.desk
        if not d.veh:
            return
        text = job.title
        while True:
            cands = R.procedure_candidates(d.kb, d.veh, text, limit=14)
            items = [f"{R.topic_name(s)}   ({s.source})" for s in cands] + ["Search with other words",
                                                                              "Skip: no manual procedure"]
            r = self.choose(f"manual procedure for: {job.title[:40]}", items)
            if r is None or r == len(cands) + 1:
                return
            if r == len(cands):
                t = self.ask("Search the manual for", text)
                if t:
                    text = t
                continue
            if cands[r].id not in job.procedures:
                job.procedures.append(cands[r].id)
            d.save()
            return

    def edit_job(self, job):
        d = self.desk
        while True:
            items, acts = [], []
            items.append((f"{job.title}", self.B))
            acts.append(("title", None))
            items.append((f"   why: {job.reason}", self.c["dim"]))
            acts.append(("none", None))
            for sid in job.procedures:
                s = d.kb.section(sid)
                items.append(f"   manual: {R.topic_name(s)}  ({s.source})  → Enter to read")
                acts.append(("proc", sid))
            for i, l in enumerate(job.labor):
                items.append(f"   labor:  {l.what[:44]:44} {l.hours:5.2f} h   [{l.source[:40]}]")
                acts.append(("labor", i))
            for i, p in enumerate(job.parts):
                price = f"${p.unit_price:,.2f}" if p.unit_price is not None else "PRICE NEEDED"
                items.append((f"   part:   {p.what[:30]:30} {p.part_number:12} ×{p.qty:g} {price:>12}   [{p.source[:30]}]",
                              self.c["warn"] if p.unit_price is None else 0))
                acts.append(("part", i))
            newbies = d.must_be_new(job)
            for sec, line in newbies:
                items.append((f"   must be new (manual): {line[:70]}", self.c["warn"]))
                acts.append(("new", line))
            r = self.choose("job", items, "c CHARM hours & parts   s attach procedure   h add hours   p add part   "
                            "Enter edit   d delete line   Esc back", keys="cshpd", filtering=False)
            if r is None:
                d.save()
                return
            if isinstance(r, tuple):
                key, idx = r
                if key == "c":
                    self.charm_pick(job)
                elif key == "s":
                    self.attach_procedure(job)
                elif key == "h":
                    t = self.ask("Labor: what")
                    hrs = self.number("Hours") if t else None
                    if t and hrs is not None:
                        job.labor.append(R.LaborLine(t, hrs, "your estimate"))
                elif key == "p":
                    self.add_part(job)
                elif key == "d" and idx is not None:
                    kind, i = acts[idx]
                    if kind == "labor":
                        job.labor.pop(i)
                    elif kind == "part":
                        job.parts.pop(i)
                    elif kind == "proc":
                        job.procedures.remove(i)
                d.save()
                continue
            kind, val = acts[r]
            if kind == "title":
                t = self.ask("Job title", job.title)
                if t:
                    job.title = t
            elif kind == "proc":
                self.read_procedure(val)
            elif kind == "labor":
                l = job.labor[val]
                hrs = self.number(f"Hours for {l.what}", l.hours)
                if hrs is not None and hrs != l.hours:
                    l.hours = hrs
                    if not l.source.startswith("your"):
                        l.source = "changed by you (book: " + l.source + ")"
            elif kind == "part":
                p = job.parts[val]
                price = self.number(f"Price each for {p.what} {p.part_number}", "" if p.unit_price is None else p.unit_price)
                if price is not None:
                    p.unit_price = price
                    p.source = "your price"
                qty = self.number("Quantity", p.qty)
                if qty is not None:
                    p.qty = qty
            elif kind == "new":
                if self.confirm(f"Add a parts line for: {val[:50]}"):
                    self.add_part(job, what=re.sub(r"^\W*\d*\)?\s*", "", val)[:60], source="manual: must be new")
            d.save()

    def add_part(self, job, what="", source="you"):
        t = self.ask("Part", what)
        if not t:
            return
        pn = self.ask("Part number (blank if none)", "") or ""
        qty = self.number("Quantity", 1) or 1
        price = self.number("Price each (blank if unknown)")
        job.parts.append(R.PartLine(t, pn.upper(), qty, price, source))

    def charm_pick(self, job):
        d = self.desk
        text = job.title
        while True:
            rows = d.labor_options(text)
            items = []
            for r in rows:
                op = " > ".join(x.strip() for x in r["operation"].split(">")[-3:])
                bits = [f"{r['hours']:g} h" if r["hours"] else "", r["part_number"] or "",
                        f"${r['price']}" if r["price"] and not str(r["price"]).startswith("$") else (r["price"] or "")]
                items.append(f"{op[:60]:60} {'  '.join(b for b in bits if b)}")
            items.append("Search CHARM with other words")
            r = self.choose(f"CHARM book times & part numbers: {text[:30]}", items,
                            "Enter add to the job   Esc done")
            if r is None:
                return
            if r == len(rows):
                t = self.ask("CHARM search", text)
                if t:
                    text = t
                continue
            d.add_charm_row(job, rows[r])
            self.flash = "added (Esc when done)"

    def read_procedure(self, sid):
        d = self.desk
        lines = []
        for s in d.kb.topic_sections(sid):
            lines += [(f"{s.title}", self.B), (s.source, self.c["dim"])]
            new = R.must_be_new(s)
            if new:
                lines.append(("  must be new: " + " / ".join(new), self.c["warn"]))
            tq = R.torque_lines(s)
            if tq:
                lines.append(("  torque:", self.c["acc"]))
                lines += [(f"    {v:32} {w[:80]}", self.c["acc"]) for w, v in tq]
            lines += [""] + render(s.body) + [""]
        self.view("procedure", lines)

    def show_quote(self):
        d, c = self.desk, self.desk.case
        q = d.quote()
        lines = [(f"Quote for {c.customer or 'customer'}: {c.car}", self.B), ""]
        for kind, text, amt in q.lines:
            if kind == "job":
                lines.append((f"  {text}", self.B))
                continue
            lines.append((f"    {text[:68]:68} {('$%.2f' % amt) if amt is not None else 'PRICE NEEDED':>12}",
                          self.c["warn"] if amt is None else 0))
        lines += ["", f"  {'Labor':70} {q.labor:>12,.2f}", f"  {'Parts':70} {q.parts:>12,.2f}"]
        if q.tax:
            lines.append(f"  {'Tax on parts':70} {q.tax:>12,.2f}")
        if q.supplies:
            lines.append(f"  {'Shop supplies':70} {q.supplies:>12,.2f}")
        lines += [(f"  {'TOTAL':70} {q.total:>12,.2f}", self.B), ""]
        if q.missing:
            lines.append((f"Not included until priced: {'; '.join(q.missing)}", self.c["warn"]))
        lines.append(("CHARM list prices are old; check current prices. Book hours are CHARM's.", self.c["dim"]))
        k = self.view("quote", lines, "r rates   q back", keys="r")
        if k == "r":
            self.screen_settings()

    # ------------------------------------------------------------------ 7. manual search
    def screen_search(self):
        d = self.desk
        if not d.veh:
            self.flash = "pick the car first (1)"
            return
        text = ""
        while True:
            text = self.ask("Search this car's manual", text)
            if not text:
                return
            hits = d.kb.search(d.veh, text, limit=30) or d.kb.search(d.veh, text, limit=30, any_word=True)
            if not hits:
                self.flash = "nothing found: try the manual's words"
                continue
            while True:
                r = self.choose(f"manual: {text}", [short(s) for s in hits])
                if r is None:
                    break
                s = hits[r]
                lines = [(s.path, self.B), (s.source, self.c["dim"])]
                tq = R.torque_lines(s)
                if tq:
                    lines += [("torque:", self.c["acc"])] + [(f"  {v:32} {w[:80]}", self.c["acc"]) for w, v in tq]
                lines += [""] + render(s.body)
                self.view("manual", lines)

    # ------------------------------------------------------------------ 8. print
    def screen_print(self):
        try:
            path = self.desk.work_order()
        except Exception as e:
            self.flash = f"couldn't write the work order: {e}"
            return
        k = self.view("work order", [("Written:", self.B), path, "",
                                 "Press o to open it in the browser and print from there, or copy it off the deck.",
                                 "It has the diagnosis with how each answer was decided, the quote, the parts list "
                                 "and the manual's repair procedures with torque values."], "o open in browser   q back", keys="o")
        if k == "o":
            import subprocess
            if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                self.flash = "no screen attached to this session (over ssh): copy the file instead"
                return
            try:
                subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
                self.flash = "opening in the browser"
            except OSError:
                self.flash = "xdg-open isn't installed: open the file from the file manager"


def main(desk):
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(lambda scr: UI(scr, desk).run())
