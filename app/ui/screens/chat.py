"""Screen 3, Brainstorm / chat: chat, debate, chart reads."""
import charm_pics
import json
import os
import re
import sqlite3
import theme as T
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget
from ui_fit import ReflowRow
from ui.config import CFG
from ui.prompts import CHAT_STYLE
from ui.context import PAGE, focus_specs, fsm_hits, squeeze
from ui.workers import ChartWorker


class ChatScreen:
    def _chat_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_chat = QTextEdit()
        self.txt_chat.setReadOnly(True)
        lay.addWidget(self.txt_chat)
        row = QHBoxLayout()
        self.in_chat = QLineEdit()
        self.in_chat.setPlaceholderText("Ask about the case (it already knows the Intake). /torque <part> and /fsm <words> "
                                        "still work typed")
        self.in_chat.returnPressed.connect(self.act_chat)
        b = QPushButton("SEND")
        b.clicked.connect(self.act_chat)
        row.addWidget(self.in_chat)
        row.addWidget(b)
        lay.addLayout(row)
        acts = ReflowRow()
        for label, tip, fn in [
            ("COMMON ISSUES", "What this model is known for and at what mileage (NHTSA, recalls, TSBs, forums). "
             "Good for a pre-purchase call", lambda: self._chat_cmd("/common")),
            ("CHARTS", "The manual's diagnostic chart pictures for the case's codes (or codes typed above), read "
             "step by step by the PC's vision model", lambda: self._chat_cmd("/chart")),
            ("DEBATE", "Weighs the competing explanations for the case, or for the question typed above (or end your "
             "text with /debate): evidence for and against each, and the test that settles it",
             lambda: self._chat_cmd("/debate")),
            ("SAVE DEBATE", "Add the last debate to the case, so the verdict sees it", lambda: self._chat_cmd("/save")),
            ("WILDCARD", "Dr. House: 5 unlikely but possible causes", lambda: self._chat_cmd("/wildcard")),
            ("SAVE WILDCARD", "Add the last wildcard ideas to the case, marked as long shots",
             lambda: self._chat_cmd("/savewild")),
            ("CLEAR", "Clear the chat and start it over with the case", self.act_chat_clear),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(fn)
            acts.add(btn)
        lay.addWidget(acts)
        self.stack.addWidget(w)

    def _chat_cmd(self, cmd):
        arg = self.in_chat.text().strip()
        if cmd in ("/torque", "/fsm") and not arg:
            self._say(self.txt_chat, f"Type a part or words first, then tap {'TORQUE' if cmd == '/torque' else 'MANUAL'}.",
                      T.AMBER)
            return
        self.in_chat.setText(f"{cmd} {arg}".strip())
        self.act_chat()

    CHAT_NOTES_MAX = 4000

    def _note_chat(self, msg):
        """Everything the mechanic types in chat goes on the case's notes list (filed with the visit), so Final
        Diagnosis sees what he replaced, checked and suspects. He can edit the list on the Final Diagnosis screen."""
        line = " ".join(msg.split())
        if not line:
            return
        notes = (self.session.get("chat_notes") or "") + "\n- " + line
        self.session.set("chat_notes", notes.strip()[-self.CHAT_NOTES_MAX:])

    def act_chat_clear(self, keep_screen=False):
        if not keep_screen:
            self.txt_chat.clear()
        self.chat_started = False
        self.chat_hist = []

    def act_chat(self):
        msg = self.in_chat.text().strip()
        if not msg or self._busy():
            return
        self.in_chat.clear()
        self._say(self.txt_chat, msg, T.FOREST_TEXT, who="You")

        low = msg.lower()
        m = re.match(r"(?s)(.*\S)\s+(/debate|/wildcard)$", msg.strip(), re.I)
        if m and not m.group(1).startswith("/"):
            self._note_chat(m.group(1))
            msg = f"{m.group(2).lower()} {m.group(1)}"
            low = msg.lower()
        bare = {"/torque": "/torque <part>", "/spec": "/spec <part>", "/fsm": "/fsm <words>",
                "/part": "/part <name or OEM number>", "/q": "/q <question>", "/question": "/question <question>",
                "/fluids": "/fluids <system, e.g. engine oil>", "/fluid": "/fluid <system>"}
        if low in bare:
            self._say(self.txt_chat, f"  usage: {bare[low]}", T.AMBER)
            return
        if low.startswith(("/fluids ", "/fluid ")):
            hits = fsm_hits(self.retriever, msg.split(" ", 1)[1] + " fluid capacity specification", k=2, chars=700)
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no manual match")
            return
        if low.startswith(("/torque ", "/spec ")):
            term = re.sub(r"\b(bolts?|nuts?|torque|specs?|for|the)\b", " ", msg.split(" ", 1)[1], flags=re.I)
            term = term.strip() or msg.split(" ", 1)[1]
            rows = focus_specs(term, self.retriever.specs(term, limit=30))[:10] or self.retriever.specs(term)
            self._say(self.txt_chat, "\n".join("  • " + r for r in rows) or "  no spec found", T.OK)
            return
        if low.startswith("/fsm "):
            hits = fsm_hits(self.retriever, msg.split(" ", 1)[1], k=3, chars=700)
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no manual match")
            return
        if low.startswith("/part "):
            self._say(self.txt_chat, self.part_text(msg.split(" ", 1)[1].strip()), T.OK)
            return
        if low == "/debate" or low.startswith("/debate "):
            self.act_debate(msg[len("/debate"):].strip())
            return
        if low == "/save":
            if not self.last_debate:
                self._say(self.txt_chat, "  nothing to save: run DEBATE first", T.AMBER)
            else:
                self.session.set("saved_debate", self.last_debate)
                self._say(self.txt_chat, "  [✓] debate saved to the case; the verdict will see it", T.OK)
            return
        if low == "/savewild":
            if not getattr(self, "last_wildcard", ""):
                self._say(self.txt_chat, "  nothing to save: run WILDCARD first", T.AMBER)
            else:
                self.session.set("saved_wildcard", self.last_wildcard)
                self._say(self.txt_chat, "  [✓] wildcard ideas saved to the case as long shots; the verdict will "
                          "weigh them", T.OK)
            return
        if low in ("/chart", "/charts") or low.startswith(("/chart ", "/charts ")):
            self.act_charts(msg.split(" ", 1)[1] if " " in msg else "")
            return
        if low.startswith(("/maintenance", "/maint", "/addmaint")):
            if "severe" in low:
                self.cmb_maint_kind.setCurrentIndex(1)
            self.switch(PAGE["maint"])
            return
        if low == "/common" or low.startswith("/common "):
            self.act_common()
            return
        if low == "/wildcard" or low.startswith("/wildcard "):
            self.act_wildcard(msg[len("/wildcard"):].strip())
            return
        if low.startswith(("/question ", "/q ")):
            q = msg.split(" ", 1)[1]
            self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
            self._run("fast", f"Answer briefly and factually. No speculation.\n\nQUESTION: {q}", self.txt_chat,
                      lambda t: None, keep_history=False)
            return
        if low.startswith("/"):
            self._say(self.txt_chat, "  commands: /maintenance (opens the MAINTENANCE screen), /chart [codes], /common, /torque <part>, /fsm <words>, /fluids <system>, /part <name or number>, /debate [q] (or end your text with /debate), /save, /wildcard [q], /savewild, /q <text>",
                      T.AMBER)
            return

        self.session.log("user", msg)
        self._record("chat", sender="user", message=msg)
        self._note_chat(msg)
        if not self.chat_started:
            self.chat_hist = []
            self.chat_started = True
        anchor = " ".join(x for x in (self.session.get("dtcs"), self.session.get("symptoms")) if x)
        recent = " ".join(u for u, _ in self.chat_hist[-2:])

        def go(ctx):
            hist, used = [], 0
            for u, d in reversed(self.chat_hist):
                turn = f"Mechanic: {u}\nYou: {d}"
                if used + len(turn) > 1600:
                    break
                hist.insert(0, turn)
                used += len(turn)
            dc = self._data_check()
            if dc and dc != getattr(self, "_shown_dc", ""):
                self._shown_dc = dc
                self._say(self.txt_chat, dc, T.AMBER)
            prompt = ((f"{dc}\n\n" if dc else "") +
                      f"CASE:\n{self.session.case_summary()}\n\nFSM DATA (the [MANUAL DIAGNOSTIC PROCEDURE] and [DTC] "
                      "entries are the manual's diagnostic tree for this car's codes: use it as your guide to where the "
                      "mechanic is and what to check next, from what's already been checked. Manual text comes first "
                      "and wins; [REPORTED CAUSES] and [COMMUNITY REPORTS] are fixes owners confirmed on real cars, "
                      "a secondary hint, never a spec or a step):\n"
                      f"{ctx or '(none)'}\n\n"
                      + ("CONVERSATION SO FAR:\n" + "\n".join(hist) + "\n\n" if hist else "")
                      + f"Mechanic: {msg}\n"
                      "(If the mechanic puts forward a cause of their own, weigh it on the evidence: what in the case "
                      "fits it, what doesn't, and the quickest test that would confirm or clear it, from the manual's "
                      "pages for that part when there are any. Not being on the diagnostic tree is no reason to set it "
                      "aside: the tree only covers what the computer can detect. Don't agree just because they said it "
                      "either.)\n" + CHAT_STYLE)
            role = "deep" if CFG.get("chat_model", "fast") == "deep" and "deep" not in CFG.get("stand_in", {}) else "fast"
            if self.mgr.home and not self.mgr.pc_why:
                role = "fast"

            def done(t):
                self.session.log(role, t)
                self._record("chat", sender=role, message=t)
                if "[LLM error" not in t:
                    self.chat_hist.append((msg, t.strip()[:600]))

            self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
            self._run(role, prompt, self.txt_chat, done, keep_history=False)

        self._lookup(f"{anchor} {recent} {msg}".strip(), 5000, go, scraped=True, verified_only=True)

    def act_debate(self, question):
        """The competing explanations weighed against the same evidence (no role-played sides; 10-25),
        on the fast model. One-off: not added to chat memory."""
        case = self.session.case_summary()
        asked = bool(question)
        question = question or "What is causing the symptoms in this case?"

        def go(ctx):
            prompt = (f"CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n" +
                      (f"THE MECHANIC'S QUESTION (debate exactly this, using the case as evidence): {question}\n\n"
                       if asked else f"QUESTION: {question}\n\n") +
                      "Weigh the competing explanations against each other. If the question names ideas, those are "
                      "the explanations; otherwise use the 2 or 3 the evidence supports best. No characters or "
                      "personas: argue the ideas themselves, using only the case, live data and manual text above, "
                      "and quote the actual numbers.\n"
                      "For each explanation: IDEA (one line), FOR (the evidence that fits it), AGAINST (evidence that "
                      "doesn't fit, including anything listed as verified good), COST (rough effort or parts if it's "
                      "right).\n"
                      "Then WHAT SETTLES IT: the one test that tells them apart, and what each result means.\n"
                      "Do not pick a winner. Keep each explanation under 100 words.")
            self._say(self.txt_chat, "", T.AMBER, who="Debate")
            self._run("fast", prompt, self.txt_chat, self._debate_done, keep_history=False)

        self._lookup(case + " " + question, 4500, go)

    def _rejected_causes(self):
        """Causes the baked card's source check threw out, for this case's codes and car (and why).
        Read fresh from the kb every time: a rebake replaces the cards. Only /wildcard uses these."""
        codes = list(dict.fromkeys(re.findall(r"\b[PBCU]\d{4}\b", self.session.get("dtcs") or "")))
        kb = CFG.get("kb_db") or ""
        if not codes or not os.path.isfile(kb):
            return []
        sql = "SELECT code, dropped_json FROM dtc_card WHERE code = ?"
        args_car = []
        if CFG.get("kb_model"):
            sql += " AND model = ?"
            args_car.append(CFG["kb_model"])
        if CFG.get("kb_year"):
            sql += " AND ? BETWEEN year_min AND year_max"
            args_car.append(CFG["kb_year"])
        out = []
        try:
            with sqlite3.connect(f"file:{kb}?mode=ro", uri=True) as db:
                for code in dict.fromkeys(codes):
                    for c, dropped in db.execute(sql, [code] + args_car):
                        try:
                            items = json.loads(dropped or "[]")
                        except ValueError:
                            continue
                        for d in items:
                            if isinstance(d, dict) and d.get("cause"):
                                line = f"{c}: {d['cause']} (rejected: {d.get('reason') or 'no reason given'})"
                                if line not in out:
                                    out.append(line)
        except sqlite3.Error:
            return []
        return out[:15]

    def act_charts(self, arg=""):
        """CHARM's diagnostic pages for the codes: text and pictures now, then the PC's reading of each picture
        as it lands (kept in the case for the PC models). Python picks the pages; the model only reads them."""
        codes = list(dict.fromkeys(re.findall(r"\b[PBCU]\d{4}\b", (arg or self.bub_dtcs.text()).upper())))[:3]
        r, db = self.retriever, getattr(self.retriever, "db", None)
        if not codes:
            self._say(self.txt_chat, "  No codes: read the car or type one, e.g. /chart P0301.", T.AMBER)
            return
        if db is None or not hasattr(r, "_veh_filter") or not getattr(r, "vehicle_ids", None):
            self._say(self.txt_chat, "  Pick the car (model and year) on Intake first, so the charts are this car's.",
                      T.AMBER)
            return
        vf, vargs = r._veh_filter("s.id")
        names, done = [], 0
        for code in codes:
            try:
                rows = db.execute(
                    "SELECT s.path, s.body, s.images FROM dtc_procedure p JOIN section s ON s.id=p.section_id "
                    f"JOIN doc d ON d.id=s.doc_id WHERE d.kind='charm' AND p.code=?{vf} GROUP BY s.id ORDER BY s.id "
                    "LIMIT 2", [code] + vargs).fetchall()
            except sqlite3.Error:
                rows = []
            if not rows:
                self._say(self.txt_chat, f"  {code}: no CHARM diagnostic page for this car.", T.AMBER)
                continue
            for path, body, imgs in rows:
                try:
                    figs = [n for n in json.loads(imgs or "[]") if charm_pics.is_charm(n)]
                except ValueError:
                    figs = []
                figs = figs[:int(CFG.get("charm_pics_max") or 8)]
                self._say_hits(self.txt_chat, [(squeeze(r._fmt(path, None, body, 1200, "charm")), figs)])
                names += [(code, path, n) for n in figs if n not in [x[2] for x in names]]
                done += 1
        if not names:
            if done:
                self._say(self.txt_chat, "  These pages have no pictures to read.", T.MUTED)
            return
        self.chart_meta = {n: (code, path, i + 1) for i, (code, path, n) in enumerate(names)}
        if getattr(self, "chart_worker", None) is not None and self.chart_worker.isRunning():
            self.chart_worker.stop_flag.set()
        self.session.set("chart_reads", "")
        self._say(self.txt_chat, f"  Asking the PC to read {len(names)} chart pictures. Readings show here as they "
                  "come; you can keep working.", T.MUTED)
        w = ChartWorker([n for *_, n in names])
        w.result.connect(lambda n, res, w=w: self._chart_read(n, res) if not w.stop_flag.is_set() else None)
        w.note.connect(lambda t, w=w: self._say(self.txt_chat, "  " + t, T.MUTED) if not w.stop_flag.is_set()
                       else None)
        self.chart_worker = w
        w.start()

    def _chart_read(self, name, res):
        code, path, i = self.chart_meta.get(name, ("", "", 0))
        label = f"{code} picture {i} ({path.split(' > ')[-1] if path else 'CHARM'})"
        state = res.get("state")
        if state == "skipped":
            return
        if state == "error":
            self._say(self.txt_chat, f"  {label}: the PC couldn't read it ({res.get('why', '')[:120]}). Tap CHARTS "
                      "to try again.", T.AMBER)
            return
        text = (res.get("text") or "").strip()
        if text.upper().startswith("NOT A CHART"):
            self._say(self.txt_chat, f"  {label}: {text.splitlines()[0][len('NOT A CHART:'):].strip()}", T.MUTED)
            return
        self._say(self.txt_chat, "", T.PURPLE_TEXT, who=f"Chart reading, {label}")
        self._say(self.txt_chat, text)
        bad = res.get("unverified")
        if bad:
            warn = f"Check these numbers on the picture (plain OCR didn't see them): {', '.join(bad[:15])}"
        elif bad is None:
            warn = "Numbers not cross-checked (no plain OCR on the PC for this picture)."
        else:
            warn = "Every number matched plain OCR of the picture."
        self._say(self.txt_chat, "  " + warn + " The picture above is the source.", T.AMBER if bad or bad is None
                  else T.OK)
        reads = self.session.get("chart_reads") or ""
        entry = f"[{code} chart, {path}, picture {i}]\n{text}" + (f"\n(unverified numbers: {', '.join(bad)})" if bad else "")
        self.session.set("chart_reads", (reads + "\n\n" + entry).strip())
