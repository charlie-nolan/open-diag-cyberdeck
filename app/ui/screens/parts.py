"""Repair Guide and Parts screens."""
import hashlib
import json
import os
import sqlite3
import theme as T
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices, QPixmap, QTextCursor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget)
from vehicle_id import MAKES
from ui.config import CFG, PS, PSQR, PSSHEET, PS_DIR, is_part_number
from ui.context import focus_specs, fsm_hits, kb_status, repair_from_verdict, spaced
from ui.widgets import Bubbles, Field


class RepairPartsScreen:
    def _sop_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("REPAIR (type or say it, or leave empty to use the verdict)")
        bl = QVBoxLayout(box)
        self.bub_repair = Field()
        self.bub_repair.setPlaceholderText("e.g. the repair the verdict named, or any job")
        bl.addWidget(self.bub_repair)
        b = QPushButton("PULL OEM INSTRUCTIONS && TORQUE SPECS")
        b.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b.clicked.connect(self.act_sop)
        bl.addWidget(b)
        lay.addWidget(box)
        self.txt_sop = QTextEdit()
        self.txt_sop.setReadOnly(True)
        lay.addWidget(self.txt_sop, 1)
        self.stack.addWidget(w)

    def act_sop(self):
        if self._busy():
            return
        verdict = self.session.get("final_verdict")
        job = self.bub_repair.text() or repair_from_verdict(verdict) or verdict[:300]
        if not job:
            self.txt_sop.clear()
            self._say(self.txt_sop, "Tap a repair above, or build a verdict first.", T.AMBER)
            return
        hits = fsm_hits(self.retriever, job + " removal installation", k=3, chars=900)
        fsm = [h for h, _ in hits]
        specs = focus_specs(job, self.retriever.specs(job, limit=40))[:10]
        for term in [] if specs else [t for t in job.replace(",", " ").split() if len(t) > 4][:4]:
            specs += [s for s in self.retriever.specs(term, limit=4) if s not in specs]
        self.txt_sop.clear()
        if not self.bub_repair.text() and len(job) < 60:
            self._say(self.txt_sop, f"REPAIR (from the verdict): {job}", T.AMBER)
        self._say(self.txt_sop, "=== SPECS FROM DATABASE (deterministic) ===", T.TEXT)
        self._say(self.txt_sop, "\n".join(" • " + s for s in specs) or "(none matched: check the FSM)", T.OK)
        self._say(self.txt_sop, "=== FSM SOURCES ===", T.TEXT)
        for h, figs in hits:
            self._say(self.txt_sop, h.split("\n", 1)[0], T.PURPLE_TEXT)
            self._say_figs(self.txt_sop, figs, label=h.split("\n", 1)[0])
        if not hits:
            self._say(self.txt_sop, "(none)")
        self._say(self.txt_sop, "=== PROCEDURE (deep model, from the sources above) ===", T.TEXT)
        prompt = (f"REPAIR: {job}\n\n" + (f"VERDICT:\n{verdict}\n\n" if verdict and not self.bub_repair.text() else "") +
                  "FSM EXCERPTS:\n" + "\n\n".join(fsm) + "\n\nSPECS:\n" + "\n".join(specs) +
                  "\n\nWrite a numbered shop procedure: tools, removal, cleaning/inspection, installation, "
                  "torque sequence, verification. Only use torque values listed under SPECS; where one is "
                  "missing write 'see FSM'. Mark any UNVERIFIED spec as such.")
        self._say(self.txt_sop, "")
        self._run("deep", prompt, self.txt_sop, lambda t: None)

    def _ps_cfg(self, parts_row=False):
        """parts_sourcing settings for a car: the Parts tab's own Year/Model row, else the Intake car."""
        cfg = PS.settings()
        make = self.cmb_p_make.currentText() if parts_row else CFG["make"]
        cfg["kb_db"], cfg["kb_ok"], cfg["kb_msg"] = kb_status(make)
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        if parts_row:
            v = {"year": self.cmb_p_year.currentText(), "model": self.cmb_p_model.currentText()}
        year = int(v["year"]) if str(v.get("year", "")).isdigit() else cfg["car"].get("year")
        cfg["car"] = {"make": make, "model": v.get("model") or cfg["car"].get("model"), "year": year}
        return cfg

    def _sync_parts_car(self):
        """The Parts search row follows the Intake car; change it there to look up another car."""
        self.cmb_p_make.setCurrentText(self.cmb_make.currentText())
        self.cmb_p_year.setCurrentText(self.cmb_year.currentText())
        self._fill_p_models(self.cmb_p_year.currentText())
        self.cmb_p_model.setCurrentText(self.cmb_model.currentText())

    def _fill_p_models(self, year):
        keep = self.cmb_p_model.currentText()
        names = self._model_list(year)
        self.cmb_p_model.clear()
        self.cmb_p_model.addItems([""] + names)
        self.cmb_p_model.setEditable(not names)
        self.cmb_p_model.setCurrentText(keep if (keep in names or not names) else "")

    def part_text(self, arg):
        """Same lookup as the Parts tab, as text (for /part in Chat). Never saved."""
        if PS is None:
            return f"Parts finder not found in {PS_DIR}"
        try:
            return self._part_text(arg)
        except sqlite3.Error as e:
            return f"Parts lookup failed: {e}. Rebuild/re-copy kb/subaru.db."

    def _part_text(self, arg):
        cfg = self._ps_cfg()
        if not cfg["kb_ok"]:
            return cfg["kb_msg"]
        if is_part_number(arg.strip()):
            return PS.sourcing(arg, cfg=cfg)["text"]
        rows = PS.parts_for_job(cfg["kb_db"], arg, cfg["car"]["model"], cfg["car"]["year"])
        if not rows:
            from parts_sourcing import junk_quote
            return junk_quote.quote_job(arg, cfg) or f"No CHARM part number for '{arg}' on this car."
        head = "\n".join(f"{pn}  {op}  {price or ''}" for pn, op, price in rows[:6])
        return head + "\n\n" + PS.sourcing(rows[0][0], cfg=cfg)["text"]

    def _parts_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lbl_part = QLabel("Parts for the verdict's repair show up here. Or search by hand.")
        self.lbl_part.setStyleSheet(f"color: {T.AMBER}; font-weight: bold;")
        self.lbl_part.setWordWrap(True)
        self.b_manual = QPushButton("MANUAL SEARCH")
        self.b_manual.setCheckable(True)
        self.b_manual.setToolTip("Look up any part on any year/model by hand")
        self.b_sheet = QPushButton("PRINT SHEET")
        self.b_sheet.setToolTip("Save a printable page with the QR codes and open it in the browser")
        self.b_sheet.clicked.connect(self.act_parts_sheet)
        top.addWidget(self.lbl_part, 1)
        top.addWidget(self.b_manual)
        top.addWidget(self.b_sheet)
        lay.addLayout(top)
        box = QGroupBox("MANUAL PART SEARCH")
        self.b_manual.toggled.connect(box.setVisible)
        bl = QVBoxLayout(box)
        car = QHBoxLayout()
        self.cmb_p_year, self.cmb_p_make, self.cmb_p_model = QComboBox(), QComboBox(), QComboBox()
        self._fill_years(self.cmb_p_year)
        self.cmb_p_make.addItems([""] + MAKES)
        self.cmb_p_make.setEnabled(False)
        self.cmb_p_year.currentTextChanged.connect(self._fill_p_models)
        self.in_part = QLineEdit()
        self.in_part.setPlaceholderText("part name or OEM part number")
        self.in_part.returnPressed.connect(self.act_find_parts)
        for label, wdg in (("Year", self.cmb_p_year), ("Make", self.cmb_p_make), ("Model", self.cmb_p_model),
                           ("Part", self.in_part)):
            car.addWidget(QLabel(label))
            car.addWidget(wdg, 3 if wdg is self.in_part else 0)
        bl.addLayout(car)
        row = QHBoxLayout()
        b = QPushButton("SEARCH PART NUMBERS && PRICES")
        b.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b.clicked.connect(self.act_find_parts)
        row.addWidget(b, 1)
        bl.addLayout(row)
        box.hide()
        lay.addWidget(box)
        self.pn_box = QGroupBox("PART NUMBERS FOR THIS CAR (tap one)")
        self.pn_lay = QVBoxLayout(self.pn_box)
        self.bub_pn = None
        self.pn_box.hide()
        lay.addWidget(self.pn_box)
        mid = QHBoxLayout()
        self.txt_parts = QTextEdit()
        self.txt_parts.setReadOnly(True)
        self.txt_parts.setStyleSheet("font-size: 18px;")
        mid.addWidget(self.txt_parts, 3)
        self.qr_box = QWidget()
        self.qr_lay = QGridLayout(self.qr_box)
        self.qr_lay.setContentsMargins(0, 0, 0, 0)
        qr_scroll = QScrollArea()
        qr_scroll.setWidgetResizable(True)
        qr_scroll.setWidget(self.qr_box)
        qr_scroll.setMinimumWidth(240)
        mid.addWidget(qr_scroll, 1)
        lay.addLayout(mid, 1)
        self.parts_shown = []
        self.stack.addWidget(w)
        self._sync_parts_car()
        if PS is None:
            self._say(self.txt_parts, f"Parts finder not found in {PS_DIR}. Copy the parts_sourcing folder there.",
                      T.AMBER)

    def act_find_parts(self):
        if PS is None:
            return
        try:
            self._find_parts()
        except sqlite3.Error as e:
            self.txt_parts.clear()
            self._say(self.txt_parts, f"Parts lookup failed: {e}. Rebuild/re-copy kb/subaru.db.", T.RED)

    def _find_parts(self):
        job = self.in_part.text().strip()
        if not job:
            self.txt_parts.clear()
            self._say(self.txt_parts, "Type a part name or number, then SEARCH.", T.AMBER)
            return
        cfg = self._ps_cfg(parts_row=True)
        self.txt_parts.clear()
        if not cfg["kb_ok"]:
            self._say(self.txt_parts, cfg["kb_msg"], T.AMBER)
            return
        if self.bub_pn is not None:
            self.bub_pn.deleteLater()
            self.bub_pn = None
        self.pn_box.hide()
        if is_part_number(job.strip()):
            self.lbl_part.setText(f"{job.strip().upper()} · {cfg['car']['year']} {cfg['car']['make']} {cfg['car']['model']}")
            self._show_part(job.strip())
            return
        rows = PS.parts_for_job(cfg["kb_db"], job, cfg["car"]["model"], cfg["car"]["year"])
        self.lbl_part.setText(f"{job} · {cfg['car']['year']} {cfg['car']['make']} {cfg['car']['model']}")
        if not rows:
            from parts_sourcing import junk_quote
            self._say(self.txt_parts, spaced(junk_quote.quote_job(job, cfg)) or
                      "No CHARM part number for this job on this car. Try other words, or type the OEM number.")
            self._clear_qr()
            return
        labels = [f"{pn}  {op.split(' > ')[-1]}" for pn, op, _ in rows[:12]]
        self.bub_pn = Bubbles([(None, labels)], cols=3, other_hint="", single=True)
        self.bub_pn.other.hide()
        for b in self.bub_pn.buttons:
            b.clicked.connect(lambda _, t=b.text(): self._show_part(t.split()[0]))
        self.pn_lay.addWidget(self.bub_pn)
        self.pn_box.show()
        self.bub_pn.buttons[0].setChecked(True)
        self._show_part(rows[0][0])

    def _clear_qr(self):
        while self.qr_lay.count():
            w = self.qr_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _show_part(self, pn):
        res = PS.sourcing(pn, cfg=self._ps_cfg(parts_row=True))
        self.parts_shown = [res]
        self.txt_parts.clear()
        self._say(self.txt_parts, spaced(res["text"]))
        self.txt_parts.moveCursor(QTextCursor.MoveOperation.Start)
        self._clear_qr()
        if not PSQR.available():
            self.qr_lay.addWidget(QLabel("QR codes need: pip install segno"), 0, 0)
            return
        qdir = os.path.join(CFG["cache_dir"], "qr")
        os.makedirs(qdir, exist_ok=True)
        for i, ln in enumerate(res["links"]):
            path = os.path.join(qdir, hashlib.sha1(ln.url.encode()).hexdigest()[:16] + ".png")
            if not os.path.isfile(path):
                PSQR.qr_png(ln.url, path, scale=4)
            img = QLabel()
            img.setPixmap(QPixmap(path))
            cap = QLabel(ln.label + ("" if ln.verified else " (link unverified)"))
            cap.setWordWrap(True)
            cap.setStyleSheet("font-size: 16px; margin-bottom: 14px;")
            self.qr_lay.addWidget(img, i * 2, 0)
            self.qr_lay.addWidget(cap, i * 2 + 1, 0)
        self._fill_row52(res)

    def _fill_row52(self, res):
        """Row52 by hand: its terms forbid crawling, so the deck never fetches it. Under Row52's QR
        code: a QR for every donor car at once (the make over all the donor years: Row52 takes one model per search),
        then the donor cars one per line, each opening its own search (model + years when row52_ids.json has the
        model's id, else make + years), and a box to teach the deck a make's/model's ids from a pasted Row52 link."""
        r52 = next((ln for ln in res["links"] if ln.site == "row52"), None)
        try:
            from parts_sourcing.interchange import donors
            from parts_sourcing import row52
            cars = donors(res["interchange"])
        except Exception:
            cars = []
        if not (r52 and cars):
            return
        pcfg = self._ps_cfg(parts_row=True)
        make, zip_code = (pcfg.get("car") or {}).get("make", ""), pcfg.get("home_zip", "")
        all_years = sorted({y for d in cars for y in d["years"]})
        span = f"{all_years[0]}-{all_years[-1]}" if len(all_years) > 1 else str(all_years[0])
        row = self.qr_lay.rowCount()

        def add(w):
            nonlocal row
            self.qr_lay.addWidget(w, row, 0)
            row += 1
        url = row52.search_url(make, zip_code, all_years)
        if url and PSQR is not None and PSQR.available():
            qdir = os.path.join(CFG["cache_dir"], "qr")
            path = os.path.join(qdir, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
            if not os.path.isfile(path):
                PSQR.qr_png(url, path, scale=4)
            img = QLabel()
            img.setPixmap(QPixmap(path))
            add(img)
            cap = QLabel(f"Row52: every {make} {span} near {zip_code}. Look for the cars below.")
            cap.setWordWrap(True)
            cap.setStyleSheet("font-size: 16px;")
            add(cap)
        elif not url:
            cap = QLabel(f"Row52 doesn't know {make}'s id yet: paste one Row52 search link for it below.")
            cap.setWordWrap(True)
            cap.setStyleSheet(f"color: {T.AMBER};")
            add(cap)
        head = QLabel("Cars that can have this part (tap one to open its Row52 search here):")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {T.AMBER}; font-weight: bold; margin-top: 8px;")
        add(head)
        for d in cars:
            u = row52.search_url(make, zip_code, d["years"], d["model"]) or r52.url
            b = QPushButton(f"{d['model']} {d['years_text']}" + (f"  ({d['engine']})" if d["engine"] else ""))
            b.setFlat(True)
            b.setStyleSheet("text-align: left; padding: 2px 4px; font-size: 16px;")
            b.setToolTip(u)
            b.clicked.connect(lambda _=False, d=d, u=u, mk=make: self._row52_by_hand(mk, d, u))
            add(b)
        teach = QLabel("Teach the deck a Row52 id: search one car on row52.com, then pick the model and paste the link:")
        teach.setWordWrap(True)
        teach.setStyleSheet(f"color: {T.MUTED}; margin-top: 8px;")
        add(teach)
        self.cmb_r52_model = QComboBox()
        self.cmb_r52_model.addItems(sorted({d["model"] for d in cars}))
        add(self.cmb_r52_model)
        self.in_r52_link = QLineEdit()
        self.in_r52_link.setPlaceholderText("paste a row52.com/Search/... link, Enter")
        self.in_r52_link.returnPressed.connect(lambda mk=make: self._row52_teach(mk))
        add(self.in_r52_link)

    def _row52_teach(self, make):
        """Save the make's/model's Row52 ids from a search link the user pasted (row52_ids.json)."""
        from parts_sourcing import row52
        model = self.cmb_r52_model.currentText()
        try:
            got = row52.learn(self.in_r52_link.text().strip(), make, model)
        except ValueError as e:
            self._say(self.txt_parts, f"Row52 link not saved: {e}", T.AMBER)
            return
        self.in_r52_link.clear()
        self._say(self.txt_parts, f"[✓] Row52 ids saved: {got}. Show the part again to use them.", T.OK)

    def _row52_by_hand(self, make, d, url):
        """One donor car (or all of them): open its Row52 search, copy what to look for."""
        text = f"{make} {d['model']}".strip()
        QApplication.clipboard().setText(text)
        QDesktopServices.openUrl(QUrl(url))
        self._say(self.txt_parts, f"Row52 opened: {text}, years {d['years_text']}"
                  + (f", engine {d['engine']}" if d["engine"] else "") + f" (\"{text}\" copied). Only cars on the "
                  "fit list above take this part; Row52 can't filter by engine, so check that on the car.", T.OK)

    def act_parts_sheet(self):
        if PS is None or not self.parts_shown:
            self._say(self.txt_parts, "Find a part first.", T.AMBER)
            return
        car = self._ps_cfg(parts_row=True)["car"]
        path = os.path.join(CFG["cache_dir"], "parts_sheet.html")
        os.makedirs(CFG["cache_dir"], exist_ok=True)
        with open(path, "w") as f:
            f.write(PSSHEET.render(self.parts_shown, f"{car['year']} {car['make']} {car['model']}",
                                   self.session.get("final_verdict")[:80]))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self._say(self.txt_parts, f"[✓] Sheet saved: {path}", T.OK)
