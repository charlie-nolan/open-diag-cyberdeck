"""python -m diagent gui: the same case run as `run`, in a PyQt6 window (brief: handoff/DIAGENT_GUI.md).

A thin layer. The harness runs unchanged on a worker thread with GuiHuman as the user's side of the loop: each
ask/act/confirm/choose call is shown on screen and waits for their reply, the same calls TerminalHuman answers in
the terminal. Colours come from app/theme.py, shared with the Home app. Fits landscape and portrait screens at
any desktop scaling (narrow windows stack the side panel under the work area)."""
import argparse
import html
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (QApplication, QBoxLayout, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QTabWidget,
                             QTextBrowser, QVBoxLayout, QWidget)

from . import HERE, ROOT, harness
from .case import Store

import theme as T                      # app/theme.py (diagent/__init__ puts app/ on sys.path)
from ui_fit import FitRoot, ReflowRow  # app/ui_fit.py

ICON = os.path.join(HERE, "icon.svg")
SHOTS = os.path.join(ROOT, "logs", "diagent", "gui_shots")
ID_RX = re.compile(r"\[([A-Z]{1,2}\d+)\]")
COMMANDS = ("done", "can't", "skip", "back", "stop")


# ---------------------------------------------------------------------------- The user's side, on the worker
class Bridge(QObject):
    """Lives on the UI thread; the worker's calls arrive here as queued signals."""
    request = pyqtSignal(dict)    # a question that waits for the user (ask, act, confirm, choose, ask_text)
    event = pyqtSignal(dict)      # say / strip / figures / log: shown, nothing waits
    finished = pyqtSignal(dict)


class GuiHuman:
    """TerminalHuman's interface for the window. Blocking calls wait on a queue the UI fills. wait_s counts the
    time spent waiting on the user (it doesn't count toward the agent's time cap). auto: a ScriptedHuman whose
    answers are filled in after a pause (--play)."""

    def __init__(self, bridge, auto=None):
        self.b = bridge
        self.auto = auto
        self.wait_s = 0.0
        self.replies = queue.Queue()
        self.closing = False

    def _wait(self, kind, fallback, **kw):
        if self.closing:
            return fallback
        if self.auto is not None:
            kw["auto"] = getattr(self.auto, kind)(*kw.pop("_args"))
        kw.pop("_args", None)
        t0 = time.time()
        self.b.request.emit(dict(kw, kind=kind))
        try:
            r = self.replies.get()
        finally:
            self.wait_s += time.time() - t0
        return fallback if r is None else r

    def ask(self, header, instruction, check, expect, unit, choices, notes):
        args = (header, instruction, check, expect, unit, choices, notes)
        return str(self._wait("ask", "stop", header=header, instruction=instruction, check=check, expect=expect,
                              unit=unit, choices=list(choices or []), notes=list(notes or []), _args=args)).strip()

    def act(self, header, title, action, n, total):
        return str(self._wait("act", "stop", header=header, title=title, action=action, n=n, total=total,
                              _args=(header, title, action, n, total))).strip()

    def confirm(self, text):
        return bool(self._wait("confirm", False, text=text, _args=(text,)))

    def choose(self, question, options):
        i = self._wait("choose", len(options) - 1, question=question, options=list(options), _args=(question, options))
        return int(i)

    def ask_text(self, prompt):
        return str(self._wait("ask_text", "", prompt=prompt, _args=(prompt,))).strip()

    def say(self, text):
        self.b.event.emit({"kind": "say", "text": text})

    def strip(self, t, judged, trigger, met):
        self.b.event.emit({"kind": "strip", "t": t, "judged": [dict(j) for j in judged],
                           "trigger": dict(trigger) if trigger else None, "met": met})

    def show_figures(self, figs, src):
        self.b.event.emit({"kind": "figures", "figs": [list(f) for f in figs], "src": src})


def run_session(a, cfg, human, bridge):
    """The worker thread: the same start and loop as `run`. Everything it prints goes to the window's log."""
    from .__main__ import open_case
    log = lambda t: bridge.event.emit({"kind": "log", "text": str(t)})      # noqa: E731
    out = {"case": None}
    try:
        case, tools, model, banner, store = open_case(a, cfg, human, out=log)
        out.update(case=case)
        bridge.event.emit({"kind": "started", "banner": banner, "case": case, "tools": tools})
        harness.run(case, tools, model, human, store, cfg, log=log)
        out.update(status=case.status, result=case.result)
    except SystemExit as e:
        out.update(status="not started", why=str(e.code or e))
    except Exception as e:     # shown on the finish screen, never a silent dead window
        out.update(status="error", why=f"{type(e).__name__}: {e}", trace=traceback.format_exc())
    bridge.finished.emit(out)


# ---------------------------------------------------------------------------- small widgets
def label(text="", colour=None, px=None, bold=False, mono=False, wrap=True):
    lb = QLabel(text)
    lb.setWordWrap(wrap)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    css = []
    if colour:
        css.append(f"color: {colour};")
    if px:
        css.append(f"font-size: {px}px;")
    if bold:
        css.append("font-weight: bold;")
    if mono:
        css.append(f"font-family: {T.MONO};")
    if css:
        lb.setStyleSheet(" ".join(css))
    return lb


def card(border=T.AIR_CORPS, bg=T.PANEL_2, name="card"):
    f = QFrame()
    f.setObjectName(name)
    f.setStyleSheet(f"QFrame#{name} {{ background-color: {bg}; border: 1px solid {border}; border-radius: 8px; }}")
    return f


def big_button(text, kind="", key=None, slot=None, tip=""):
    b = QPushButton(text)
    if kind:
        b.setObjectName(kind)
    b.setMinimumHeight(48)                     # big enough for a gloved hand or the trackball
    if tip or key:
        b.setToolTip((tip + " " if tip else "") + (f"({key})" if key else ""))
    if slot:
        b.clicked.connect(slot)
    return b


def cite_html(text, colour=T.TEXT):
    """Plain text with [M3]-style ids as purple citation links (tap to read the source)."""
    out, last = [], 0
    for m in ID_RX.finditer(text):
        out.append(html.escape(text[last:m.start()]))
        out.append(f"<a href='{m.group(1)}' style='color:{T.PURPLE_TEXT}; text-decoration:none;'>"
                   f"[{m.group(1)}]</a>")
        last = m.end()
    out.append(html.escape(text[last:]))
    return f"<span style='color:{colour}'>{''.join(out)}</span>"


class Sparkline(QWidget):
    """One watched value: name, big current value + unit, a line of recent samples over the shaded zone."""

    def __init__(self, key):
        super().__init__()
        self.key, self.vals, self.zone, self.unit, self.status = key, [], (None, None), "", ""
        self.setMinimumHeight(74)

    def add(self, j):
        self.vals = (self.vals + [float(j["value"])])[-120:]
        self.zone = (j.get("low"), j.get("high"))
        self.unit, self.status = j.get("unit") or "", j.get("status") or ""
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QColor(T.TEXT))
        p.setFont(QFont(self.font().family(), 11))
        p.drawText(QRectF(0, 0, w * 0.6, 22), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.key)
        v = self.vals[-1] if self.vals else None
        st = self.status.replace("_", " ") or "no zone"
        p.setPen(QColor(T.STATUS.get(self.status, T.GREY)))
        p.drawText(QRectF(0, 22, w * 0.6, 18), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, st)
        f = QFont("DejaVu Sans Mono", 20)
        p.setFont(f)
        p.setPen(QColor(T.AQUA))
        p.drawText(QRectF(w * 0.4, 0, w * 0.6, 34), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "--" if v is None else f"{v:g} {self.unit}")
        top, bot = 44, h - 4
        if len(self.vals) < 2:
            return
        lo, hi = min(self.vals), max(self.vals)
        zl, zh = self.zone
        for z in (zl, zh):
            if isinstance(z, (int, float)):
                lo, hi = min(lo, z), max(hi, z)
        span = (hi - lo) or 1.0
        y = lambda x: bot - (x - lo) / span * (bot - top)     # noqa: E731
        if isinstance(zl, (int, float)) and isinstance(zh, (int, float)):
            p.fillRect(QRectF(0, y(zh), w, y(zl) - y(zh)), QColor(T.PANEL))
        path = QPainterPath()
        n = len(self.vals)
        for i, x in enumerate(self.vals):
            pt = QPointF(i * (w - 1) / max(1, n - 1), y(x))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        p.setPen(QPen(QColor(T.AQUA), 2))
        p.drawPath(path)


class Zoom(QDialog):
    """A figure full-screen: scroll to pan, +/- or the wheel with Ctrl to zoom, Esc to close."""

    def __init__(self, parent, path, name):
        super().__init__(parent)
        self.setWindowTitle(name)
        self.pix = QPixmap(path)
        self.scale = 1.0
        lay = QVBoxLayout(self)
        self.lbl = QLabel()
        sc = QScrollArea()
        sc.setWidget(self.lbl)
        sc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sc, 1)
        row = QHBoxLayout()
        for t, f in (("−", 1 / 1.25), ("+", 1.25)):
            row.addWidget(big_button(t, slot=lambda _=False, f=f: self.zoom(f)))
        row.addStretch(1)
        row.addWidget(big_button("Close", slot=self.accept, key="Esc"))
        lay.addLayout(row)
        QShortcut(QKeySequence("+"), self, activated=lambda: self.zoom(1.25))
        QShortcut(QKeySequence("-"), self, activated=lambda: self.zoom(1 / 1.25))
        self.zoom(1.0)
        self.showMaximized()

    def zoom(self, f):
        self.scale = max(0.2, min(6.0, self.scale * f))
        if not self.pix.isNull():
            self.lbl.setPixmap(self.pix.scaledToWidth(int(self.pix.width() * self.scale),
                                                      Qt.TransformationMode.SmoothTransformation))


# ---------------------------------------------------------------------------- the window
class Window(QMainWindow):
    def __init__(self, a, cfg):
        super().__init__()
        self.a, self.cfg = a, cfg
        self.bridge = Bridge()
        self.bridge.request.connect(self.on_request)
        self.bridge.event.connect(self.on_event)
        self.bridge.finished.connect(self.on_finished)
        self.human = None
        self.worker = None
        self.case = None
        self.tools = None
        self.req = None
        self.busy_since = None
        self.sparks = {}
        self.figs = []
        self.speak = False
        self.setWindowTitle("diagent")
        if os.path.isfile(ICON):
            from PyQt6.QtGui import QIcon
            self.setWindowIcon(QIcon(ICON))
        self.setStyleSheet(T.qss(16))

        root = FitRoot()
        root.setObjectName("page")
        lay = QVBoxLayout(root)
        lay.setContentsMargins(12, 10, 12, 10)
        top = QHBoxLayout()
        self.lbl_car = label("diagent: no case open", T.TEXT, 20, bold=True)
        self.lbl_mode = label("", T.MUTED)
        self.lbl_mode.setObjectName("chip")
        self.lbl_mode.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_mode.setMinimumWidth(120)
        self.lbl_busy = label("", T.AMBER, mono=True, wrap=False)
        top.addWidget(self.lbl_car, 2)
        top.addWidget(self.lbl_busy)
        top.addWidget(self.lbl_mode, 1)
        lay.addLayout(top)
        tools_row = ReflowRow()
        self.b_panel = big_button("CASE PANEL", key="F3", slot=self.toggle_panel, tip="Path, hypotheses, log")
        self.b_panel.setCheckable(True)
        self.b_panel.setChecked(True)
        self.b_speak = big_button("SPEAK: OFF", key="F4", slot=self.toggle_speak,
                                  tip="Read each instruction and question aloud (the main app's voice settings)")
        self.b_speak.setCheckable(True)
        self.b_new = big_button("NEW CASE", slot=self.new_case)
        for b in (self.b_panel, self.b_speak, self.b_new):
            tools_row.add(b)
        lay.addWidget(tools_row)
        QShortcut(QKeySequence("F3"), self, activated=self.b_panel.click)
        QShortcut(QKeySequence("F4"), self, activated=self.b_speak.click)

        # body: work area + side panel, side by side in landscape, stacked in portrait
        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.stack = QStackedWidget()
        self.body.addWidget(self.stack, 3)
        self.side = self._side_panel()
        self.body.addWidget(self.side, 2)
        lay.addLayout(self.body, 1)

        self.start_page = self._start_page()
        self.work_page = self._work_page()
        self.finish_page = self._finish_page()
        for p in (self.start_page, self.work_page, self.finish_page):
            self.stack.addWidget(p)

        frame = QScrollArea()
        frame.setWidgetResizable(True)
        frame.setFrameShape(QFrame.Shape.NoFrame)
        frame.setWidget(root)
        self.setCentralWidget(frame)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(1280, avail.width()) if avail else 1280, min(820, avail.height()) if avail else 820)
        self._keys()
        self.show_start()

    # ------------------------------------------------------------------ layout
    def resizeEvent(self, e):
        portrait = e.size().width() < 1000 or e.size().height() > e.size().width()
        self.body.setDirection(QBoxLayout.Direction.TopToBottom if portrait else QBoxLayout.Direction.LeftToRight)
        self.body.setStretch(0, 3)
        self.body.setStretch(1, 2 if not portrait else 1)
        super().resizeEvent(e)

    def toggle_panel(self):
        self.side.setVisible(self.b_panel.isChecked())

    def toggle_speak(self):
        self.speak = self.b_speak.isChecked()
        self.b_speak.setText("SPEAK: ON" if self.speak else "SPEAK: OFF")

    def _say_aloud(self, text):
        if not self.speak or not text:
            return
        try:
            import voice
            vcfg = {}
            p = os.path.join(ROOT, "app", "config.json")
            if os.path.isfile(p):
                vcfg = json.load(open(p))
            voice.speak(vcfg, re.sub(r"\[[A-Z]{1,2}\d+\]", "", text))
        except Exception as e:      # no speech engine: say so once, keep working
            self.log(f"[voice] {e}")
            self.b_speak.setChecked(False)
            self.toggle_speak()

    # ------------------------------------------------------------------ start
    def _start_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        box = card()
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.f = {}
        for key, hint in (("vin", "17 characters, or empty: READ CAR / pickers"), ("make", ""), ("model", ""),
                          ("year", ""), ("engine", "optional"), ("codes", "comma-separated, if not read from the car"),
                          ("symptoms", "what it does, when (typed or dictated)")):
            e = QLineEdit()
            e.setPlaceholderText(hint)
            self.f[key] = e
            form.addRow(label(key.upper(), T.MUTED, wrap=False), e)
        self.f["vin"].setStyleSheet(f"font-family: {T.MONO};")
        self.f["codes"].setStyleSheet(f"font-family: {T.MONO};")
        self.cmb_car = QComboBox()
        self.cmb_car.addItems(["No car connected (typed codes or symptoms)", "Read the car (cable)",
                               "Simulated car from a scenario file..."])
        self.cmb_agent = QComboBox()
        self.cmb_agent.addItems(["auto: PC model if reachable, else plain manual walk", "pc: PC model only",
                                 "off: plain manual walk, no model"])
        form.addRow(label("CAR", T.MUTED, wrap=False), self.cmb_car)
        form.addRow(label("AGENT", T.MUTED, wrap=False), self.cmb_agent)
        lay.addWidget(box)
        lay.addWidget(big_button("START CASE", "primary", "Ctrl+Enter", self.start_from_form))
        self.lbl_start = label("", T.AMBER)
        lay.addWidget(self.lbl_start)

        lay.addWidget(label("SAVED CASES (pick one to resume)", T.MUTED, bold=True))
        self.lst_cases = QListWidget()
        self.lst_cases.itemActivated.connect(lambda it: self.resume(it.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.lst_cases, 1)
        row = ReflowRow()
        row.add(big_button("RESUME PICKED CASE", slot=lambda: self.lst_cases.currentItem() and self.resume(
            self.lst_cases.currentItem().data(Qt.ItemDataRole.UserRole))))
        self.cmb_play = QComboBox()
        self.cmb_play.setToolTip("A scripted test scenario, played with the answers filled in automatically")
        row.add(self.cmb_play)
        row.add(big_button("PLAY TEST SCENARIO", slot=lambda: self.play(self.cmb_play.currentText())))
        lay.addWidget(row)
        return page

    def _fill_start(self):
        for k in self.f:
            v = getattr(self.a, k, None)
            self.f[k].setText("" if v is None else str(v))
        self.lst_cases.clear()
        try:
            st = Store(self.cfg["case_db"])
            rows = st.db.execute("SELECT id, updated, status, label FROM case_ WHERE label NOT LIKE 'test:%' "
                                 "ORDER BY updated DESC LIMIT 50").fetchall()
        except Exception as e:
            rows = []
            self.lbl_start.setText(f"case list unreadable: {e}")
        for cid, upd, status, lab in rows:
            it = QListWidgetItem(f"#{cid}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(upd))}  {status:9}  {lab}")
            it.setData(Qt.ItemDataRole.UserRole, cid)
            self.lst_cases.addItem(it)
        self.cmb_play.clear()
        import glob
        self.cmb_play.addItems(sorted(os.path.basename(p)[:-5] for p in glob.glob(os.path.join(HERE, "scenarios", "*.json"))))

    def show_start(self):
        self._fill_start()
        self.lbl_mode.setVisible(bool(self.lbl_mode.text()))
        self.stack.setCurrentWidget(self.start_page)

    def start_from_form(self):
        a = argparse.Namespace(**{k: (self.f[k].text().strip() or None) for k in self.f}, resume=None, play=None,
                               install_icon=False)
        try:
            a.year = int(a.year) if a.year else None
        except ValueError:
            self.lbl_start.setText("Year must be a number.")
            return
        a.agent = ("auto", "pc", "off")[self.cmb_agent.currentIndex()]
        a.car = ("none", "real", None)[self.cmb_car.currentIndex()]
        if a.car is None:
            p, _ = QFileDialog.getOpenFileName(self, "Scenario file", os.path.join(HERE, "scenarios"), "*.json")
            if not p:
                return
            a.car = p
        if not (a.codes or a.symptoms or a.car != "none"):
            self.lbl_start.setText("Give codes or symptoms, or pick Read the car.")
            return
        self.begin(a)

    def resume(self, cid):
        a = argparse.Namespace(**vars(self.a))
        a.resume, a.play = int(cid), None
        a.car = "none" if a.car not in ("real",) else a.car
        self.begin(a)

    def play(self, name):
        if not name:
            return
        from .human import ScriptedHuman
        path = os.path.join(HERE, "scenarios", name + ".json")
        sc = json.load(open(path))
        given = sc.get("given", {})
        a = argparse.Namespace(vin=given.get("vin"), make=given.get("make"), model=given.get("model"),
                               year=given.get("year"), engine=given.get("engine"), codes=",".join(sc.get("codes", [])),
                               symptoms=sc.get("symptoms") or None, agent="off", car=path, resume=None, play=name,
                               install_icon=False)
        self.begin(a, auto=ScriptedHuman(sc))

    def begin(self, a, auto=None):
        self.lbl_start.setText("")
        self.feed.clear()
        self.txt_log.clear()
        self.sparks.clear()
        self._clear(self.live_lay)
        self.live.hide()
        self.figs = []
        self._show_figs()
        self.lbl_car.setText("Identifying the car...")
        self.lbl_mode.setText("PLAYING " + a.play if a.play else "")
        self.lbl_mode.setVisible(bool(a.play))
        self.human = GuiHuman(self.bridge, auto=auto)
        self.case = None
        self.busy_since = time.time()
        self.stack.setCurrentWidget(self.work_page)
        self._render({"kind": "wait"})
        self.worker = threading.Thread(target=run_session, args=(a, self.cfg, self.human, self.bridge), daemon=True)
        self.worker.start()

    def new_case(self):
        if self.worker is not None and self.worker.is_alive():
            self.reply("stop" if self.req and self.req["kind"] in ("ask", "act") else None)
            return
        self.show_start()

    # ------------------------------------------------------------------ work area
    def _work_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.prompt = card(T.AIR_CORPS, T.PANEL_2, "prompt")
        self.prompt_lay = QVBoxLayout(self.prompt)
        self.prompt_lay.setContentsMargins(16, 12, 16, 12)
        self.prompt_lay.setSpacing(10)
        lay.addWidget(self.prompt)
        self.reply_box = QWidget()
        self.reply_lay = QVBoxLayout(self.reply_box)
        self.reply_lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.reply_box)
        # live strip (watch_live and live reads)
        self.live = card(T.TEAL, T.BG, "live")
        lv = QVBoxLayout(self.live)
        lv.addWidget(label("LIVE", T.TEAL, 12, bold=True))
        self.live_lay = QVBoxLayout()
        lv.addLayout(self.live_lay)
        self.lbl_trigger = label("", T.TEAL)
        lv.addWidget(self.lbl_trigger)
        self.live.hide()
        lay.addWidget(self.live)
        # figures
        self.fig_box = card(T.AIR_CORPS, T.BG, "figs")
        fl = QVBoxLayout(self.fig_box)
        self.lbl_figsrc = label("", T.PURPLE_TEXT)
        fl.addWidget(self.lbl_figsrc)
        self.fig_row = ReflowRow()
        fl.addWidget(self.fig_row)
        self.fig_box.hide()
        lay.addWidget(self.fig_box)
        lay.addWidget(label("WHAT THE AGENT SAID", T.MUTED, 12, bold=True))
        self.feed = QTextBrowser()
        self.feed.setOpenLinks(False)
        self.feed.anchorClicked.connect(lambda u: self.show_source(u.toString()))
        self.feed.setMinimumHeight(90)
        lay.addWidget(self.feed, 1)
        return page

    def _clear(self, lay):
        while lay.count():
            it = lay.takeAt(0)
            if it.widget():
                w = it.widget()
                w.hide()               # gone now, not at the next event loop pass
                w.setParent(None)
                w.deleteLater()
            elif it.layout():
                self._clear(it.layout())

    def _header(self, header):
        """'Step 4 [M3]' -> a step label and a purple source chip."""
        row = QHBoxLayout()
        m = ID_RX.search(header or "")
        row.addWidget(label(ID_RX.sub("", header or "").strip().upper(), T.FOREST_TEXT, 13, bold=True))
        row.addStretch(1)
        if m:
            row.addWidget(self._chip(m.group(1)))
        return row

    def _chip(self, sid):
        b = QPushButton(f"source {sid}")
        b.setStyleSheet(T.chip(T.PURPLE) + " background: transparent; min-height: 18px; padding: 4px 12px;")
        b.setToolTip("Read the manual text this comes from")
        b.clicked.connect(lambda: self.show_source(sid))
        return b

    def _text(self, text, px=20, colour=T.TEXT):
        lb = QLabel(cite_html(text, colour))
        lb.setTextFormat(Qt.TextFormat.RichText)
        lb.setWordWrap(True)
        lb.setStyleSheet(f"font-size: {px}px;")
        lb.linkActivated.connect(self.show_source)
        lb.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse |
                                   Qt.TextInteractionFlag.TextSelectableByMouse)
        return lb

    def _commands(self, which=COMMANDS, primary=None):
        row = ReflowRow()
        keys = {"done": "D", "can't": "C", "skip": "S", "back": "B", "stop": "Esc"}
        for c in which:
            kind = "stop" if c == "stop" else ("primary" if c == primary else "")
            row.add(big_button({"can't": "Can't", "back": "◀ Back"}.get(c, c.capitalize()), kind, keys.get(c),
                               lambda _=False, c=c: self.reply(c)))
        return row

    def _render(self, r):
        """Show one request from the harness: the prompt card on top, the reply controls under it."""
        self.req = r if r.get("kind") != "wait" else None
        self._clear(self.prompt_lay)
        self._clear(self.reply_lay)
        k = r["kind"]
        self.prompt.setStyleSheet("QFrame#prompt { background-color: %s; border: 1px solid %s; border-radius: 8px; }"
                                  % (T.PANEL_2, T.AIR_CORPS))
        if k == "wait":
            self.prompt_lay.addWidget(label("The agent is working. Your turn comes up here.", T.MUTED, 18))
            return
        self.busy_since = None
        self.lbl_busy.setText("")
        spoken = ""
        if k == "ask":
            self.prompt_lay.addLayout(self._header(r["header"]))
            self.prompt_lay.addWidget(self._text(r["instruction"], 22))
            for n in r["notes"]:
                self.prompt_lay.addWidget(self._text("! " + n, 15, T.AMBER if not ID_RX.search(n) else T.PURPLE_TEXT))
            if r.get("check"):
                rule = QFrame()
                rule.setFrameShape(QFrame.Shape.HLine)
                rule.setStyleSheet(f"color: {T.AIR_CORPS};")
                self.prompt_lay.addWidget(rule)
                self.prompt_lay.addWidget(self._text(r["check"], 20))
            self._reply_controls(r)
            spoken = f"{r['instruction']}. {r.get('check') or ''}"
        elif k == "act":
            self.prompt_lay.addLayout(self._header(r["header"]))
            if r.get("title"):
                self.prompt_lay.addWidget(self._text(r["title"], 15, T.MUTED))
            self.prompt_lay.addWidget(label(f"ACTION {r['n']} OF {r['total']}", T.MUTED, 12, bold=True))
            self.prompt_lay.addWidget(self._text(r["action"], 22))
            self.reply_lay.addWidget(self._commands(primary="done"))
            spoken = r["action"]
        elif k == "confirm":
            detour = r["text"].startswith("Detour?")
            colour = T.PURPLE if detour else T.FOREST
            self.prompt.setStyleSheet("QFrame#prompt { background-color: %s; border: 1px solid %s; border-left: 6px "
                                      "solid %s; border-radius: 8px; }" % (T.PANEL, colour, colour))
            self.prompt_lay.addWidget(label("FIELD KNOWLEDGE DETOUR" if detour else "SAFETY CHECK",
                                            T.PURPLE_TEXT if detour else T.FOREST_TEXT, 13, bold=True))
            self.prompt_lay.addWidget(self._text(r["text"], 20))
            row = ReflowRow()
            row.add(big_button("Yes", "primary", "Y", lambda: self.reply(True)))
            row.add(big_button("Not now" if detour else "No", "", "N", lambda: self.reply(False)))
            self.reply_lay.addWidget(row)
            spoken = r["text"]
        elif k == "choose":
            self.prompt_lay.addWidget(self._text(r["question"], 20))
            grid = QVBoxLayout()
            for i, o in enumerate(r["options"]):
                b = big_button(f"{i + 1}.  {o}", key=str(i + 1) if i < 9 else None,
                               slot=lambda _=False, i=i: self.reply(i))
                b.setStyleSheet("text-align: left; padding-left: 14px;")
                grid.addWidget(b)
            self.reply_lay.addLayout(grid)
            spoken = r["question"]
        elif k == "ask_text":
            self.prompt_lay.addWidget(self._text(r["prompt"], 20))
            self._entry_row("Type here (Enter sends; empty = I can't tell)", allow_empty=True)
            spoken = r["prompt"]
        self._say_aloud(spoken)
        if "auto" in r:      # --play: show it, then answer as the scenario does
            QTimer.singleShot(1300, lambda r=r: self.req is r and self.reply(r["auto"]))

    def _entry_row(self, hint, allow_empty=False, unit=None):
        row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(hint)
        self.entry.setMinimumHeight(48)
        self.entry.setStyleSheet(f"font-family: {T.MONO}; font-size: 20px;")
        self.entry.returnPressed.connect(lambda: self._send_entry(allow_empty, unit))
        row.addWidget(self.entry, 1)
        if unit:
            row.addWidget(label(unit, T.AQUA, 20, mono=True, wrap=False))
        row.addWidget(big_button("Send", "primary", "Enter", lambda: self._send_entry(allow_empty, unit)))
        self.reply_lay.addLayout(row)
        self.entry.setFocus()

    def _send_entry(self, allow_empty, unit):
        t = self.entry.text().strip()
        if not t and not allow_empty:
            return
        if unit and t and re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?", t):
            t = f"{t} {unit}"            # the unit chip: units.py still checks it
        self.reply(t)

    def _reply_controls(self, r):
        e = r["expect"]
        if e == "yesno":
            row = ReflowRow()
            row.add(big_button("Yes", "primary", "Y", lambda: self.reply("yes")))
            row.add(big_button("No", "", "N", lambda: self.reply("no")))
            self.reply_lay.addWidget(row)
        elif e == "choice" and r["choices"]:
            row = ReflowRow()
            for c in r["choices"]:
                row.add(big_button(c, slot=lambda _=False, c=c: self.reply(c)))
            self.reply_lay.addWidget(row)
        elif e == "number":
            self._entry_row(f"reading{' in ' + r['unit'] if r.get('unit') else ' with its unit'}", unit=r.get("unit"))
            pad = QGridLayout()
            for i, t in enumerate("789456123-0."):
                b = big_button(t, slot=lambda _=False, t=t: self.entry.insert(t))
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                pad.addWidget(b, i // 3, i % 3)
            back = big_button("⌫", slot=lambda: self.entry.backspace())
            back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            pad.addWidget(back, 0, 3)
            wrap = QWidget()
            wrap.setLayout(pad)
            wrap.setMaximumWidth(420)
            self.reply_lay.addWidget(wrap)
        elif e not in ("done",):
            self._entry_row("What you see (Enter sends)")
        self.reply_lay.addWidget(self._commands(primary="done" if e == "done" else None))

    def reply(self, value):
        if self.human is None or self.req is None:
            return
        self.req_kind, self.req = self.req["kind"], None
        shown = {True: "yes", False: "no", "": "done"}.get(value, value)
        if self.req_kind == "choose" and isinstance(value, int):
            shown = f"picked {value + 1}"
        self.feed.append(f"<span style='color:{T.MUTED}'>You: {html.escape(str(shown))}</span>")
        self._render({"kind": "wait"})
        self.busy_since = time.time()
        self.human.replies.put(value)

    def _keys(self):
        def key(seq, fn):
            QShortcut(QKeySequence(seq), self, activated=fn)

        def cmd(c, kinds=("ask", "act")):
            def f():
                if self.req and self.req["kind"] in kinds and not self._typing():
                    self.reply(c)
            return f

        def yn(v):
            def f():
                if not self.req or self._typing():
                    return
                if self.req["kind"] == "confirm":
                    self.reply(v)
                elif self.req["kind"] == "ask" and self.req["expect"] == "yesno":
                    self.reply("yes" if v else "no")
            return f
        key("Y", yn(True))
        key("N", yn(False))
        key("D", cmd("done"))
        key("C", cmd("can't"))
        key("S", cmd("skip"))
        key("B", cmd("back"))
        key("Esc", cmd("stop"))
        key("Ctrl+Return", lambda: self.stack.currentWidget() is self.start_page and self.start_from_form())
        for i in range(9):
            key(str(i + 1), lambda i=i: self.req and self.req["kind"] == "choose" and not self._typing()
                and i < len(self.req["options"]) and self.reply(i))

    def _typing(self):
        w = QApplication.focusWidget()
        return isinstance(w, QLineEdit)

    # ------------------------------------------------------------------ events from the worker
    def on_request(self, r):
        self._render(r)

    def on_event(self, e):
        k = e["kind"]
        if k == "say":
            self.feed.append(cite_html("Agent: " + e["text"], T.TEXT))
            self._say_aloud(e["text"])
        elif k == "log":
            self.log(e["text"])
        elif k == "started":
            self.case, self.tools = e["case"], e["tools"]
            c = self.case.car
            self.lbl_car.setText(f"{c.get('year') or ''} {c.get('make') or ''} {c.get('model') or ''} "
                                 f"{c.get('engine') or ''}  ·  case {self.case.id}".strip())
            self.lbl_mode.setText(("PLAYING · " if self.human.auto else "") + e["banner"])
            self.lbl_mode.setVisible(True)
        elif k == "figures":
            self.figs = e["figs"]
            self.lbl_figsrc.setText(f"Figures from {e['src']} (tap to zoom)")
            self._show_figs()
        elif k == "strip":
            self.live.show()
            for j in e["judged"]:
                sp = self.sparks.get(j["key"])
                if sp is None:
                    sp = self.sparks[j["key"]] = Sparkline(j["key"])
                    self.live_lay.addWidget(sp)
                sp.add(j)
            t = e["trigger"]
            if t:
                self.lbl_trigger.setText("Got it." if e["met"] else
                                         f"Hold it: {t['key']} {t['op']} {t['value']:g}  ({e['t']:.0f} s)")
                self.lbl_trigger.setStyleSheet(f"color: {T.AQUA if e['met'] else T.TEAL}; font-size: 16px;")

    def _show_figs(self):
        for w in list(self.fig_row.widgets):
            self.fig_row.grid.removeWidget(w)
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self.fig_row.widgets, self.fig_row.cols = [], 0
        for name, path in self.figs[:8]:
            b = QPushButton()
            b.setMinimumHeight(48)
            if path and os.path.isfile(path):
                pm = QPixmap(path)
                if not pm.isNull():
                    from PyQt6.QtGui import QIcon
                    b.setIcon(QIcon(pm.scaledToHeight(120, Qt.TransformationMode.SmoothTransformation)))
                    b.setIconSize(pm.scaledToHeight(120).size())
                b.clicked.connect(lambda _=False, p=path, n=name: Zoom(self, p, n))
            else:
                b.setText(f"{name} (not on this machine)")
                b.setEnabled(False)
            b.setToolTip(name)
            self.fig_row.add(b)
        self.fig_box.setVisible(bool(self.figs))

    def on_finished(self, out):
        self.req = None
        self.busy_since = None
        self.lbl_busy.setText("")
        self.refresh_side()
        self._finish(out)
        self.stack.setCurrentWidget(self.finish_page)

    def tick(self):
        if self.busy_since is not None and self.worker is not None and self.worker.is_alive():
            self.lbl_busy.setText(f"agent working {time.time() - self.busy_since:.0f} s")
        self.refresh_side()

    def log(self, text):
        self.txt_log.appendPlainText(text)

    # ------------------------------------------------------------------ side panel
    def _side_panel(self):
        tabs = QTabWidget()
        tabs.setMinimumWidth(260)
        self.txt_path = QTextBrowser()
        self.txt_path.setOpenLinks(False)
        self.txt_path.anchorClicked.connect(lambda u: self.show_source(u.toString()))
        tabs.addTab(self.txt_path, "PATH")
        self.txt_board = QTextBrowser()
        self.txt_board.setOpenLinks(False)
        self.txt_board.anchorClicked.connect(lambda u: self.show_source(u.toString()))
        tabs.addTab(self.txt_board, "HYPOTHESES")
        logw = QWidget()
        ll = QVBoxLayout(logw)
        ll.setContentsMargins(0, 0, 0, 0)
        self.in_filter = QLineEdit()
        self.in_filter.setPlaceholderText("filter the log")
        self.in_filter.textChanged.connect(self._filter_log)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("font-size: 12px;")
        self.txt_log.setMaximumBlockCount(5000)
        ll.addWidget(self.in_filter)
        ll.addWidget(self.txt_log, 1)
        tabs.addTab(logw, "LOG")
        self.txt_source = QTextBrowser()
        tabs.addTab(self.txt_source, "SOURCE")
        self.side_tabs = tabs
        self._side_key = None
        return tabs

    def _filter_log(self, t):
        doc = self.txt_log.document()
        for i in range(doc.blockCount()):
            b = doc.findBlockByNumber(i)
            b.setVisible(not t or t.lower() in b.text().lower())
        self.txt_log.viewport().update()

    def refresh_side(self):
        c = self.case
        if c is None:
            return
        try:   # the worker may be changing the case right now; a missed refresh is fine, the next tick catches up
            p = dict(c.proc or {})
            path = list(p.get("path", []))
            hyps = [dict(h) for h in c.hypotheses]
            key = (len(path), p.get("current"), len(hyps), json.dumps(hyps, default=str), p.get("ended"))
        except RuntimeError:
            return
        if key == self._side_key:
            return
        self._side_key = key
        rows = []
        if p.get("code"):
            rows.append(f"<b>{html.escape(str(p.get('code')))}</b> "
                        f"<span style='color:{T.MUTED}'>{html.escape(str(p.get('variant') or p.get('mode') or ''))}</span>")
        chain = " → ".join(f"<span style='font-family:monospace'>{e['step']} {html.escape(str(e['answer']))}</span>"
                           for e in path)
        if p.get("current") and not p.get("ended"):
            chain += f" → <span style='color:{T.FOREST_TEXT}'>{p['current']}</span>"
        rows.append(chain or f"<span style='color:{T.MUTED}'>no steps walked yet</span>")
        for e in path:
            rows.append(f"<p><span style='color:{T.MUTED}'>Step {e['step']}: {html.escape(str(e['answer']))} "
                        f"({html.escape(str(e['how']))})</span>" + "".join(
                            f" <a href='{r}' style='color:{T.PURPLE_TEXT}'>[{r}]</a>" for r in e.get("readings", [])))
        if p.get("ended"):
            rows.append(f"<p style='color:{T.OK}'>Tree ended: {html.escape(str(p.get('end_text') or ''))}</p>")
        self.txt_path.setHtml("<br>".join(rows))
        colours = {"supported": T.OK, "confirmed": T.OK, "ruled_out": T.GREY, "open": T.AMBER}
        hs = []
        for h in hyps:
            col = colours.get(h.get("status"), T.MUTED)
            ev = " ".join(f"<a href='{x}' style='color:{T.PURPLE_TEXT}'>[{x}]</a>" for x in h.get("evidence", []))
            hs.append(f"<p><span style='color:{col}; border:1px solid {col}'>&nbsp;{html.escape(str(h.get('status')))}"
                      f"&nbsp;</span> {html.escape(str(h.get('cause')))}"
                      + (f" <i style='color:{T.MUTED}'>(general knowledge)</i>" if h.get("general") else "")
                      + f"<br>{ev}</p>")
        self.txt_board.setHtml("".join(hs) or f"<span style='color:{T.MUTED}'>No hypotheses yet.</span>")

    def show_source(self, sid):
        """A citation tapped: the manual text behind it, in the side panel's SOURCE tab."""
        c = self.case
        s = (c.sources.get(sid) if c else None) or {}
        r = next((x for x in (c.readings if c else []) if x.get("id") == sid), None)
        if r:
            body = (f"<b>Reading {sid}</b><br><span style='font-family:monospace; color:{T.AQUA}'>"
                    f"{html.escape(str(r.get('value')))} {html.escape(str(r.get('unit') or ''))}</span><br>"
                    f"step {r.get('step')}, {html.escape(str(r.get('how')))}<br>raw: {html.escape(str(r.get('raw')))}")
        elif s:
            body = (f"<b style='color:{T.PURPLE_TEXT}'>[{sid}] {html.escape(str(s.get('label')))}</b>"
                    + (f"<br><span style='color:{T.MUTED}'>page {html.escape(str(s['page']))}</span>" if s.get("page") else "")
                    + f"<pre style='white-space:pre-wrap'>{html.escape(str(s.get('text') or '(no text kept)'))}</pre>")
        else:
            body = f"<span style='color:{T.MUTED}'>{html.escape(sid)}: not in this case's sources.</span>"
        self.txt_source.setHtml(body)
        self.side.setVisible(True)
        self.b_panel.setChecked(True)
        self.side_tabs.setCurrentWidget(self.txt_source)

    # ------------------------------------------------------------------ finish
    def _finish_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        self.fin = QTextBrowser()
        self.fin.setOpenLinks(False)
        self.fin.anchorClicked.connect(lambda u: self.show_source(u.toString()))
        self.fin.setStyleSheet("font-size: 16px;")
        lay.addWidget(self.fin, 1)
        row = ReflowRow()
        row.add(big_button("SAVE WORK ORDER (HTML)", "primary", slot=self.save_order))
        row.add(big_button("NEW CASE", slot=self.show_start))
        lay.addWidget(row)
        self.lbl_saved = label("", T.MUTED)
        lay.addWidget(self.lbl_saved)
        return page

    def _finish(self, out):
        c = self.case
        res = out.get("result") or (c.result if c else {}) or {}
        status = out.get("status", "?")
        word = {"diagnosis": ("DIAGNOSIS", T.OK), "unconfirmed": ("NOT CONFIRMED", T.AMBER)}.get(
            res.get("result"), (str(status).upper(), T.AMBER if status != "error" else T.RED))
        parts = [f"<h2 style='color:{word[1]}'>{html.escape(word[0])}</h2>"]
        if out.get("why"):
            parts.append(f"<p style='color:{T.AMBER}'>{html.escape(out['why'])}</p>")
        if res.get("summary"):
            parts.append(f"<p>{cite_html(res['summary'])}</p>")
        if res.get("settle_test"):
            parts.append(f"<p><b>Still needed:</b> {cite_html(res['settle_test'])}</p>")
        if c is not None:
            path = (c.proc or {}).get("path", [])
            if path:
                parts.append("<p><b>Path:</b> <span style='font-family:monospace'>"
                             + html.escape(" → ".join(f"{e['step']} {e['answer']}" for e in path)) + "</span></p>")
            if c.readings:
                parts.append("<p><b>Readings:</b><br>" + "<br>".join(
                    f"<span style='font-family:monospace; color:{T.AQUA}'>{r['id']}: {html.escape(str(r['value']))} "
                    f"{html.escape(str(r.get('unit') or ''))}</span> (step {r.get('step')})" for r in c.readings) + "</p>")
            cites = res.get("citations") or []
            if cites:
                parts.append("<p><b>Sources:</b> " + " ".join(
                    f"<a href='{x}' style='color:{T.PURPLE_TEXT}'>[{x}] "
                    f"{html.escape(str((c.sources.get(x) or {}).get('label', '')))}</a><br>" for x in cites) + "</p>")
            parts.append(f"<p style='color:{T.MUTED}'>Case {c.id} is saved. Terminal: python -m diagent show {c.id}</p>")
        if out.get("trace"):
            parts.append(f"<pre style='color:{T.RED}'>{html.escape(out['trace'])}</pre>")
            self.log(out["trace"])
        self.fin.setHtml("".join(parts))
        self.lbl_saved.setText("")

    def save_order(self):
        if self.case is None:
            return
        d = os.path.join(ROOT, "logs", "diagent")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"work_order_case{self.case.id}.html")
        body = self.fin.toHtml()
        with open(path, "w") as f:
            f.write(body.replace("<body", f"<body style='background:{T.BG}; color:{T.TEXT}; font-family:sans-serif'", 1))
        self.lbl_saved.setText(f"Saved {path}")
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def closeEvent(self, e):
        if self.human is not None:      # anything still waiting gets "stop"; the case is saved after every call
            self.human.closing = True
            self.human.replies.put(None)
        super().closeEvent(e)


# ---------------------------------------------------------------------------- launcher
def install_icon():
    """Add diagent to the app list (KDE on the PC, the deck's Ubuntu menu). It never autostarts."""
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "diagent.desktop")
    run = f"sh -c 'cd \"{ROOT}\" && exec \"{sys.executable}\" -m diagent gui'"
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Diagent\nGenericName=Car diagnosis\n"
                "Comment=Manual-led diagnostic agent: walks the factory manual's steps with you\n"
                f"Exec={run}\nIcon={ICON}\nTerminal=false\nCategories=Utility;\nStartupWMClass=diagent\n")
    os.chmod(path, 0o755)
    print(f"added {path}")
    return 0


def main(a, cfg):
    if a.install_icon:
        return install_icon()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("diagent")
    app.setDesktopFileName("diagent")      # the panel shows the app list icon for this window
    w = Window(a, cfg)
    w.showMaximized() if not os.environ.get("DIAGENT_NO_MAX") else w.show()
    if a.play:
        w.play(a.play)
    elif a.resume:
        w.resume(a.resume)
    elif a.codes or a.symptoms or a.car != "none":
        w.begin(a)
    return app.exec()
