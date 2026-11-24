"""Screen 2, Maintenance, plus the common-issues and wildcard buttons."""
import common_issues
import json
import maintenance
import os
import re
import sqlite3
import sys
import theme as T
import time
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from ui.config import CFG, PS
from ui.context import PAGE, fsm_hits, log_caught


class MaintenanceScreen:
    MAINT_STATES = ("", "done before", "doing today", "declined")
    MAINT_GROUPS = {"past": "SHOULD HAVE BEEN DONE in the last {lb:,} mi (ask the customer)",
                    "next": "DUE NOW: next service at {next:,} mi",
                    "longer": "LONG-INTERVAL JOBS last due before that (ask when they were really done)",
                    "later": "COMING UP at {later}"}

    def _maint_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lbl_maint = QLabel("Factory schedule for the car on Intake (needs model, year and miles).")
        self.lbl_maint.setStyleSheet(f"color: {T.AMBER}; font-weight: bold;")
        self.lbl_maint.setWordWrap(True)
        self.cmb_maint_kind = QComboBox()
        self.cmb_maint_kind.addItems(["Normal service", "Severe service"])
        self.cmb_maint_kind.setToolTip("Severe: towing, dusty roads, short trips, extreme heat or cold")
        self.cmb_maint_kind.currentIndexChanged.connect(lambda _: self._maint_fill())
        b_man = QPushButton("MANUAL FOR THIS")
        b_man.setToolTip("The manual's procedure for the picked row")
        b_man.clicked.connect(self.act_maint_manual)
        top.addWidget(self.lbl_maint, 1)
        top.addWidget(self.cmb_maint_kind)
        top.addWidget(b_man)
        if PS is not None:
            b_parts = QPushButton("PARTS + PRICES FOR THIS")
            b_parts.clicked.connect(self.act_maint_parts)
            top.addWidget(b_parts)
        lay.addLayout(top)
        self.tree_maint = QTreeWidget()
        self.tree_maint.setHeaderLabels(["Item", "Due (mi)", "Status", "Done at (mi, date)"])
        self.tree_maint.setColumnWidth(0, 300)
        self.tree_maint.setColumnWidth(1, 320)
        self.tree_maint.setColumnWidth(2, 150)
        self.tree_maint.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree_maint.itemClicked.connect(self._maint_clicked)
        self.tree_maint.itemChanged.connect(self._maint_edited)
        lay.addWidget(self.tree_maint, 3)
        self.txt_maint = QTextEdit()
        self.txt_maint.setReadOnly(True)
        lay.addWidget(self.txt_maint, 2)
        self.maint_rows = {}
        self.stack.addWidget(w)

    def _maint_status(self):
        try:
            return json.loads(self.session.get("maint_status") or "{}")
        except ValueError:
            return {}

    def _maint_past_done(self):
        """{item key: 'mi, date' text} for items done before on this car, from its earlier visits (newest wins)."""
        try:
            key = self._file_key(json.loads(self.session.get("vehicle_info") or "{}"))
        except ValueError:
            key = ""
        out = {}
        for _, started, _, case in (self.session.visits(key, exclude=self.session.get("visit_id") or None)
                                    if key else []):
            try:
                st = json.loads(case.get("maint_status") or "{}")
            except ValueError:
                continue
            for k, v in st.items():
                if k not in out and v.get("s") in ("done before", "doing today"):
                    out[k] = v.get("at") or started[:10]
        return out

    def _maint_fill(self):
        """Rebuild the checklist from the factory schedule, the miles on Intake and what's been ticked."""
        self.tree_maint.blockSignals(True)
        self.tree_maint.clear()
        self.maint_rows = {}
        self.tree_maint.blockSignals(False)
        self.txt_maint.clear()
        miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0)
        r, db = self.retriever, getattr(self.retriever, "db", None)
        if db is None or not getattr(r, "vehicle_ids", None):
            self.lbl_maint.setText("Pick the car (model and year) on Intake first.")
            return
        if not miles:
            self.lbl_maint.setText("Type the car's miles on Intake first.")
            return
        severe = self.cmb_maint_kind.currentIndex() == 1
        try:
            sched, _ = maintenance.schedule(db, r.vehicle_ids, severe)
        except sqlite3.Error as e:
            sched = {}
            self._say(self.txt_maint, f"[!] schedule lookup failed: {e}", T.AMBER)
        p = maintenance.plan(sched, miles)
        rows = maintenance.checklist(p)
        if not rows:
            self.lbl_maint.setText("No CHARM service schedule for this car. The manual's own schedule pages are below.")
            hits = fsm_hits(r, "periodic maintenance schedule service intervals", k=2, chars=900)
            if hits:
                self._say_hits(self.txt_maint, hits)
            return
        status = self._maint_status()
        past = self._maint_past_done()
        for k, at in past.items():
            if k not in status:
                status[k] = {"s": "done before", "at": at, "earlier": True}
        self.session.set("maint_status", json.dumps(status) if status else "")
        kind = "Severe" if severe else "Normal"
        self.lbl_maint.setText(f"{kind} Service schedule (CHARM) at {miles:,} mi. Tap Status to tick each item; "
                               "tap Done at to type when it was done." + (
                                   f" (Schedule pages stop at {p['last_mi']:,} mi; later points repeat each item's "
                                   "spacing.)" if p["extrapolated"] else ""))
        self.tree_maint.blockSignals(True)
        heads = {}
        for g, e in rows:
            if g not in heads:
                title = self.MAINT_GROUPS[g].format(lb=maintenance.LOOKBACK, next=p["next_mi"],
                                                    later=" and ".join(f"{m:,}" for m in p["later_mi"]) + " mi")
                h = QTreeWidgetItem([title])
                h.setFirstColumnSpanned(True)
                h.setFlags(Qt.ItemFlag.ItemIsEnabled)
                h.setForeground(0, QColor(T.TEXT))
                self.tree_maint.addTopLevelItem(h)
                h.setFirstColumnSpanned(True)
                heads[g] = h
            st = status.get(e["key"], {})
            it = QTreeWidgetItem([f"{e['action']} {e['item']}", e["due_text"], "", st.get("at", "")])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            it.setData(0, Qt.ItemDataRole.UserRole, e["key"])
            if e["note"]:
                it.setToolTip(0, e["note"])
            heads[g].addChild(it)
            cmb = QComboBox()
            cmb.addItems(["—"] + list(self.MAINT_STATES[1:]))
            cmb.setCurrentIndex(self.MAINT_STATES.index(st.get("s", "")) if st.get("s", "") in self.MAINT_STATES else 0)
            cmb.currentIndexChanged.connect(lambda i, k=e["key"]: self._maint_set(k, self.MAINT_STATES[i]))
            self.tree_maint.setItemWidget(it, 2, cmb)
            self.maint_rows[e["key"]] = (it, e)
            self._maint_paint(e["key"])
        self.tree_maint.expandAll()
        self.tree_maint.blockSignals(False)
        self._maint_rollup()

    def _maint_paint(self, key):
        """Colour a row by its status, and show when it comes due again after a typed 'done at' mileage."""
        it, e = self.maint_rows[key]
        st = self._maint_status().get(key, {})
        colour = {"done before": T.OK, "doing today": T.AMBER, "declined": T.MUTED}.get(st.get("s"), T.TEXT)
        due = e["due_text"]
        done_mi = self._maint_miles(st.get("at", ""))
        if st.get("s") in ("done before", "doing today") and done_mi:
            again = maintenance.next_after(e, done_mi)
            if again:
                due = f"done {done_mi:,}; again at {again:,}"
        self.tree_maint.blockSignals(True)
        for c in (0, 1, 3):
            it.setForeground(c, QColor(colour))
        it.setText(1, due)
        it.setText(3, st.get("at", ""))
        self.tree_maint.blockSignals(False)

    @staticmethod
    def _maint_miles(text):
        """The mileage in a typed 'done at' ('98,000 mi, 2019', '98000', '98k'), never a year; None if there's none."""
        m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", text or "", re.I)
        if m:
            return int(float(m.group(1)) * 1000)
        m = re.search(r"\b(\d{1,3}(?:,\d{3})+|\d{5,7})\b", text or "")
        return int(m.group(1).replace(",", "")) if m else None

    def _maint_clicked(self, it, col):
        if col == 3 and it.data(0, Qt.ItemDataRole.UserRole):
            self.tree_maint.editItem(it, 3)
        key = it.data(0, Qt.ItemDataRole.UserRole)
        if key and key in self.maint_rows and self.maint_rows[key][1]["note"]:
            self.lbl_voice.setText(self.maint_rows[key][1]["note"][:160])

    def _maint_edited(self, it, col):
        key = it.data(0, Qt.ItemDataRole.UserRole)
        if col != 3 or not key:
            return
        status = self._maint_status()
        st = status.setdefault(key, {"s": ""})
        st["at"] = it.text(3).strip()
        st.pop("earlier", None)
        if st["at"] and not st["s"]:
            st["s"] = "done before"
            w = self.tree_maint.itemWidget(it, 2)
            w.blockSignals(True)
            w.setCurrentIndex(self.MAINT_STATES.index("done before"))
            w.blockSignals(False)
        self._maint_save(status)
        self._maint_paint(key)

    def _maint_set(self, key, state):
        status = self._maint_status()
        st = status.setdefault(key, {})
        st["s"] = state
        st.pop("earlier", None)
        if state == "doing today":
            miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0)
            st["at"] = f"{miles:,} mi, {time.strftime('%Y-%m-%d')}" if miles else time.strftime("%Y-%m-%d")
        elif not state:
            st.pop("at", None)
        if not state and not st.get("at"):
            status.pop(key, None)
        self._maint_save(status)
        self._maint_paint(key)
        self._maint_rollup()

    def _maint_save(self, status):
        """Keep the ticks with the visit; today's jobs go to Final Diagnosis (as before with /addmaint)."""
        self.session.set("maint_status", json.dumps(status) if status else "")
        today = [f"{self.maint_rows[k][1]['action']} {self.maint_rows[k][1]['item']} (factory schedule, "
                 f"{self.maint_rows[k][1]['due_text']})" for k, v in status.items()
                 if v.get("s") == "doing today" and k in self.maint_rows]
        self.session.set("maint_selected", "\n".join(today))
        self._file_visit()

    def _maint_rollup(self):
        """Today's jobs with the CHARM book hours and part numbers for this car."""
        status = self._maint_status()
        today = [self.maint_rows[k][1] for k, v in status.items() if v.get("s") == "doing today" and k in self.maint_rows]
        self.txt_maint.clear()
        if not today:
            self._say(self.txt_maint, "Jobs marked 'doing today' are listed here with book hours and part numbers, "
                      "and go into Final Diagnosis.", T.MUTED)
            return
        cache = getattr(self, "maint_cache", {})
        self.maint_cache = cache
        cfg = None
        if PS is not None:
            try:
                cfg = self._ps_cfg()
                cfg = cfg if cfg["kb_ok"] else None
            except Exception as e:
                log_caught("maintenance parts settings", e)
                cfg = None
        lines, total = [], 0.0
        for e in today:
            name = e["item"]
            ck = (CFG.get("kb_model"), CFG.get("kb_year"), name)
            if ck not in cache and cfg is not None:
                hours = parts = None
                try:
                    from parts_sourcing import junk_quote
                    hours = junk_quote.labor_hours(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])
                except Exception as e:
                    log_caught(f"book hours for {name!r}", e)
                try:
                    parts = PS.parts_for_job(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])[:3]
                except Exception as e:
                    log_caught(f"part numbers for {name!r}", e)
                cache[ck] = (hours, parts or [])
            hours, parts = cache.get(ck, (None, []))
            if hours and hours.get("hours"):
                total += float(hours["hours"])
            lines.append(f"{e['action']} {name}: " + (f"{float(hours['hours']):g} h book time" if hours and
                                                      hours.get("hours") else "no book time found")
                         + ("\n      parts: " + "; ".join(f"{pn} {op.split(' > ')[-1]}" + (f" ({price})" if price else "")
                                                          for pn, op, price in parts) if parts else ""))
        self._say(self.txt_maint, f"DOING TODAY ({len(today)} job{'s' if len(today) != 1 else ''}, {total:g} h book time found):\n"
                  + "\n".join("  " + l for l in lines)
                  + "\n(Book hours and part numbers are CHARM's for this car; check each part number before ordering.)",
                  T.AMBER)

    def _maint_picked(self):
        it = self.tree_maint.currentItem()
        key = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        return self.maint_rows.get(key, (None, None))[1] if key else None

    def act_maint_manual(self):
        e = self._maint_picked()
        if e is None:
            self.lbl_voice.setText("Tap a maintenance row first")
            return
        hits = fsm_hits(self.retriever, f"{e['action']} {e['item']} procedure", k=2, chars=1200)
        self.txt_maint.clear()
        self._say(self.txt_maint, f"{e['action']} {e['item']}" + (f": {e['note']}" if e["note"] else ""), T.TEXT)
        if hits:
            self._say_hits(self.txt_maint, hits)
        else:
            self._say(self.txt_maint, "No manual match for this item.", T.AMBER)

    def act_maint_parts(self):
        e = self._maint_picked()
        if e is None:
            self.lbl_voice.setText("Tap a maintenance row first")
            return
        self.in_part.setText(e["item"])
        self.act_find_parts()
        self.switch(PAGE["parts"])

    def act_common(self):
        """Top known problems for the car on Intake, and at what mileage. Python counts; the model only words it."""
        model, year = CFG.get("kb_model"), CFG.get("kb_year")
        miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0) or None
        db = getattr(self.retriever, "db", None)
        path = os.path.expanduser(CFG.get("kb_db") or "")
        if not os.path.isfile(path) or db is None:
            self._say(self.txt_chat, "  This car's database isn't loaded, so there's nothing to count.", T.AMBER)
            return
        aliases = getattr(sys.modules.get(type(self.retriever).__module__), "MODEL_ALIASES", {})
        r = common_issues.gather(path, model, year, miles, aliases)
        if r.get("error"):
            self._say(self.txt_chat, "  " + r["error"], T.AMBER)
            return
        facts = "\n".join(r["lines"])
        self._say(self.txt_chat, f"Counts for {r['scope']} (from the database):\n{facts}", T.OK, who="Deck")
        car = " ".join(x for x in (str(year or ""), CFG.get("make") or "", model) if x)
        prompt = common_issues.PROMPT.format(car=car, mi=f" with {miles:,} miles" if miles else "",
                                             scope=r["scope"], facts=facts)
        role = "fast"
        self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
        self._run(role, prompt, self.txt_chat, lambda t: None, keep_history=False)

    def act_wildcard(self, question):
        """Dr. House mode: 5 unlikely-but-possible causes, high temperature. SAVE WILDCARD keeps them as long shots."""
        case = self.session.case_summary()
        q = question or "What else could this be?"
        forum = getattr(self.retriever, "knowledge", lambda *a, **k: [])(case + " " + q, k=3)
        rejected = self._rejected_causes()
        prompt = (f"CASE:\n{case}\n\nFORUM FIXES (other owners, unverified):\n" + ("\n\n".join(forum) or "(none)") +
                  ("\n\nCAUSES REJECTED BY THE CARD'S SOURCE CHECK (and why; unproven, may still be worth a "
                   "thought):\n" + "\n".join(rejected) if rejected else "") +
                  f"\n\nQUESTION: {q}\n\n"
                  "You are a brilliant, contrarian diagnostician in the style of Dr. House. Ignore the obvious answer. "
                  "Give exactly 5 unlikely but physically possible causes that fit these symptoms and numbers, "
                  "including odd interactions and things a tired mechanic would miss. For each: the cause in one "
                  "line, the clue in the case that points to it, and a cheap test to rule it in or out. "
                  "Never contradict anything listed as verified good.")
        self._say(self.txt_chat, "", T.PURPLE_TEXT, who="Wildcard (speculative)")
        def done(t):
            if "[LLM error" not in t:
                self.last_wildcard = t.strip()
            self._say(self.txt_chat, "  (not saved: tap SAVE WILDCARD to add these to the case as long shots)",
                      T.MUTED)
        self._run("deep", prompt, self.txt_chat, done, keep_history=False, temperature=0.85)

    def _debate_done(self, text):
        self.last_debate = text
        self._say(self.txt_chat, "  (not saved: tap SAVE DEBATE to add it to the case)", T.MUTED)
