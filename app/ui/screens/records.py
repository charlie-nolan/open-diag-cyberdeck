"""Case and car records: visits, saved case, OBD reads, test drives."""
import car_records
import drive_file
import json
import live_data
import os
import re
import sanity_check
import theme as T
import time
from PyQt6.QtWidgets import QFileDialog, QInputDialog
from ui.config import CFG
from ui.context import make_retriever, remember_ecu_vin
from ui.workers import CaseContextWorker, DriveWorker, OBDWorker


class RecordsScreen:
    def _store_case(self):
        """Write Intake to the session. If the case changed, the chat starts over with the new case."""
        v = {"year": self.cmb_year.currentText(), "make": self.cmb_make.currentText(),
             "model": self.cmb_model.currentText(), "vin": self.in_vin.text().strip().upper(),
             "trans": self.cmb_trans.currentText(), "miles": self.in_miles.text().strip(),
             "engine": self.lbl_engine.text().replace("Engine: ", ""), "customer": self.in_customer.text().strip()}
        self.session.set("vehicle_info", json.dumps(v))
        key = self._file_key(v)
        if key.startswith("NOVIN|") and key != getattr(self, "shown_key", ""):
            self.shown_key = key
            self._show_past_visits(key)
        if v["vin"] and getattr(self, "last_ecu", "") and v["vin"] != getattr(self, "vin_at_read", ""):
            remember_ecu_vin(self.last_ecu, v["vin"])
        desc = " ".join(x for x in (v["year"], v["make"], v["model"]) if x)
        extra = ", ".join(x for x in (v["engine"], v["trans"], f"{v['miles']} mi" if v["miles"] else "") if x)
        self.session.set("vehicle", desc + (f" ({extra})" if extra else ""))
        self.lbl_title.setText(self._title_text())
        codes = [f"{c} ({st})" if st else c for c, st in sanity_check.parse_codes(self.bub_dtcs.text())
                 if re.fullmatch(r"[PBCU]\d{4}", c)]
        self.session.set("dtcs", ", ".join(dict.fromkeys(codes)))
        self.session.set("telemetry", self.in_live.text().strip())
        read = CFG.get("_live") or ("", "")
        self.session.set("telemetry_source", read[0] if self.in_live.text().strip() == read[1].strip() else "")
        self.session.set("symptoms", self.bub_symptoms.text())
        self.session.set("ruled_out", self.bub_good.text())
        self.session.set("suspect", self.in_suspect.text().strip())
        self.session.set("history", self.in_history.text().strip())
        note = self.txt_note.toPlainText().strip()
        self.session.set("intake_note", note)
        if not note:
            self.session.set("intake_summary", "")
        model = v["model"] or None
        year = int(v["year"]) if v["year"].isdigit() else None
        if (model, year) != (CFG.get("kb_model"), CFG.get("kb_year")):
            CFG["kb_model"], CFG["kb_year"] = model, year
            if hasattr(self, "cmb_p_year"):
                self._sync_parts_car()
            try:
                self.retriever = make_retriever(CFG)
                self._warm_retriever()
            except Exception as e:
                self._say(self.txt_intake, f"[!] manual lookup not reloaded: {e}", T.AMBER)
        if not key:
            self.session.set("earlier_visits", "")
        self._past_readings(key, v)
        case = self.session.case_summary()
        if case != self.last_case:
            self.last_case = case
            self.session.set("final_verdict", "")
            self.chat_started = False
        self._file_visit()
        return case

    def _file_visit(self):
        """File the open case under its car's VIN (one row per case, updated as it changes)."""
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        vin = self._file_key(v)
        case = {k: self.session.get(k) for k in self.CASE_KEYS}
        if not vin or not any(case.values()):
            return
        vid = self.session.save_visit(self.session.get("visit_id"), vin, self.session.get("vehicle"), case)
        self.session.set("visit_id", str(vid))
        if CFG.get("car_records", True):
            try:
                rec = car_records.CarRecords(vin)
                rec.update_car(v)
                rec.save_visit(vid, self.session.get("vehicle"), case, v.get("miles", ""))
            except Exception as e:
                self._records_warn(e)
            self._flush_records()

    def _rec_car(self, v):
        return {"make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "engine": v.get("engine"),
                "transmission": v.get("trans"), "vin": v.get("vin"), "db": os.path.expanduser(CFG.get("kb_db") or "")}

    def _records_warn(self, e):
        if not getattr(self, "_rec_warned", False):
            self._rec_warned = True
            self._say(self.txt_intake, f"[!] Car record file not written (kept to try again): {e}", T.AMBER)

    def _record(self, kind, **kw):
        if not CFG.get("car_records", True):
            return
        item = {"kind": kind, "t": time.time(), "visit_ref": self.session.get("visit_id"), **kw}
        try:
            pend = json.loads(self.session.get("records_pending") or "[]")
        except ValueError:
            pend = []
        pend.append(item)
        self.session.set("records_pending", json.dumps(pend[-3000:]))
        self._flush_records()

    def _flush_records(self):
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
            pend = json.loads(self.session.get("records_pending") or "[]")
        except ValueError:
            return
        key = self._file_key(v)
        if not key or not pend:
            return
        try:
            rec, car, vid = car_records.CarRecords(key), self._rec_car(v), self.session.get("visit_id")
            for x in pend:
                if x["kind"] == "chat":
                    rec.add_chat(x["t"], x.get("sender", ""), x.get("message", ""), x.get("visit_ref") or vid)
                else:
                    rec.add_reading(x["t"], x["kind"], x.get("codes", ""), x.get("live", ""), x.get("source", ""),
                                    x.get("visit_ref") or vid, x.get("drive_id", ""), v.get("miles", ""), car)
            self.session.set("records_pending", "")
        except Exception as e:
            self._records_warn(e)

    def _past_readings(self, key, v):
        """The values read now next to the same values at the car's earlier visits (Python, from the record file)."""
        live = self.session.get("telemetry")
        text = ""
        if key and live and CFG.get("car_records", True):
            try:
                text = car_records.CarRecords(key).compare_text(live, self.session.get("telemetry_source"),
                                                                self._rec_car(v), self.session.get("visit_id"))
            except Exception as e:
                self._records_warn(e)
        self.session.set("past_readings", text)

    @staticmethod
    def _live_source(live):
        read = CFG.get("_live") or ("", "")
        return read[0] if live and live.strip() == (read[1] or "").strip() else ""

    @staticmethod
    def _file_key(v):
        """Where this car's visits are filed: its VIN, else customer/plate + year make model (a car the deck
        couldn't read a VIN from still gets a history). '' when there's nothing to file it under."""
        vin = (v.get("vin") or "").upper()
        if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            return vin
        cust = re.sub(r"\s+", " ", (v.get("customer") or "").strip().lower())
        car = " ".join(str(v.get(k) or "") for k in ("year", "make", "model")).strip().lower()
        return f"NOVIN|{cust}|{car}" if cust and v.get("model") else ""

    @staticmethod
    def _visit_line(started, case):
        verdict = re.sub(r"\s+", " ", re.sub(r"<think>.*?</think>", "", case.get("final_verdict") or "", flags=re.S))
        bits = [started[:10], case.get("dtcs") or "no codes", case.get("symptoms") or ""]
        if verdict.strip():
            bits.append("verdict: " + verdict.strip()[:90])
        return " | ".join(b for b in bits if b)

    def _show_past_visits(self, vin):
        rows = self.session.visits(vin, exclude=self.session.get("visit_id") or None) if vin else []
        self.past_visits = rows
        self.b_visits.setEnabled(bool(rows))
        self.b_visits.setText(f"PAST VISITS ({len(rows)})" if rows else "PAST VISITS")
        lines = [self._visit_line(started, case) for _, started, _, case in rows]
        self.session.set("earlier_visits", "; ".join(lines[:3])[:600])
        if rows:
            self._say(self.txt_intake, f"[history] {len(rows)} earlier visit(s) for this car (PAST VISITS opens one):\n"
                      + "\n".join("    " + l for l in lines[:5]), T.PURPLE_TEXT)

    def act_past_visits(self):
        rows = getattr(self, "past_visits", [])
        if not rows:
            return
        labels = [self._visit_line(started, case)[:110] for _, started, _, case in rows]
        pick, ok = QInputDialog.getItem(self, "Past visits", "Open which visit? (the case on screen is kept "
                                        "for LOAD LAST CASE)", labels, 0, False)
        if not ok:
            return
        vid, started, _, case = rows[labels.index(pick)]
        self._clear_case()
        for k in self.CASE_KEYS:
            self.session.set(k, case.get(k, ""))
        self.session.set("visit_id", str(vid))
        self._load_case()
        self.chat_started = False
        self.txt_intake.clear()
        self._say(self.txt_intake, f"[*] Visit from {started} loaded. Changes are saved to that visit.", T.MUTED)
        self.last_case = self._store_case()
        self._show_past_visits(self._file_key(json.loads(self.session.get("vehicle_info") or "{}")))

    def act_read_obd(self):
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        self._say(self.txt_intake, f"[*] Reading the car ({self.cmb_adapter.currentText()})...", T.MUTED)
        self.b_obd.setEnabled(False)
        self.b_obd.setText("READING...")
        self.obd_worker = OBDWorker(self.cmb_adapter.currentData(), CFG["obd_port"], self.cmb_make.currentText())
        self.obd_worker.result.connect(self._obd_done)
        self.obd_worker.ident.connect(self._apply_ident)
        self.obd_worker.note.connect(lambda n: self._say(self.txt_intake, f"[car] {n}", T.AMBER))
        self.obd_worker.mode06.connect(lambda t: self.session.set("mode06", t))
        self.obd_worker.finished.connect(lambda: (self.b_obd.setEnabled(True),
                                                  self.b_obd.setText("READ CAR (codes + live data)")))
        self.obd_worker.start()

    def _obd_done(self, codes, live):
        """A scan replaces the codes with what the car has now (10-24: appending repeated them every
        read). Codes typed by hand are replaced too: the car's answer is the current state."""
        old = self.bub_dtcs.text().strip()
        self.bub_dtcs.set_text(codes)
        if old and old != codes:
            self._say(self.txt_intake, f"[*] Codes before this scan: {old}", T.MUTED)
        if live:
            self.in_live.setText(live)
        self._say(self.txt_intake, f"[✓] Codes: {codes or 'none stored'} | {live}", T.AQUA)
        self._record("read", codes=codes, live=live, source=self._live_source(live))

    def act_test_drive(self):
        if self.drive_worker is not None:
            self.drive_worker.stop_flag.set()
            self.b_drive.setEnabled(False)
            self.b_drive.setText("STOPPING...")
            return
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            self._say(self.txt_intake, "[!] Wait for READ CAR (or the other program) to finish first.", T.AMBER)
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        every = max(10, int(CFG.get("drive_every_s", 30)))
        self.drive_id = str(time.time())
        self.drive_n = 0
        self.drive_notes = set()
        self.session.set("drive_id", self.drive_id)
        self.session.set("drive_log", "")
        self.session.set("drive_log_short", "")
        self._show_drive_line()
        self.drive_path = self._drive_file_start(self.drive_id)
        self._say(self.txt_intake, f"[*] Test drive started: reading the car every {every} s. Drive through idle, "
                  "cruise and some harder acceleration, then tap STOP TEST DRIVE. Eyes on the road.", T.FOREST_TEXT)
        w = DriveWorker(self.cmb_adapter.currentData(), CFG["obd_port"], every, self.cmb_make.currentText())
        self.drive_worker = self.obd_worker = w
        w.sample.connect(self._drive_sample)
        first = {"done": False}

        def ident(*a):
            if not first["done"]:
                first["done"] = True
                self._apply_ident(*a)
        w.ident.connect(ident)
        w.note.connect(self._drive_note)
        w.finished.connect(self._drive_finished)
        self.b_drive.setText("STOP TEST DRIVE (0 readings)")
        self.b_drive.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT};")
        self.b_obd.setEnabled(False)
        w.start()

    def _drive_note(self, n):
        if n not in self.drive_notes:
            self.drive_notes.add(n)
            self._say(self.txt_intake, f"[car] {n}", T.AMBER)

    def _drive_file_start(self, drive_id):
        car = " ".join(x for x in (self.cmb_year.currentText(), self.cmb_make.currentText(),
                                   self.cmb_model.currentText()) if x)
        try:
            return drive_file.start(drive_id, car)
        except (OSError, ValueError) as e:
            self._say(self.txt_intake, f"[!] Couldn't start a drive file: {e}", T.AMBER)
            return ""

    def act_load_drive(self):
        if self.drive_worker is not None:
            self._say(self.txt_intake, "[!] Stop the test drive first.", T.AMBER)
            return
        self._recover_drives()
        path = QFileDialog.getOpenFileName(self, "Load test drive", drive_file.DIR, "Test drives (*.jsonl)")[0]
        if not path:
            return
        try:
            head, rows = drive_file.read(path)
        except OSError as e:
            self._say(self.txt_intake, f"[!] Couldn't read that file: {e}", T.AMBER)
            return
        if not rows:
            self._say(self.txt_intake, "[!] That drive has no readings in it.", T.AMBER)
            return
        self.drive_id = str(head.get("drive_id") or rows[0][0])
        if not self.session.drive_samples(self.drive_id):
            for row in rows:
                self.session.add_drive_sample(self.drive_id, *row)
        self.session.set("drive_id", self.drive_id)
        self.drive_n = len(rows)
        self._build_drive_log()
        self._drive_codes(rows[-1][1], rows[-1][2])
        self._store_case()
        self._say(self.txt_intake, f"[✓] Loaded {len(rows)} readings from {os.path.basename(path)}"
                  + (f" ({head['car']})" if head.get("car") else "") + ". The models now see this drive.", T.OK)

    def _recover_drives(self):
        """Drives from before files existed, or a crashed one: their readings are in the session DB, so give each a file."""
        for drive_id in self.session.drive_ids():
            try:
                if drive_file.has(drive_id):
                    continue
                path = drive_file.start(drive_id)
                for row in self.session.drive_samples(drive_id):
                    drive_file.add(path, *row)
            except (OSError, ValueError) as e:
                self._say(self.txt_intake, f"[!] Couldn't save drive {drive_id} to a file: {e}", T.AMBER)

    def _drive_sample(self, t, codes, live):
        self.session.add_drive_sample(self.drive_id, t, codes, live)
        self._record("drive", t=t, codes=codes, live=live, drive_id=self.drive_id, source=self._live_source(live))
        try:
            if self.drive_path:
                drive_file.add(self.drive_path, t, codes, live)
        except OSError as e:
            self._drive_note(f"Drive file not written: {e}")
        self.drive_n += 1
        self._obd_done(codes, live) if self.drive_n == 1 else self._drive_codes(codes, live)
        self.b_drive.setText(f"STOP TEST DRIVE ({self.drive_n} readings)")
        self._build_drive_log()

    def _drive_codes(self, codes, live):
        """Later readings: same replace rule as READ CAR, but only a changed code list is worth a line."""
        old = self.bub_dtcs.text().strip()
        if codes != old:
            self.bub_dtcs.set_text(codes)
            self._say(self.txt_intake, f"[car] Codes now: {codes or 'none'} (were: {old or 'none'})", T.AMBER)
        if live:
            self.in_live.setText(live)

    def _build_drive_log(self):
        rows = self.session.drive_samples(getattr(self, "drive_id", "") or self.session.get("drive_id"))
        if not rows:
            return
        self.session.set("drive_log", live_data.drive_summary(rows, max_rows=18, max_chars=2800))
        self.session.set("drive_log_short", live_data.drive_summary(rows, max_rows=6, max_chars=1100))
        self._show_drive_line()

    def _show_drive_line(self):
        log = self.session.get("drive_log")
        self.lbl_drive.setText(("Test drive: " + log.split("\n")[0].split(".")[0] + ". "
                                + (log.split("\n")[1] if "\n" in log else "")) if log else "")

    def _drive_finished(self):
        self.drive_worker = None
        self.b_drive.setEnabled(True)
        self.b_drive.setText(f"TEST DRIVE (every {CFG.get('drive_every_s', 30)} s)")
        self.b_drive.setStyleSheet("")
        self.b_obd.setEnabled(True)
        n = getattr(self, "drive_n", 0)
        if n:
            self._build_drive_log()
            self._store_case()
            self._say(self.txt_intake, f"[✓] Test drive saved: {n} readings. Brainstorm, debate, wildcard and the "
                      "verdict all see the whole drive.", T.OK)
        else:
            self._say(self.txt_intake, "[!] Test drive stopped with no readings (no answer from the car).", T.AMBER)

    def act_save_case(self):
        case = self._store_case()
        self._say(self.txt_intake, "[✓] Case saved.", T.OK)
        w = getattr(self, "case_worker", None)
        if w is not None and w.isRunning():
            return
        self._say(self.txt_intake, "[*] Looking up what the manual has for it...", T.MUTED)
        self.case_worker = CaseContextWorker(self.retriever, case, CFG.get("intake_show_chars", 16000))
        self.case_worker.result.connect(lambda ctx: self._say(self.txt_intake, ctx or "(nothing matched)"))
        self.case_worker.start()

    CASE_KEYS = ("dtcs", "telemetry", "telemetry_source", "mode06", "past_readings", "symptoms", "ruled_out",
                 "suspect", "history", "intake_note", "intake_summary", "final_verdict", "saved_debate",
                 "saved_wildcard", "drive_log", "drive_log_short", "chart_reads", "chat_notes", "drive_id",
                 "maint_selected", "maint_status")
    CAR_KEYS = ("vehicle", "vehicle_info", "earlier_visits")

    def _clear_case(self):
        """Empty the case fields (the car stays). The old case is kept for LOAD LAST CASE."""
        old = {k: self.session.get(k) for k in self.CASE_KEYS}
        if any(old.values()):
            self.session.set("prev_case", json.dumps(dict(old, visit_id=self.session.get("visit_id"))))
        for k in self.CASE_KEYS:
            self.session.set(k, "")
        self.session.set("visit_id", "")

    def act_load_last(self):
        try:
            old = json.loads(self.session.get("prev_case") or "{}")
        except ValueError:
            old = {}
        if not old:
            self._say(self.txt_intake, "[!] No earlier case saved.", T.AMBER)
            return
        self._clear_case()
        for k in self.CASE_KEYS:
            self.session.set(k, old.get(k, ""))
        self.session.set("visit_id", old.get("visit_id", ""))
        self._load_case()
        self.chat_started = False
        self.txt_intake.clear()
        self._say(self.txt_intake, "[*] Last case loaded.", T.MUTED)
        self.last_case = self._store_case()

    def act_new_case(self):
        self._clear_case()
        self._load_case()
        self.last_debate = ""
        self.last_wildcard = ""
        self.chat_started = False
        for t in (self.txt_chat, self.txt_verdict, self.txt_sop, self.txt_parts):
            t.clear()
        self._reset_verdict_tabs()
        self.txt_intake.clear()
        self._say(self.txt_intake, "[*] New case.", T.MUTED)
        self.last_case = self._store_case()
