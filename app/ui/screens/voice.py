"""Voice: dictation, spoken commands, notes and the speaker."""
import re
import theme as T
import voice
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLineEdit, QPlainTextEdit, QTextEdit
from ui.config import CFG, save_config_key
from ui.context import PAGE, focus_specs, fsm_hits, log_caught


class VoiceScreen:
    def _start_voice(self):
        listen, dictate, why = voice.voice_status(CFG) if CFG["voice_enabled"] else (False, False, "voice off in config")
        self.lbl_voice.setText(why)
        self.b_mic.setEnabled(dictate)
        if listen or dictate:
            self.voice = voice.VoiceClient(CFG, self._voice_vocabulary() if listen else [])
            self.voice.command.connect(self.on_voice)
            self.voice.note.connect(self.on_note)
            self.voice.status.connect(self.on_voice_status)
            self.voice.start()

    def _stop_voice(self):
        if self.voice:
            self.voice.stop()
            self.voice.wait(1500)
            self.voice = None

    def _fill_mics(self, refresh=False):
        self.cmb_mic.blockSignals(True)
        keep = self.cmb_mic.currentData() if self.cmb_mic.count() else CFG.get("mic_device", "")
        self.cmb_mic.clear()
        self.cmb_mic.addItem("Mic: system default", "")
        for _, name in voice.input_devices(refresh=refresh):
            self.cmb_mic.addItem(f"Mic: {name}", name)
        if keep:
            i = self.cmb_mic.findData(keep)
            if i < 0:
                self.cmb_mic.addItem(f"Mic: {keep}", keep)
                i = self.cmb_mic.count() - 1
            self.cmb_mic.setCurrentIndex(i)
        self.cmb_mic.blockSignals(False)

    def act_mic_changed(self):
        CFG["mic_device"] = self.cmb_mic.currentData() or ""
        save_config_key("mic_device", CFG["mic_device"])
        self._stop_voice()
        self._start_voice()

    def _speak(self, text):
        """Say text with the listener paused, so the mic doesn't hear the deck (it once typed
        "CyberVecordio Check" into Notes)."""
        if self.voice is not None:
            self.voice.send("pause")
            ms = int(1500 + len(text) * 1000 / (CFG.get("tts_rate", 145) * 5.5 / 60))
            QTimer.singleShot(ms, lambda: self.voice is not None and self.voice.send("resume"))
        return voice.speak(CFG, text)

    def act_speaker_test(self):
        if not self._speak("Cyberdeck audio check."):
            self.lbl_voice.setText("no speech engine: sudo apt install espeak-ng")

    def act_mic(self):
        if self.voice is not None and not self.voice.isRunning():
            self._stop_voice()
            self._start_voice()
        if self.voice is None:
            self.lbl_voice.setText("dictation needs whisper.cpp set up (see README)")
            return
        if self.b_mic.text().startswith("●"):
            self.voice.send("dictate")
            return
        self._start_dictation(self.stack.currentIndex())
        self.voice.send("dictate")

    def _track_focus(self, old, new):
        if isinstance(new, (QLineEdit, QPlainTextEdit, QTextEdit)) and not new.isReadOnly():
            self.last_edit = new

    def _edit_target(self):
        """The text box dictation should type into: the last one the cursor was in, if it's on screen now."""
        w = self.last_edit
        try:
            return w if w is not None and w.isVisible() and w.isEnabled() and not w.isReadOnly() else None
        except RuntimeError:
            return None

    def _start_dictation(self, page):
        self.dictate_target = page
        self.dictate_widget = self._edit_target()
        self.b_mic.setText("● DONE")
        self.b_mic.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT};")

    @staticmethod
    def _insert_text(w, text):
        """Type text at the cursor, with a space before it when it would run into a word."""
        if isinstance(w, QLineEdit):
            pos, cur = w.cursorPosition(), w.text()
            w.insert((" " if pos and not cur[pos - 1].isspace() else "") + text)
        else:
            c = w.textCursor()
            before = w.document().characterAt(c.position() - 1) if c.position() else ""
            c.insertText((" " if before and not before.isspace() else "") + text)
            w.setTextCursor(c)
        w.setFocus()

    def on_voice_status(self, s):
        self.lbl_voice.setText(s)
        if s.startswith(("dictation", "note transcribed", "whisper error")):
            self.b_mic.setText("🎤 MIC")
            self.b_mic.setStyleSheet("")

    def _voice_vocabulary(self):
        """Part names from the kb, so Vosk's closed phrase list can still hear 'torque for caliper bracket bolt'."""
        words = set()
        try:
            db = getattr(self.retriever, "db", None) or self.retriever.kb
            sql = "SELECT component FROM spec" if hasattr(self.retriever, "db") else "SELECT component FROM fast_specs"
            for (c,) in db.execute(sql):
                words |= {w for w in re.findall(r"[a-z]{3,}", (c or "").lower())}
        except Exception as e:
            log_caught("voice vocabulary", e)
        return sorted(words)[:3000]

    def on_voice(self, cmd, arg):
        if cmd == "read_car":
            self.switch(PAGE["intake"])
            self.act_read_obd()
        elif cmd == "save_case":
            self.act_save_case()
        elif cmd == "new_case":
            self.act_new_case()
        elif cmd == "verdict":
            self.switch(PAGE["verdict"])
            self.act_verdict()
        elif cmd == "torque":
            rows = focus_specs(arg, self.retriever.specs(arg, limit=30))[:8] or self.retriever.specs(arg)
            self.switch(PAGE["chat"])
            self._say(self.txt_chat, f"/torque {arg}", who="You (voice)")
            self._say(self.txt_chat, "\n".join("  " + r for r in rows) or "  no match", T.OK)
            self._speak(re.sub(r"^\[[^\]]*\]\s*", "", rows[0]).replace("N·m", "newton meters")
                        .replace("ft-lb", "foot pounds") if rows else f"No torque spec found for {arg}")
        elif cmd == "fluid":
            hits = fsm_hits(self.retriever, f"{arg} fluid capacity specification", k=1, chars=700)
            self.switch(PAGE["chat"])
            self._say(self.txt_chat, f"fluid {arg}", who="You (voice)")
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no FSM match")
            self._speak("There's no fluid table yet. The closest manual section is on screen.")
        elif cmd == "next_step":
            self._speak(self._next_step(arg))
        elif cmd == "goto":
            self.switch(PAGE[{"repair": "sop", "procedure": "sop", "maintenance": "maint"}.get(arg, arg)])
        elif cmd == "stop":
            self.act_stop()
        elif cmd == "dictating":
            if arg == "intake":
                self.switch(PAGE["intake"])
                self.last_edit = self.txt_note
            self._start_dictation(self.stack.currentIndex())

    def _next_step(self, said):
        """Stateless: match what was said against the open procedure's numbered steps, return the next one."""
        steps = re.findall(r"^\s*(\d+)[.)]\s+(.+)$", self.txt_sop.toPlainText(), flags=re.M)
        if not steps:
            return "No repair procedure is open."
        if not said:
            return f"Step {steps[0][0]}. {steps[0][1]}"
        said_words = set(re.findall(r"[a-z]{3,}", said.lower()))
        scores = [len(said_words & set(re.findall(r"[a-z]{3,}", t.lower()))) for _, t in steps]
        best = max(range(len(steps)), key=lambda i: scores[i])
        if scores[best] == 0:
            return "I couldn't match that to a step."
        if best + 1 >= len(steps):
            return "That was the last step."
        n, text = steps[best + 1]
        return f"Step {n}. {text}"

    def on_note(self, text):
        """whisper.cpp text goes into the box the cursor was in when dictation started (any page). With no box
        picked: MIC on Chat asks it, on Repair sets the job, on Parts searches, otherwise it's an intake note."""
        target, self.dictate_target = self.dictate_target, None
        w, self.dictate_widget = self.dictate_widget, None
        self.b_mic.setText("🎤 MIC")
        self.b_mic.setStyleSheet("")
        if not text:
            return
        try:
            alive = w is not None and w.isEnabled() and not w.isReadOnly()
        except RuntimeError:
            alive = False
        if alive and w is not self.txt_note:
            self._insert_text(w, text)
            if w is self.in_chat:
                if self._busy():
                    self.lbl_voice.setText("model busy: your words are in the chat box, press Enter when it's free")
                else:
                    self.act_chat()
            elif w is self.in_part:
                self.act_find_parts()
            return
        if alive:
            self.switch(PAGE["intake"])
            self._insert_text(w, text)
            self._summarize_note()
            return
        if target == PAGE["chat"]:
            self.in_chat.setText(text)
            self.act_chat()
            return
        if target == PAGE["sop"]:
            self.bub_repair.set_text(text)
            return
        if target == PAGE["parts"]:
            self.in_part.setText(text)
            self.act_find_parts()
            return
        self.switch(PAGE["intake"])
        self.txt_note.appendPlainText(text)
        self._summarize_note()

    def _summarize_note(self):
        self._say(self.txt_intake, "[🎤] Note added. Summarizing...", T.MUTED)
        if self._busy():
            self._say(self.txt_intake, "    (model busy: summary skipped; the raw note is kept)", T.MUTED)
            return
        prompt = ("Customer/mechanic intake note, transcribed from speech:\n" + self.txt_note.toPlainText() +
                  "\n\nPull out, as three short lines, only what the note says:\nSYMPTOMS: ...\n"
                  "PARTS ALREADY REPLACED OR CHECKED: ...\nWHEN IT HAPPENS: ...\nWrite 'not mentioned' where it says nothing.")
        self._say(self.txt_intake, "", who="Summary")
        self._run("fast", prompt, self.txt_intake, self._note_summary_done, keep_history=False)

    def _note_summary_done(self, text):
        self.session.set("intake_summary", "" if "[LLM error" in text else text.strip())
