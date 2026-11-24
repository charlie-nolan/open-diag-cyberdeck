"""Final Diagnosis: the verdict, its steps and the per-issue tabs."""
import answer_check
import html
import re
import sanity_check
import sys
import theme as T
import verdict_steps
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget)
from ui_fit import ReflowRow
from ui.config import CFG, PS
from ui.context import (
    OPINION_FIELDS, PAGE, cited_pages, focus_specs, fsm_hits, log_caught, part_from_job, parts_from_job,
    repair_from_verdict)


class VerdictScreen:
    def _verdict_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = ReflowRow()
        b = QPushButton("SYNTHESIZE ALL EVIDENCE && BUILD VERDICT")
        b.setStyleSheet(f"background-color: {T.AMBER}; color: {T.BG};")
        b.clicked.connect(self.act_verdict)
        row.add(b)
        self.chk_think = QCheckBox("THINK HARDER (slower)")
        if self.mgr.backend == "rkllm" and "think" in CFG.get("stand_in", {}):
            self.chk_think.setText("THINK HARDER (1.7B thinking)")
            self.chk_think.setToolTip("The Qwen3-4B-Thinking .rkllm isn't in models/rkllm/ yet, so the 1.7B thinks instead")
        row.add(self.chk_think)
        self.b_to_sop = QPushButton("REPAIR GUIDE →")
        self.b_to_sop.setToolTip("Write the repair procedure for this verdict")
        self.b_to_sop.clicked.connect(lambda: (self.switch(PAGE["sop"]), self.act_sop()))
        row.add(self.b_to_sop)
        b_parts = QPushButton("PARTS →")
        b_parts.setToolTip("Part numbers, prices and QR codes for the repair in this verdict")
        b_parts.clicked.connect(lambda: self.switch(PAGE["parts"]))
        row.add(b_parts)
        lay.addWidget(row)
        notes_lbl = QLabel("YOUR NOTES FROM CHAT (sent to Final Diagnosis; delete any line that's wrong):")
        notes_lbl.setWordWrap(True)
        lay.addWidget(notes_lbl)
        self.txt_notes = QPlainTextEdit()
        self.txt_notes.setMaximumHeight(110)
        self.txt_notes.setPlaceholderText("What you type in chat shows up here: parts you replaced, things you checked, "
                                          "your theories. You can also type here.")
        lay.addWidget(self.txt_notes)
        self.tabs_verdict = QTabWidget()
        self.txt_verdict = QTextEdit()
        self.txt_verdict.setReadOnly(True)
        self.tabs_verdict.addTab(self.txt_verdict, "WORKING")
        lay.addWidget(self.tabs_verdict, 1)
        self.stack.addWidget(w)

    VERDICT_FIELDS = ("WHY HERE", "FOR", "AGAINST", "TEST", "REPAIR", "PARTS")

    def _data_flags(self):
        """The DATA CHECK flags and notes (app/sanity_check.py) for this case; [] on any failure."""
        try:
            case = {k: self.session.get(k) for k in ("dtcs", "telemetry", "symptoms", "history", "ruled_out",
                                                     "suspect", "intake_note")}
            case["dtcs_raw"] = self.bub_dtcs.text()
            ref = sanity_check.find_ref_db(CFG.get("kb_db"), CFG.get("make"))
            return sanity_check.check(case, self.retriever, ref)
        except Exception as e:
            print(f"data check failed: {type(e).__name__}: {e}", file=sys.stderr)
            return []

    def _data_check(self):
        """The DATA CHECK block for the top of the prompts; '' when the case data shows no problem. Never stops a
        request: a failure here just means no block."""
        return sanity_check.block(self._data_flags())

    def _steps_mode(self):
        """Final Diagnosis in short steps? Config "verdict_steps": true/false, or "auto": on for local models
        (Ollama on the PC, RKLLM on the deck), off for API backends and team mode, which follow the long prompt."""
        v = CFG.get("verdict_steps", "auto")
        if v != "auto":
            return bool(v)
        return type(self.mgr).__name__ == "ModelManager" and getattr(self.mgr, "backend", "") not in ("claude", "openai", "cli")

    def act_verdict(self):
        if self._busy():
            return
        self.session.set("chat_notes", self.txt_notes.toPlainText().strip())
        case = self.session.case_summary()
        on_pc = self.mgr.home and not self.mgr.pc_why
        most = 5 if on_pc else 3
        maint = self.session.get("maint_selected")
        flags = self._data_flags()
        dc = sanity_check.block(flags)
        top = f"{dc}\n\n" if dc else ""
        flagged = dc.startswith("DATA CHECK")
        self._verdict_redo, self._verdict_retried = None, False
        gate = not (self.session.get("chat_notes") or getattr(self, "chat_hist", None))
        opinions = self.session.opinions()
        blind = bool(CFG.get("verdict_blind_first", True) and opinions)
        case1 = self.session.case_summary(exclude=[k for k, _ in OPINION_FIELDS]) if blind else case
        stop = verdict_steps.hard_stop(flags) if gate else ""
        if stop:
            self._reset_verdict_tabs()
            self.txt_verdict.clear()
            self._say(self.txt_verdict, dc, T.AMBER)
            self._say(self.txt_verdict, stop)
            self._verdict_done(stop)
            return
        steps = self._steps_mode() and not self.chk_think.isChecked()

        def go(ctx):
            prompt = (f"{top}CASE:\n{case1}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
                      "Write a ranked work plan for this car. Cover every code in the case.\n"
                      f"- Up to {most} ISSUES, only as many as the evidence supports; one is right when one cause "
                      "explains everything. Separate faults (or a second part that also has to be bought) are separate "
                      "issues.\n"
                      "- Order: rank by the evidence. When two causes fit the evidence equally well, the one with the "
                      "cheaper or quicker test goes first. Say why each one sits where it does.\n"
                      "- Every idea the mechanic states in their notes gets its own issue with the evidence for and "
                      "against it, even if you rank it low.\n"
                      "- A part they say they replaced or checked is never issue 1. List it last, as a recheck, only "
                      "if the evidence still points at it, and say why.\n"
                      "- [REPORTED CAUSES] and [COMMUNITY REPORTS] are supporting evidence, ranked below the manual: an "
                      "owner-reported cause is never ISSUE 1 unless the manual's procedure or the case's own readings "
                      "point there too, and then cite that step or reading.\n"
                      + ("- Don't build an issue on a reading, code or part the DATA CHECK above flags.\n"
                         if flagged else "")
                      + ("- If the evidence is too thin to rank causes (or the readings it would rest on are flagged "
                         "above), don't guess: write only\nNEEDS MORE DATA: <one line: what is missing>\nTEST: <a test "
                         "or reading to get, and what each result would mean> (one line each, up to 5)\nand stop.\n"
                         if gate else "") +
                      "- Use only the case, live data and manual text above, and quote the real numbers. Never write "
                      "part numbers or prices: the deck adds them from its database.\n\n"
                      "Write exactly this format:\n"
                      "SUMMARY: <2-3 sentences: how many problems, and the order to work in>\n"
                      "=== ISSUE 1: <short name> ===\n"
                      "WHY HERE: <why it's ranked here>\n"
                      "FOR: <evidence that fits>\n"
                      "AGAINST: <evidence that doesn't fit, or none>\n"
                      "TEST: <the test that confirms or clears it, and what each result means>\n"
                      "REPAIR: <the job if confirmed, in a few words, e.g. replace front brake pads>\n"
                      "PARTS: <part names only, comma separated, e.g. exhaust manifold gasket, spark plugs; "
                      "no actions or steps; or none>\n"
                      "=== ISSUE 2: <short name> ===\n(same fields)\n"
                      + ("=== ALSO DO: maintenance ===\nREPAIR: <the approved maintenance jobs>\nPARTS: <part names>\n"
                         if maint else "") +
                      "Keep each issue under 120 words.")
            fmt = prompt[prompt.index("Write exactly this format:"):]
            full = f"{top}CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"

            def pass2_prompt(plan):
                short = f"{top}CASE:\n{case}\n\nFSM DATA (the pages your first pass used):\n{cited_pages(ctx, plan)}\n\n"
                return (short + "YOUR FIRST-PASS PLAN (written from the evidence only, before the opinions below were "
                        "shown):\n" + plan.strip() + "\n\nOPINIONS LEFT OUT OF THE FIRST PASS (guesses and earlier "
                        "diagnoses, not test results):\n" + opinions + "\n\n"
                        "For each opinion, say whether the evidence in the CASE and FSM DATA supports it, and the test "
                        "that would settle it. Reorder only for evidence (a code, a reading, a test result or a manual "
                        "page), or to put the manual's earlier or cheaper step first; never because someone suspects "
                        "it, and name what moved it. An opinion may move up only if a reading, code or test result in "
                        "the CASE supports it: the opinion itself, or how common that fault is in general, is not "
                        "evidence. An opinion the plan doesn't cover gets its own issue, ranked by its evidence. Then "
                        "write the final plan.\n"
                        "Start with one line per opinion:\nOPINION CHECK: <opinion> - <supported / not supported / "
                        "untested>: <the evidence, and the test>\nthen " + fmt[0].lower() + fmt[1:])

            def pass1_done(plan):
                if "[LLM error" in plan or not plan.strip():
                    return
                self._blind_pass1 = plan.strip()
                self.txt_verdict.clear()
                self._say(self.txt_verdict, "[*] Pass 1 (evidence only) done. Pass 2: weighing the left-out opinions "
                          "against the evidence...", T.MUTED)
                w = self.worker
                if re.search(r"(?m)^\W*NEEDS MORE DATA", plan) and not re.search(r"(?im)^\W*=+\s*ISSUE", plan):
                    self._verdict_done(plan)
                    return
                self._verdict_redo = pass2_prompt(plan)
                go2 = lambda: self._run("deep", pass2_prompt(plan), self.txt_verdict, self._verdict_done, temperature=0.0)
                self._after_thread(w, go2)
            self._reset_verdict_tabs()
            self.txt_verdict.clear()
            if dc:
                self._say(self.txt_verdict, dc, T.AMBER)
            self._say(self.txt_verdict, "[*] Synthesizing all evidence..." + (
                " Pass 1 of 2: evidence only, the Suspect field and other opinions left out." if blind else ""), T.MUTED)
            self._blind_pass1 = ""
            if steps:
                self._verdict_steps(top, case, case1, ctx, opinions if blind else "", fmt, most, on_pc)
                return
            if self.chk_think.isChecked():
                def draft_done(draft):
                    if "[LLM error" in draft or not draft.strip():
                        return
                    self.txt_verdict.clear()
                    self._say(self.txt_verdict, "[*] Draft written. Thinking model is now checking it against the "
                              "evidence (its reasoning streams below; only its final plan is kept)...", T.MUTED)
                    review = ((full + prompt[prompt.index("Write a ranked work plan"):] if blind else prompt)
                              + "\n\nDRAFT PLAN (written from the same data by another technician"
                              + (", before it saw the Suspect field and other opinions" if blind else "")
                              + "):\n" + draft.strip()
                              + "\n\nCheck the draft against the CASE and FSM DATA above. Keep every part of it that the "
                              "evidence supports. Change the ranking, a test or an issue only where the case or the "
                              "manual shows it is wrong or missing something, and say what evidence made you change it. "
                              "Decide once per point and move on; don't reopen a point you have settled. Then write "
                              "the final plan in exactly the format above.")
                    self._draft_verdict = draft.strip()
                    w = self.worker
                    go = lambda: self._run("think", review, self.txt_verdict, self._think_done)
                    self._after_thread(w, go)
                self._draft_verdict = ""
                self._run("deep", prompt, self.txt_verdict, draft_done, temperature=0.0)
            else:
                if not blind:
                    self._verdict_redo = prompt
                self._run("deep", prompt, self.txt_verdict, pass1_done if blind else self._verdict_done, temperature=0.0)

        self._lookup(case1, 6000 if on_pc else 3500, go)

    def _chain(self, role, prompt, then, **kw):
        """_run after the current worker's thread has finished (a done-callback runs before the thread ends)."""
        w = self.worker
        go = lambda: self._run(role, prompt, self.txt_verdict, then, temperature=0.0, **kw)
        self._after_thread(w, go)

    def _after_thread(self, w, go):
        """go() once worker w's thread has ended, checked by a short timer. Was finished.connect: the thread could
        emit finished while the done-callback still ran, yet still report isRunning(), so go never ran (pass 2 of
        FINAL DIAGNOSIS never started after "Pass 1 done"). go runs exactly once."""
        fired = [False]

        def fire(*_):
            if not fired[0]:
                fired[0] = True
                go()
        if w is None or not w.isRunning():
            fire()
            return

        def poll():
            if fired[0]:
                return
            if w.isRunning():
                QTimer.singleShot(200, poll)
            else:
                fire()
        QTimer.singleShot(200, poll)

    def _verdict_steps(self, top, case, case1, ctx, opinions, fmt, most, on_pc):
        """Final Diagnosis in short steps (app/verdict_steps.py): candidates, one check per candidate, Python
        ranking, then the write-up. Candidates come from the evidence only (case1); opinions join as guesses."""
        V = verdict_steps
        self._steps_log = []

        def got_candidates(text):
            if "[LLM error" in text:
                return
            cands = V.parse_candidates(text) + V.opinion_candidates(opinions)
            self._steps_log.append(("candidates", text))
            if not cands:
                self._say(self.txt_verdict, "[!] No candidate list came back; writing the plan in one pass.", T.AMBER)
                self._chain("deep", V.write_prompt(top, case, ctx, [], fmt, most, opinions), self._verdict_done)
                return
            self.txt_verdict.clear()
            self._say(self.txt_verdict, f"[*] Step 2 of 3: checking {len(cands)} candidates against the case and the "
                      "manual...", T.MUTED)
            check_next(cands, 0)

        def check_next(cands, i):
            if i >= len(cands):
                ranked = V.rank(V.case_against(cands, {k: self.session.get(k) for k in ("ruled_out", "history")}))
                self._steps_log.append(("ranking", V.ranking_text(ranked)))
                self.txt_verdict.clear()
                self._say(self.txt_verdict, "[*] Step 3 of 3: ranked by the deck from the checks:\n"
                          + V.ranking_text(ranked), T.MUTED)
                wp = V.write_prompt(top, case, ctx, ranked, fmt, most, opinions)
                self._verdict_redo = wp
                self._chain("deep", wp, self._verdict_done)
                return

            def got(text, c=cands[i]):
                if "[LLM error" in text:
                    return
                c.update(V.parse_check(text))
                self._steps_log.append((f"check {c['name']}", text))
                check_next(cands, i + 1)
            self._chain("deep", V.check_prompt(top, case1, ctx, cands[i]), got)

        self._say(self.txt_verdict, "[*] Step 1 of 3: listing the causes worth checking (evidence only)...", T.MUTED)
        self._run("deep", V.candidates_prompt(top, case1, ctx), self.txt_verdict, got_candidates, temperature=0.0)


    def _reset_verdict_tabs(self):
        """Back to the one streaming tab (the issue tabs and summary of the last verdict go)."""
        while self.tabs_verdict.count() > 1:
            self.tabs_verdict.removeTab(1 if self.tabs_verdict.widget(0) is self.txt_verdict else 0)
        self.tabs_verdict.setTabText(0, "WORKING")
        self.tabs_verdict.setCurrentIndex(0)

    @classmethod
    def parse_verdict(cls, text):
        """(summary, [issue dicts]) from the plan format; [] when the model ignored the format."""
        text = re.sub(r"(?s)<think>.*?</think>", "", text)
        blocks = re.split(r"(?m)^[\s#*_]*=+\s*\**\s*((?:ISSUE\s*\d+|ALSO DO)\b[^\n=]*?)\s*\**\s*=+[\s*_]*$", text)
        m = re.search(r"(?is)SUMMARY\s*\**\s*:\s*(.*)", blocks[0])
        summary = (m.group(1) if m else blocks[0]).strip()
        issues = []
        for head, body in zip(blocks[1::2], blocks[2::2]):
            kind, _, name = head.partition(":")
            d = {"head": head.strip(), "name": name.strip(" *") or kind.strip(), "also": kind.upper().startswith("ALSO"),
                 "body": body.strip()}
            rx = r"(?im)^[\s*_-]*(" + "|".join(cls.VERDICT_FIELDS) + r")\s*\**\s*:\s*"
            parts = re.split(rx, body)
            for key, val in zip(parts[1::2], parts[2::2]):
                d[key.upper()] = val.strip()
            issues.append(d)
        return summary, issues

    def _part_rows(self, name):
        """[(part number, description, price)] from the car's database for one part name, [] when none."""
        if PS is None or not name:
            return []
        try:
            cfg = self._ps_cfg()
            if not cfg["kb_ok"]:
                return []
            return PS.parts_for_job(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])[:3]
        except Exception as e:
            log_caught(f"part rows for {name!r}", e)
            return []

    def _issue_parts(self, d):
        """Part names for one issue: the PARTS field (any action words the model added are dropped), else the
        part its REPAIR line names."""
        names = [n for p in re.split(r"[,;\n]", d.get("PARTS", "")) if p.strip(" .")
                 and p.strip(" .").lower() not in ("none", "n/a", "-", "none needed") for n in parts_from_job(p.strip(" ."))]
        if not names and d.get("REPAIR"):
            names = parts_from_job(d["REPAIR"])
        return list(dict.fromkeys(n for n in names if n))

    def _build_verdict_tabs(self, text):
        summary, issues = self.parse_verdict(text)
        self.tabs_verdict.setTabText(0, "FULL TEXT")
        if not issues:
            return
        all_parts = []
        for n, d in enumerate(issues, 1):
            t = QTextEdit()
            t.setReadOnly(True)
            box = QWidget()
            bl = QVBoxLayout(box)
            row = QHBoxLayout()
            job = d.get("REPAIR") or d["name"]
            b1 = QPushButton("REPAIR GUIDE FOR THIS")
            b1.clicked.connect(lambda _=False, j=job: (self.bub_repair.set_text(j), self.switch(PAGE["sop"]), self.act_sop()))
            row.addWidget(b1)
            if PS is not None:
                b2 = QPushButton("PARTS + PRICES FOR THIS")
                pq = (self._issue_parts(d) or [part_from_job(job)])[0]
                b2.clicked.connect(lambda _=False, j=pq: (self.in_part.setText(j), self.act_find_parts(),
                                                          self.switch(PAGE["parts"])))
                row.addWidget(b2)
            row.addStretch(1)
            bl.addLayout(row)
            bl.addWidget(t, 1)
            title = ("ALSO: " if d["also"] else f"{n}. ") + d["name"][:28]
            self.tabs_verdict.insertTab(self.tabs_verdict.count() - 1, box, title)
            for key in self.VERDICT_FIELDS[:-1]:
                if d.get(key):
                    self._say(t, f"<b>{key}:</b> " + html.escape(d[key]).replace("\n", "<br>"), raw=True)
            if not any(d.get(k) for k in self.VERDICT_FIELDS):
                self._say(t, d["body"])
            self._say(t, "=== PARTS (numbers from the database, not the model) ===", T.TEXT)
            for name in self._issue_parts(d) or ([part_from_job(job)] if d["also"] else []):
                rows = self._part_rows(name)
                all_parts.append((n, name, rows))
                self._say(t, f"{name}: " + ("; ".join(f"{pn} {op} {price or ''}".strip() for pn, op, price in rows)
                                            if rows else "no part number in the database (look it up on PARTS)"),
                          T.OK if rows else T.AMBER)
            specs = focus_specs(job, self.retriever.specs(job, limit=30))[:6]
            if specs:
                self._say(t, "=== SPECS (database) ===", T.TEXT)
                self._say(t, "\n".join(" • " + x for x in specs), T.OK)
            hits = fsm_hits(self.retriever, job + " removal installation" if not d["also"] else job, k=2, chars=700)
            if hits:
                self._say(t, "=== MANUAL ===", T.TEXT)
                self._say_hits(t, hits)
            t.moveCursor(QTextCursor.MoveOperation.Start)
        t = QTextEdit()
        t.setReadOnly(True)
        self.tabs_verdict.insertTab(0, t, "SUMMARY")
        self._say(t, summary or "(no summary written)")
        self._say(t, "=== WORK ORDER ===", T.TEXT)
        self._say(t, "\n".join((" • also: " if d["also"] else f" {n}. ") + d["name"] +
                                (f": {d['REPAIR']}" if d.get("REPAIR") else "") for n, d in enumerate(issues, 1)))
        self._say(t, "=== ALL PARTS (numbers from the database) ===", T.TEXT)
        self._say(t, "\n".join(f" [{'also' if issues[n - 1]['also'] else n}] {name}: " +
                                ("; ".join(f"{pn} {price or ''}".strip() for pn, _, price in rows) if rows
                                 else "no number in the database") for n, name, rows in all_parts) or "(none named)")
        self._say(t, "Only buy a later issue's parts if the earlier test doesn't settle it.", T.MUTED)
        self.tabs_verdict.setCurrentIndex(0)

    def _think_done(self, text):
        """The reasoning streamed while it worked; once the answer is in, show only the answer."""
        if "[LLM error" in text:
            if getattr(self, "_draft_verdict", ""):
                self._say(self.txt_verdict, "[!] The review failed, so the plain model's plan is kept, unchecked.", T.AMBER)
                self._verdict_done(self._draft_verdict)
            return
        answer = re.sub(r"(?s)^.*</think>", "", text).strip()
        if "</think>" not in text or not answer:
            draft = getattr(self, "_draft_verdict", "")
            if draft:
                self.txt_verdict.clear()
                self._say(self.txt_verdict, "[!] The thinking model didn't finish its review, so this is the plain "
                          "model's plan, unchecked:", T.AMBER)
                self._say(self.txt_verdict, draft)
                self._verdict_done(draft)
            else:
                self._say(self.txt_verdict, "[!] The model ran out of room before it finished thinking, so no "
                          "verdict was saved. Run it again without THINK HARDER.", T.AMBER)
            return
        self.txt_verdict.clear()
        self._say(self.txt_verdict, f"[✓] Verdict ({self.mgr.label('think')}; its reasoning is hidden):", T.OK)
        self._say(self.txt_verdict, answer)
        self._verdict_done(answer)

    def _verdict_done(self, text):
        """Save the verdict and load its repair into the Parts tab, so part numbers are waiting there."""
        if "[LLM error" in text:
            return
        redo = getattr(self, "_verdict_redo", None)
        if redo and not getattr(self, "_verdict_retried", False):
            try:
                notes = answer_check.check(self.parse_verdict(text)[1],
                                           {k: self.session.get(k) for k in ("ruled_out", "history")}, None)
            except Exception:
                notes = []
            if notes:
                self._verdict_retried = True
                self._say(self.txt_verdict, "[answer check] sending the plan back once: " + "; ".join(notes),
                          T.PYCHECK)
                self._chain("deep", redo + "\n\nYOUR PLAN:\n" + text.strip() + "\n\nTHE DECK'S CHECK OF THAT PLAN:\n"
                            + "\n".join(notes) + "\nFix the plan where the check is right: a part verified good or "
                            "just replaced is not issue 1 unless the evidence shows that new part itself is at fault, and "
                            "then say so in WHY HERE. Otherwise keep the plan and say why. Then write the whole plan "
                            "again in exactly the same format.", self._verdict_done)
                return
        self.session.set("final_verdict", text)
        self._file_visit()
        if re.search(r"(?m)^\W*NEEDS MORE DATA", text) and not re.search(r"(?im)^\W*=+\s*ISSUE", text):
            tests = [ln.strip() for ln in text.splitlines() if re.match(r"\W*(NEEDS MORE DATA|TEST)\b", ln)]
            self._say(self.txt_verdict, "[!] Not enough data for a diagnosis yet. Run these, then add the results "
                      "in BRAINSTORM / CHAT or Intake and run FINAL DIAGNOSIS again:", T.PYCHECK)
            self._say(self.txt_chat, "Final Diagnosis stopped for more data. Tests it asked for:\n" + "\n".join(tests),
                      T.PYCHECK, who="Deck")
            return
        self._build_verdict_tabs(text)
        try:
            case = {k: self.session.get(k) for k in ("ruled_out", "history")}
            notes = answer_check.check(self.parse_verdict(text)[1], case, self.retriever)
        except Exception as e:
            notes = [f"(answer check failed: {type(e).__name__})"]
        if notes:
            self._say(self.txt_verdict, "[answer check] " + "\n  • ".join([""] + notes).strip(), T.PYCHECK)
        _, issues = self.parse_verdict(text)
        first = next((d for d in issues if not d["also"]), None)
        job = (first.get("REPAIR") if first else None) or repair_from_verdict(text)
        if not job:
            self._say(self.txt_verdict, "(no repair named in the verdict, so the Parts tab wasn't filled)", T.MUTED)
            return
        self.bub_repair.set_text(job)
        if PS is not None:
            part = (self._issue_parts(first) if first else [])[:1] or [part_from_job(job)]
            self.in_part.setText(part[0])
            self.act_find_parts()
            self._say(self.txt_verdict, f"[✓] Parts tab ready for: {part[0]}. Tap PARTS →", T.OK)
