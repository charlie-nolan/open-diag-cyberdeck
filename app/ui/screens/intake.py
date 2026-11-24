"""Screen 1, Intake and scan: car identity, VIN, tools, and loading the saved case."""
import json
import os
import re
import shlex
import theme as T
from PyQt6.QtCore import QProcess
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget)
from retrieval import Retriever as LegacyRetriever
from ui_fit import ReflowRow
from vehicle_id import MAKES, car_from_rom, decode_vin, make_from_ssm
from ui.config import CFG, save_config_key
from ui.context import MODELS, NullRetriever, TRANS, case_make, ecu_vins, make_retriever, remember_ecu_vin, use_make
from ui.workers import VinWorker, nhtsa_cache_path
from ui.widgets import Field, vehicle_table


class IntakeScreen:
    def _intake_view(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        lay = QVBoxLayout(inner)

        scan = QGroupBox("SCAN THE CAR (or fill in by hand below)")
        row = QHBoxLayout(scan)
        self.cmb_adapter = QComboBox()
        self.cmb_adapter.addItem("OBDLink EX: OBD-II + VIN + Subaru SSM", "obdlink")
        self.cmb_adapter.addItem("Bare KKL cable (fallback): Subaru SSM", "kkl")
        self.cmb_adapter.setCurrentIndex(max(0, self.cmb_adapter.findData(CFG["obd_adapter"])))
        self.cmb_adapter.currentIndexChanged.connect(
            lambda: (CFG.__setitem__("obd_adapter", self.cmb_adapter.currentData()),
                     save_config_key("obd_adapter", self.cmb_adapter.currentData())))
        self.b_obd = QPushButton("READ CAR (codes + live data)")
        self.b_obd.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        self.b_obd.clicked.connect(self.act_read_obd)
        self.b_drive = QPushButton(f"TEST DRIVE (every {CFG.get('drive_every_s', 30)} s)")
        self.b_drive.setToolTip("Start before driving, stop after: reads the car the whole drive so the models see "
                                "idle, cruise and load, not one snapshot. Set it up parked; let a passenger work it.")
        self.b_drive.clicked.connect(self.act_test_drive)
        self.drive_worker = None
        self.drive_path = ""
        self.b_load_drive = QPushButton("LOAD DRIVE FILE")
        self.b_load_drive.setToolTip("Bring a saved test drive back into this case. Every drive is written to drives/ "
                                     "as it records, so one survives a crash.")
        self.b_load_drive.clicked.connect(self.act_load_drive)
        row.addWidget(self.cmb_adapter)
        row.addWidget(self.b_obd, 1)
        row.addWidget(self.b_drive)
        row.addWidget(self.b_load_drive)
        lay.addWidget(scan)

        veh = QGroupBox("VEHICLE")
        vbox = QVBoxLayout(veh)
        vin_row = QHBoxLayout()
        self.in_vin = QLineEdit()
        self.in_vin.setPlaceholderText("VIN (17 characters): filled by READ CAR when the car reports it, or type it")
        self.in_vin.setMaxLength(20)
        self.in_vin.editingFinished.connect(lambda: self._apply_vin(self.in_vin.text()))
        self.lbl_kb = QLabel("")
        self.lbl_kb.setWordWrap(True)
        vin_row.addWidget(QLabel("VIN"))
        vin_row.addWidget(self.in_vin, 2)
        vin_row.addWidget(self.lbl_kb, 3)
        self.b_tool = QPushButton("")
        self.b_tool.clicked.connect(self.act_open_tool)
        self.b_tool.hide()
        self.tool_proc = None
        vin_row.addWidget(self.b_tool)
        vbox.addLayout(vin_row)
        vr = QHBoxLayout()
        vbox.addLayout(vr)
        self.vehicles = vehicle_table(self.retriever)
        self.cmb_year, self.cmb_make, self.cmb_model, self.cmb_trans = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        self._fill_years(self.cmb_year)
        self.cmb_make.addItems([""] + MAKES)
        self.cmb_make.currentTextChanged.connect(self._switch_make)
        self.cmb_trans.addItems(TRANS)
        self.in_miles = QLineEdit()
        self.in_miles.setPlaceholderText("miles")
        self.in_miles.setMaximumWidth(130)
        self.in_customer = QLineEdit()
        self.in_customer.setPlaceholderText("customer / plate")
        self.in_customer.setToolTip("Files the visit under this name when the car has no VIN on the deck")
        self.in_customer.setMaximumWidth(200)
        self.lbl_engine = QLabel("")
        self.lbl_engine.setStyleSheet(f"color: {T.MUTED};")
        self.cmb_year.currentTextChanged.connect(self._fill_models)
        self.cmb_model.currentTextChanged.connect(self._show_engine)
        for label, wdg in (("Year", self.cmb_year), ("Make", self.cmb_make), ("Model", self.cmb_model),
                           ("Trans", self.cmb_trans), ("Miles", self.in_miles), ("Customer", self.in_customer)):
            vr.addWidget(QLabel(label))
            vr.addWidget(wdg)
        vr.addWidget(self.lbl_engine, 1)
        lay.addWidget(veh)

        case = QGroupBox("WHAT'S WRONG")
        mf = QFormLayout(case)
        self.bub_dtcs = Field()
        self.bub_dtcs.setPlaceholderText("filled by READ CAR, or type them (e.g. P0420, P0171)")
        self.in_live = QLineEdit()
        self.in_live.setPlaceholderText("filled by READ CAR, or type readings (e.g. LTFT +12%, idle 650)")
        self.bub_symptoms = Field()
        self.bub_symptoms.setPlaceholderText("what it does, when (e.g. ticks when cold, stalls at lights)")
        self.bub_good = Field()
        self.bub_good.setPlaceholderText("already checked and fine (optional)")
        self.in_suspect = QLineEdit()
        self.in_suspect.setPlaceholderText("already suspected (optional)")
        self.in_history = QLineEdit()
        self.in_history.setPlaceholderText("recent work on the car (optional)")
        self.txt_note = QPlainTextEdit()
        self.txt_note.setPlaceholderText("Customer story. Press 🎤 MIC below, or say 'hey deck, intake note'")
        self.txt_note.setMaximumHeight(70)
        mf.addRow("Codes:", self.bub_dtcs)
        mf.addRow("Live data:", self.in_live)
        self.lbl_drive = QLabel("")
        self.lbl_drive.setStyleSheet(f"color: {T.MUTED};")
        self.lbl_drive.setWordWrap(True)
        mf.addRow("", self.lbl_drive)
        mf.addRow("Symptoms:", self.bub_symptoms)
        mf.addRow("Checked OK:", self.bub_good)
        mf.addRow("Suspect:", self.in_suspect)
        mf.addRow("History:", self.in_history)
        mf.addRow("Notes:", self.txt_note)
        lay.addWidget(case)

        btns = ReflowRow()
        b_save = QPushButton("SAVE CASE + SHOW WHAT THE MANUAL HAS")
        b_save.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b_save.clicked.connect(self.act_save_case)
        b_new = QPushButton("NEW CASE")
        b_new.clicked.connect(self.act_new_case)
        b_last = QPushButton("LOAD LAST CASE")
        b_last.clicked.connect(self.act_load_last)
        self.b_visits = QPushButton("PAST VISITS")
        self.b_visits.setToolTip("Earlier cases saved for this VIN")
        self.b_visits.setEnabled(False)
        self.b_visits.clicked.connect(self.act_past_visits)
        for b in (b_save, b_new, b_last, self.b_visits):
            btns.add(b)
        lay.addWidget(btns)

        self.txt_intake = QTextEdit()
        self.txt_intake.setReadOnly(True)
        self.txt_intake.setMinimumHeight(160)
        lay.addWidget(self.txt_intake)

        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.stack.addWidget(page)
        self._clear_case()
        self._load_case()
        self._say(self.txt_intake, "[*] New case. LOAD LAST CASE brings back the previous one.", T.MUTED)

    def _fill_years(self, cmb):
        keep = cmb.currentText()
        years = sorted({y for _, y in self.vehicles}, reverse=True) or list(range(2026, 1980, -1))
        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItems([""] + [str(y) for y in years])
        cmb.blockSignals(False)
        cmb.setCurrentText(keep)

    def _model_list(self, year):
        """Models the make's manuals cover for that year; the built-in list only for Subaru without a kb."""
        have = sorted({m for m, y in self.vehicles if str(y) == year}) if self.vehicles else []
        return have or (MODELS if CFG["make"] == "Subaru" else [])

    def _fill_models(self, year):
        keep = self.cmb_model.currentText()
        names = self._model_list(year)
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        self.cmb_model.addItems([""] + names)
        self.cmb_model.setEditable(not names)
        self.cmb_model.blockSignals(False)
        self.cmb_model.setCurrentText(keep if (keep in names or not names) else "")
        self._show_engine()

    def _launcher(self, make=None):
        """(name, argv) of the program configured for this make, or None."""
        entry = (CFG.get("launchers") or {}).get(make if make is not None else CFG["make"])
        if not entry:
            return None
        if isinstance(entry, str):
            entry = {"cmd": entry}
        cmd = entry.get("cmd") or ""
        argv = [os.path.expanduser(a) for a in (cmd if isinstance(cmd, list) else shlex.split(cmd))]
        if not argv:
            return None
        return entry.get("name") or os.path.splitext(os.path.basename(argv[-1]))[0], argv

    def _show_tool(self):
        tool = self._launcher()
        busy = self.tool_proc is not None
        self.b_tool.setVisible(bool(tool) or busy)
        if tool and not busy:
            self.b_tool.setText(f"OPEN {tool[0].upper()}")
            self.b_tool.setToolTip(f"Opens {tool[0]} for {CFG['make']} and lends it the cable until you close it")

    def act_open_tool(self):
        """Open the make's own program (e.g. FORScan under Wine). Only one program can use the cable,
        so READ CAR waits until that program is closed."""
        tool = self._launcher()
        if not tool or self.tool_proc is not None:
            return
        if self.obd_worker is not None and self.obd_worker.isRunning():
            self._say(self.txt_intake, "[!] Wait for READ CAR to finish, then open it.", T.AMBER)
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        name, argv = tool
        proc = QProcess(self)
        proc.finished.connect(lambda *_: self._tool_closed(name))
        proc.errorOccurred.connect(lambda e: self._tool_closed(name, f"could not start ({argv[0]}: {e.name})"))
        self.tool_proc = proc
        self.b_obd.setEnabled(False)
        self.b_obd.setText(f"CABLE LENT TO {name.upper()}")
        self.b_tool.setEnabled(False)
        self.b_tool.setText(f"{name.upper()} OPEN")
        self._say(self.txt_intake, f"[*] Opening {name}. The cable is its until you close it.", T.MUTED)
        proc.start(argv[0], argv[1:])

    def _tool_closed(self, name, error=""):
        if self.tool_proc is None:
            return
        self.tool_proc = None
        self.b_obd.setEnabled(True)
        self.b_obd.setText("READ CAR (codes + live data)")
        self.b_tool.setEnabled(True)
        self._show_tool()
        if error:
            self._say(self.txt_intake, f"[!] {name} {error}. Check \"launchers\" in config.json.", T.RED)
        else:
            self._say(self.txt_intake, f"[✓] {name} closed; the cable is back for READ CAR.", T.OK)

    def _show_kb(self):
        ok = os.path.isfile(CFG["kb_db"] or "") or CFG["make"] == "Subaru" and isinstance(
            self.retriever, LegacyRetriever)
        search = ""
        if os.path.isfile(CFG["kb_db"] or ""):
            search = (" · vector search on" if getattr(self.retriever, "embed", None) is not None
                      else " · keyword search only")
        self.lbl_kb.setText(self.kb_msg + search)
        self._show_tool()
        self.lbl_kb.setStyleSheet(f"color: {T.OK};" if ok else f"color: {T.AMBER};")

    def _switch_make(self, make):
        """Load the make's knowledge DB (startup, Make dropdown, VIN, or the SSM reply)."""
        if make == CFG["make"] and hasattr(self, "lbl_kb") and self.lbl_kb.text():
            return
        self.kb_msg = use_make(make)
        try:
            self.retriever = make_retriever(CFG)
            self._warm_retriever()
        except Exception as e:
            self.retriever = NullRetriever()
            self.kb_msg = f"{make} database failed to open: {e}"
        self.vehicles = vehicle_table(self.retriever)
        self.lbl_engine.setText("")
        self._fill_years(self.cmb_year)
        self._fill_models(self.cmb_year.currentText())
        if hasattr(self, "cmb_p_make"):
            self.cmb_p_make.setCurrentText(make)
            self._fill_years(self.cmb_p_year)
            self._fill_p_models(self.cmb_p_year.currentText())
        self._show_kb()

    def _apply_vin(self, text):
        """Typed or read VIN -> make (loads its DB), year, and model where the VIN says it plainly."""
        if not text.strip():
            return
        d = decode_vin(text, CFG.get("vin_db"), nhtsa_cache_path())
        if d["note"] and not d["make"]:
            self._say(self.txt_intake, f"[VIN] {d['note']}", T.AMBER)
            return
        self.in_vin.setText(d["vin"])
        if getattr(self, "last_ecu", "") and d.get("vin"):
            remember_ecu_vin(self.last_ecu, d["vin"])
        if d["make"]:
            self.cmb_make.setCurrentText(d["make"])
        if d["year"]:
            self.cmb_year.setCurrentText(str(d["year"]))
        if d["model"]:
            self._fill_models(self.cmb_year.currentText())
            if self.cmb_model.findText(d["model"]) < 0 and self.cmb_model.isEditable():
                self.cmb_model.addItem(d["model"])
            self.cmb_model.setCurrentText(d["model"])
        if d.get("engine") and not self.lbl_engine.text():
            self.lbl_engine.setText(f"Engine: {d['engine']}")
        self._set_trans(d.get("trans", ""))
        if not d.get("engine") and not getattr(self, "vin_worker", None):
            self.vin_worker = VinWorker(d["vin"])
            self.vin_worker.result.connect(self._nhtsa_done)
            self.vin_worker.finished.connect(lambda: setattr(self, "vin_worker", None))
            self.vin_worker.start()
        found = " ".join(str(x) for x in (d["year"], d["make"], d["model"]) if x)
        self._say(self.txt_intake, f"[VIN] {found or 'not decoded'}" + (f" ({d['note']})" if d["note"] else "")
                  + ("" if d["model"] else ". Pick the Model below."), T.OK)
        self._show_past_visits(d["vin"])

    def _set_trans(self, trans):
        t = (trans or "").lower()
        pick = "Manual" if "manual" in t else "Automatic" if re.search(r"auto|cvt|continuously", t) else ""
        if pick:
            self.cmb_trans.setCurrentText(pick)

    def _nhtsa_done(self, vin, d):
        if vin != self.in_vin.text().strip().upper():
            return
        if not d:
            self._say(self.txt_intake, "[VIN] No internet (or NHTSA doesn't know it): engine and transmission "
                      "come from the manuals once you pick the model.", T.MUTED)
            return
        if d.get("make") and d["make"] != self.cmb_make.currentText():
            self.cmb_make.setCurrentText(d["make"])
        if d.get("year"):
            self.cmb_year.setCurrentText(str(d["year"]))
        if d.get("model"):
            self._fill_models(self.cmb_year.currentText())
            hit = next((self.cmb_model.itemText(i) for i in range(self.cmb_model.count())
                        if self.cmb_model.itemText(i) and d["model"].lower().startswith(
                            self.cmb_model.itemText(i).lower())), "")
            if not hit and self.cmb_model.isEditable():
                self.cmb_model.addItem(d["model"])
                hit = d["model"]
            if hit:
                self.cmb_model.setCurrentText(hit)
        if d.get("engine"):
            self.lbl_engine.setText(f"Engine: {d['engine']}")
        self._set_trans(d.get("trans"))
        desc = ", ".join(x for x in (" ".join(str(y) for y in (d.get("year"), d.get("make"), d.get("model"),
                                                                d.get("trim")) if y),
                                     d.get("engine"), d.get("trans"), d.get("drive")) if x)
        self._say(self.txt_intake, f"[VIN] NHTSA: {desc}. Saved on the deck for next time.", T.OK)

    def _apply_ident(self, how, value, engine, ecu=""):
        """What the car itself said: a VIN over OBD-II, an SSM reply (always a Subaru), or only a hint.
        A VIN typed in once is remembered against the ECU's ROM/calibration id, so the next read fills it."""
        self.last_ecu = ecu
        self.vin_at_read = self.in_vin.text().strip().upper()
        known = ecu_vins().get(ecu, "") if ecu else ""
        if how == "vin":
            self._apply_vin(value)
            return
        if known and not self.in_vin.text().strip():
            self._say(self.txt_intake, f"[car] Known ECU ({ecu}): using the VIN saved for it.", T.OK)
            self.in_vin.setText(known)
            self._apply_vin(known)
        if how == "hint":
            if not known:
                self._say(self.txt_intake, f"[car] No VIN from this car ({value or 'no hint'}).", T.AMBER)
            return
        make = make_from_ssm(value)
        if make and self.cmb_make.currentText() != make:
            self.cmb_make.setCurrentText(make)
        if engine and not self.lbl_engine.text():
            self.lbl_engine.setText(f"Engine: {engine}")
        car = None if known else car_from_rom(ecu.replace("ROM ", ""), CFG.get("rom_db"))
        if car and car["year"]:
            self.cmb_year.setCurrentText(str(car["year"]))
            self._fill_models(self.cmb_year.currentText())
            hit = next((self.cmb_model.itemText(i) for i in range(self.cmb_model.count())
                        if self.cmb_model.itemText(i) and car["model"].lower().startswith(
                            self.cmb_model.itemText(i).lower())), "")
            if hit:
                self.cmb_model.setCurrentText(hit)
            desc = " ".join(x for x in (str(car["year"]), make, car["model"], car["submodel"], car["transmission"],
                                        car["market"]) if x)
            self._say(self.txt_intake, f"[car] ECU {ecu}: built for a {desc} (RomRaider ECU list)."
                      + ("" if hit else " Pick the Model below.") + " Type the VIN once to be sure: the deck "
                      "remembers it for this ECU.", T.OK)
            return
        if not known:
            self._say(self.txt_intake, f"[car] ECU answered SSM (ID {value}): {make}, {engine or 'engine unknown'}. "
                      "This car doesn't report its VIN (most cars before 2005 don't, over any cable). Type it "
                      "once (windshield or door jamb): the deck remembers it for this ECU and fills it in "
                      "on every read after.", T.OK)

    def _show_engine(self, *_):
        """The engine comes from the manuals for that model/year; no need to know it."""
        try:
            year = int(self.cmb_year.currentText())
        except ValueError:
            year = None
        engines = sorted(e for e in self.vehicles.get((self.cmb_model.currentText(), year), ()) if e)
        if engines or self.vehicles:
            self.lbl_engine.setText(f"Engine: {' or '.join(engines)}" if engines else "")

    def _title_text(self):
        return f"SYSTEM: {(self.session.get('vehicle') or CFG['vehicle'] or 'no vehicle picked (Intake)').upper()}"

    def _load_case(self):
        v = {}
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            pass
        v["make"] = case_make(v)
        self.cmb_make.blockSignals(True)
        self.cmb_make.setCurrentText(v.get("make", ""))
        self.cmb_make.blockSignals(False)
        if v.get("make", "") != CFG["make"]:
            self._switch_make(v.get("make", ""))
        self._show_kb()
        self.in_vin.setText(v.get("vin", ""))
        self.cmb_year.setCurrentText(v.get("year", ""))
        self._fill_models(self.cmb_year.currentText())
        self.cmb_model.setCurrentText(v.get("model", ""))
        self.cmb_trans.setCurrentText(v.get("trans", ""))
        self.in_miles.setText(v.get("miles", ""))
        self.in_customer.setText(v.get("customer", ""))
        self.bub_dtcs.set_text(self.session.get("dtcs"))
        self.in_live.setText(self.session.get("telemetry"))
        self._show_drive_line()
        self.bub_symptoms.set_text(self.session.get("symptoms"))
        self.bub_good.set_text(self.session.get("ruled_out"))
        self.in_suspect.setText(self.session.get("suspect"))
        self.in_history.setText(self.session.get("history"))
        self.txt_note.setPlainText(self.session.get("intake_note"))
