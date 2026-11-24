#!/usr/bin/env python3
"""
Cyberdeck diagnostic GUI (one knowledge DB per make): two local models, no agents.

  FAST model  (default Qwen3-1.7B)              -> 2. Chat / brainstorm
  DEEP model  (default Qwen3-4B-Instruct-2507)  -> 3. Final verdict, 4. Repair SOP

Answers are grounded in the make's knowledge DB, kb/<make>.db (factory manual sections, DTC procedures, specs,
TSBs), through kb/kb_retriever.py. Torque specs are looked up, never generated. A Subaru deck without
kb/subaru.db falls back to the legacy retriever (app/retrieval.py: subaru_diag.db + test_subaru_knowledge.db).

Backend: RKLLM on the Orange Pi (aarch64 + librkllmrt.so present), Ollama on the desktop.
Settings: app/config.json (optional; only the keys you want to change. The mic and cable picks are saved there).
"""
import datetime
import faulthandler
import glob
import hashlib
import html
import json
import os
import platform
import queue
import re
import shlex
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error

from PyQt6.QtCore import QObject, QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut, QTextCharFormat, QTextCursor,
                         QTextDocument)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                             QTabWidget,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
                             QPlainTextEdit,
                             QPushButton, QScrollArea,
                             QStackedWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssm"))
from llm_backend import (ClaudeModel, CliModel, OllamaModel, OpenAICompatModel, RKLLMModel, mem_available_gb, mem_total_gb, ollama_models,  # noqa: E402
                         probe_rkllm)
from retrieval import Retriever as LegacyRetriever  # noqa: E402
from vehicle_id import MAKES, car_from_rom, decode_vin, kb_path_for, make_from_ssm, nhtsa_decode  # noqa: E402
import car_records
import charm_pics
import common_issues
import drive_file
import live_data
import maintenance
import procedures
import sanity_check
import answer_check
import verdict_steps
import obd_all
import telemetry_ref
import voice  # noqa: E402
import theme as T
from ui_fit import FitRoot, ReflowRow  # noqa: E402
from ui.config import (
    CFG, CONFIG_MIGRATIONS, CONFIG_PATH, CONFIG_VERSION, DEFAULT_CONFIG, PS, PSQR, PSSHEET, PS_DIR, ROOT,
    _CONFIG_LOCK, _forget_old_home_defaults, _write_config, is_part_number, load_config, migrate_config,
    save_config_key)
from ui.prompts import CHAT_STYLE, DEEP_SYSTEM, FAST_SYSTEM
from ui.context import (
    CASE_MAX_CHARS, CHART_READS_MAX, DRIVE_KEY, FIG_W, GENERIC, JOB_VERBS, MODELS, NullRetriever,
    OPINION_FIELDS, PAGE, REPAIRS, SCREENS, SHOP_WORDS, STOPWORDS, TRANS, VERDICT_KEYWORDS, _fit, case_context,
    case_make, cited_pages, community_block, confirmed_fixes, ecu_vins, find_embed_model, focus_specs, fsm_hits,
    kb_status, load_figure, log_caught, make_retriever, part_from_job, parts_from_job, remember_ecu_vin,
    repair_from_verdict, spaced, squeeze, use_make)
from ui.session import Session
from ui.models import ModelManager
from ui.workers import (
    CaseContextWorker, ChartWorker, DriveWorker, GenWorker, OBDWorker, PicWorker, PreloadWorker, TestWorker,
    VinWorker, WarmWorker, nhtsa_cache_path)
from ui.widgets import BUSY, Bubbles, ERR, ErrorBus, Field, IDLE, PopupCombo, STYLE, vehicle_table
from ui.screens.voice import VoiceScreen
from ui.screens.intake import IntakeScreen
from ui.screens.records import RecordsScreen
from ui.screens.chat import ChatScreen
from ui.screens.maintenance import MaintenanceScreen
from ui.screens.verdict import VerdictScreen
from ui.screens.parts import RepairPartsScreen
from ui.screens.two_way import TwoWayTestsScreen


class CyberdeckApp(VoiceScreen, IntakeScreen, RecordsScreen, ChatScreen, MaintenanceScreen, VerdictScreen, RepairPartsScreen, TwoWayTestsScreen, QMainWindow):
    def __init__(self, bus=None):
        super().__init__()
        self.session = Session(CFG["session_db"])
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        if v.get("model"):
            CFG["kb_model"], CFG["kb_year"] = v["model"], int(v["year"]) if str(v.get("year", "")).isdigit() else None
        v["make"] = case_make(v)
        self.kb_msg = use_make(v["make"])
        try:
            self.retriever = make_retriever(CFG)
            self._warm_retriever()
        except Exception as e:
            self.retriever = NullRetriever()
            self.kb_msg = f"manual database failed to open: {e}"
        self.mgr = ModelManager(CFG)
        self.preload = None
        if self.mgr.backend == "rkllm":
            self.preload = PreloadWorker(self.mgr)
            self.preload.start()
        DRIVE_KEY["key"] = lambda: "drive_log" if self.mgr.home and not self.mgr.pc_why else "drive_log_short"
        self.worker = None
        self.obd_worker = None
        self.test_worker = None
        self.chat_started = False
        self.chat_hist = []
        self.last_case = self.session.case_summary()
        self.last_debate = ""
        self.dictate_target = None
        self.dictate_widget = None
        self.last_edit = None
        self.voice = None

        self.setWindowTitle("CYBERDECK HOME // PC MODELS" if CFG.get("home") else "CYBERDECK // DIAGNOSTIC OS")
        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(1180, avail.width()) if avail else 1180, min(720, avail.height()) if avail else 720)
        self.setStyleSheet(STYLE)

        root = FitRoot()
        lay = QVBoxLayout(root)
        top = QHBoxLayout()
        self.lbl_title = title = QLabel(self._title_text())
        title.setStyleSheet(f"color: {T.FOREST_TEXT}; font-weight: bold;")
        self.lbl_llm = QLabel("")
        self._llm_idle()
        for lbl in (title, self.lbl_llm):
            lbl.setWordWrap(True)
            lbl.setMinimumWidth(120)
        self.lbl_llm.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_llm.setObjectName("chip")
        top.addWidget(title, 1)
        top.addWidget(self.lbl_llm, 1)
        if self.mgr.home:
            self.b_deck_only = QPushButton("DECK ONLY")
            self.b_deck_only.setCheckable(True)
            self.b_deck_only.setChecked(self.mgr.deck_only)
            self.b_deck_only.setToolTip("On: don't look for the home PC, answer with the deck's own models. Remembered.")
            self.b_deck_only.toggled.connect(self.act_deck_only)
            top.addWidget(self.b_deck_only)
        lay.addLayout(top)

        nav = ReflowRow()
        self.nav = [QPushButton(label) for _, label in SCREENS]
        for i, b in enumerate(self.nav):
            b.setCheckable(True)
            b.clicked.connect(lambda _, i=i: self.switch(i))
            nav.add(b)
        lay.addWidget(nav)

        self.stack = QStackedWidget()
        for name, _ in SCREENS:
            getattr(self, f"_{name}_view")()
            assert self.stack.count() == PAGE[name] + 1, f"_{name}_view must add exactly one page"
        lay.addWidget(self.stack, 1)

        bar = QHBoxLayout()
        self.b_mic = QPushButton("🎤 MIC")
        self.b_mic.setToolTip("Or press F2, or say 'hey deck'. Talk, then stay quiet for 2 s. The words go "
                              "into the box the cursor is in (Chat and Parts then run it); with no box picked: "
                              "Intake notes, Chat question, Repair job, Parts search.")
        self.b_mic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        QApplication.instance().focusChanged.connect(self._track_focus)
        QShortcut(QKeySequence("F2"), self, activated=self.act_mic)
        self.b_mic.clicked.connect(self.act_mic)
        self.cmb_mic = PopupCombo(lambda: self._fill_mics(refresh=True))
        self.cmb_mic.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_mic.setMinimumContentsLength(12)
        self._fill_mics()
        self.cmb_mic.currentIndexChanged.connect(self.act_mic_changed)
        b_test = QPushButton("🔊 TEST")
        b_test.setToolTip("Say a test phrase through the speaker/headphones")
        b_test.clicked.connect(self.act_speaker_test)
        self.lbl_voice = QLabel("")
        self.lbl_voice.setStyleSheet(IDLE)
        self.b_stop = QPushButton("■ STOP")
        self.b_stop.setToolTip("Stop the answer being written")
        self.b_stop.clicked.connect(self.act_stop)
        for wdg in (self.b_mic, self.cmb_mic, b_test):
            bar.addWidget(wdg)
        self.lbl_voice.setWordWrap(True)
        self.lbl_voice.setMinimumWidth(100)
        bar.addWidget(self.lbl_voice, 1)
        bar.addWidget(self.b_stop)
        lay.addLayout(bar)

        root.setObjectName("page")
        frame = QScrollArea()
        frame.setWidgetResizable(True)
        frame.setFrameShape(QScrollArea.Shape.NoFrame)
        frame.setWidget(root)
        self.setCentralWidget(frame)
        self.switch(PAGE["intake"])
        if bus is not None:
            bus.error.connect(self.on_error)
        self._start_voice()

    def on_error(self, msg):
        self.lbl_llm.setText(f"error: {msg[:120]}")
        self.lbl_llm.setStyleSheet(ERR)
        out = getattr(self, f"txt_{SCREENS[self.stack.currentIndex()][0]}")
        self._say(out, f"[error] {msg}", T.RED)

    def act_deck_only(self, on):
        if self.worker is not None and self.worker.isRunning():
            self.b_deck_only.blockSignals(True)
            self.b_deck_only.setChecked(not on)
            self.b_deck_only.blockSignals(False)
            self.lbl_llm.setText("busy: wait for the current answer, or press STOP")
            self.lbl_llm.setStyleSheet(BUSY)
            return
        self.mgr.set_deck_only(on)
        save_config_key("deck_only", bool(on))
        if on and self.mgr.backend == "rkllm" and "fast" not in self.mgr.models:
            self.preload = PreloadWorker(self.mgr)
            self.preload.start()
        self._llm_idle()

    def _llm_idle(self):
        self.lbl_llm.setText(f"[{self.mgr.backend}] fast: {self.mgr.label('fast')} | deep: {self.mgr.label('deep')} (idle)")
        self.lbl_llm.setStyleSheet(IDLE)

    def _say(self, out, text, color=None, who=None, raw=False):
        """Append a line; who= adds a coloured 'NAME >' label like the old app. raw=True: text is already HTML."""
        body = text if raw else html.escape(text).replace("\n", "<br>")
        if who:
            body = f"<span style='color:{color or T.FOREST_TEXT};font-weight:bold'>{who} &gt;</span> {body}"
        elif color:
            body = f"<span style='color:{color}'>{body}</span>"
        out.append(body)
        out.moveCursor(QTextCursor.MoveOperation.End)

    def _fig_html(self, out, path):
        """<img> for one figure file, '' when it's missing or unreadable. A CHARM picture name means its
        cached copy (see _charm_figs for fetching it)."""
        if charm_pics.is_charm(path):
            path = charm_pics.cache_path(CFG["cache_dir"], path)
        if not os.path.isfile(path):
            return ""
        url = QUrl.fromLocalFile(path)
        doc = out.document()
        if doc.resource(QTextDocument.ResourceType.ImageResource, url) is None:
            pm = load_figure(path)
            if pm.isNull():
                return ""
            doc.addResource(QTextDocument.ResourceType.ImageResource, url, pm)
        return f'<br><img src="{url.toString()}"><br>'

    def _charm_figs(self, out, label, names):
        """A CHARM page's pictures: the cached ones now, the rest fetched from the PC and drawn when they
        arrive (with the page's tag, since other text may have been added below by then)."""
        names = names[:int(CFG.get("charm_pics_max") or 8)]
        now = "".join(self._fig_html(out, n) for n in names)
        missing = [n for n in names if not os.path.isfile(charm_pics.cache_path(CFG["cache_dir"], n))]
        if missing:
            w = PicWorker(missing)
            self._pic_workers = [x for x in getattr(self, "_pic_workers", []) if x.isRunning()] + [w]

            def arrived(have, why, out=out, label=label, n=len(missing)):
                gray = f"<span style='color:{T.MUTED}'>{{}}</span>"
                imgs = "".join(self._fig_html(out, p) for p in have)
                if imgs:
                    out.append(gray.format("Pictures for " + html.escape(label)) + imgs)
                if len(have) < n:
                    k = n - len(have)
                    out.append(gray.format(f"{k} CHARM picture{'s' * (k > 1)} for {html.escape(label)} not shown"
                                           f" ({html.escape(why or 'unreadable')})" + (
                                               ". They show once the PC is reachable." if why == "PC not reachable"
                                               else ".")))
                out.moveCursor(QTextCursor.MoveOperation.End)
            w.done.connect(arrived)
            w.start()
        return now

    def _say_hits(self, out, hits, color=None, sep="<br><br>"):
        """Manual sections with their figures drawn where the text says [fig]. CHARM pages have no [fig]
        marks: their pictures follow the text."""
        blocks, later = [], []
        for text, figs in hits:
            charm = [f for f in figs if charm_pics.is_charm(f)]
            figs = [f for f in figs if not charm_pics.is_charm(f)]
            parts = text.split("[fig]")
            body = html.escape(parts[0])
            for i, p in enumerate(parts[1:]):
                body += (self._fig_html(out, figs[i]) if i < len(figs) else "") + html.escape(p)
            body = body.replace("\n", "<br>")
            if charm:
                later.append((text.split("\n", 1)[0], charm))
            blocks.append(body)
        body = sep.join(blocks)
        if color:
            body = f"<span style='color:{color}'>{body}</span>"
        out.append(body)
        for label, charm in later:
            imgs = self._charm_figs(out, label, charm)
            if imgs:
                out.append(imgs)
        out.moveCursor(QTextCursor.MoveOperation.End)

    def _say_figs(self, out, figs, limit=6, label="this page"):
        """A section's figures on their own (the SOP sources)."""
        charm = [f for f in figs if charm_pics.is_charm(f)]
        imgs = [h for h in (self._fig_html(out, f) for f in figs[:limit] if not charm_pics.is_charm(f)) if h]
        if charm:
            imgs.append(self._charm_figs(out, label, charm))
        imgs = [h for h in imgs if h]
        if imgs:
            out.append("".join(imgs))
            out.moveCursor(QTextCursor.MoveOperation.End)

    def switch(self, i):
        if self.stack.currentIndex() == PAGE["intake"] and i != PAGE["intake"]:
            self._store_case()
        if self.stack.currentIndex() == PAGE["tests"] and i != PAGE["tests"] and self.test_worker is not None \
                and self.lbl_testmode.text().startswith("RUNNING"):
            self.test_worker.emergency_stop()
        if i == PAGE["verdict"] and hasattr(self, "txt_notes"):
            self.txt_notes.setPlainText(self.session.get("chat_notes"))
            verdict = self.session.get("final_verdict")
            if verdict and self.tabs_verdict.count() == 1 and not self.txt_verdict.toPlainText().strip():
                self._say(self.txt_verdict, verdict)
                self._build_verdict_tabs(verdict)
        if i == PAGE["maint"]:
            self._maint_fill()
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.nav):
            b.setChecked(i == j)

    def _warm_retriever(self):
        """Load the embedding model and this car's vectors on a thread right after the car is picked. The first
        vector search did that on whatever called it: 11 s on the PC for chevrolet.db, far longer on the deck,
        and /fsm, maintenance and the repair guide still search on the UI thread."""
        r = self.retriever
        if not hasattr(r, "search_ids"):
            return
        w = WarmWorker(r)
        self.warmers = [x for x in getattr(self, "warmers", []) if x.isRunning()] + [w]
        w.start()

    def _lookup(self, text, budget, then, **kw):
        """case_context off the UI thread, then then(ctx) back on it. The first lookup on a big make loads the
        embedding model and the car's vectors: seconds on the PC, a minute or more on the deck, and the window
        looked crashed while it waited (11-02)."""
        w = CaseContextWorker(self.retriever, text, budget, **kw)
        self.lookups = [x for x in getattr(self, "lookups", []) if x.isRunning()] + [w]
        self.lbl_llm.setText("LOOKING UP THE MANUAL...")
        self.lbl_llm.setStyleSheet(BUSY)
        w.result.connect(then)
        w.start()

    def _busy(self):
        if any(w.isRunning() for w in getattr(self, "lookups", [])):
            self.lbl_llm.setText("busy: looking up the manual for the last request")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        if self.test_worker is not None and self.lbl_testmode.text().startswith("RUNNING"):
            self.lbl_llm.setText("a test is running: STOP it first (TESTS screen)")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        if self.worker is not None and self.worker.isRunning():
            self.lbl_llm.setText("busy: wait for the current answer, or press STOP")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        return False

    def answer_room(self, prompt):
        """Tokens the answer may use: whatever the model's context has left after the prompt. Answers end where
        the model ends them (the prompt says how long); this only stops one that would overflow the context.
        11-01: fixed caps per mode (700 for wildcard, 300 for chat...) cut answers off mid-sentence."""
        on_pc = bool(getattr(self.mgr, "home", False)) and not getattr(self.mgr, "pc_why", None)
        ctx = CFG["home_context_len"] if on_pc else CFG["max_context_len"]
        return max(256, ctx - int(len(prompt) / 3.0) - 128)

    def _run(self, role, prompt, out: QTextEdit, on_done, keep_history=False, temperature=None):
        """Stream one model answer into out. Its length is set by answer_room(): the prompt says how long to be."""
        self.worker = GenWorker(self.mgr, role, prompt, keep_history, self.answer_room(prompt), temperature)
        plain = QTextCharFormat()
        plain.setForeground(QColor(T.TEXT))

        def tok(t):
            c = out.textCursor()
            c.movePosition(QTextCursor.MoveOperation.End)
            c.insertText(t, plain)
            out.setTextCursor(c)
            out.ensureCursorVisible()

        def status(s):
            self.lbl_llm.setText(s.upper())
            self.lbl_llm.setStyleSheet(BUSY)

        def done(text):
            self._llm_idle()
            if "[LLM error" in text:
                self.lbl_llm.setText("model error: see the answer box")
                self.lbl_llm.setStyleSheet(ERR)
            on_done(text)

        self.worker.token.connect(tok)
        self.worker.status.connect(status)
        self.worker.done.connect(done)
        self.worker.start()

    def act_stop(self):
        if self.worker is not None and self.worker.isRunning():
            model = self.mgr.models.get(self.worker.role)
            if model:
                model.abort()

    def closeEvent(self, e):
        if self.drive_worker is not None:
            self.drive_worker.stop_flag.set()
            if not self.drive_worker.wait(30000):
                self._say(self.txt_intake, "[!] Still finishing a reading; close again in a moment.", T.AMBER)
                e.ignore()
                return
        if self.tool_proc is not None:
            self._say(self.txt_intake, "[!] Close the other program first; it's still using the cable.", T.AMBER)
            e.ignore()
            return
        if self.test_worker is not None:
            self.test_worker.do("quit")
            if not self.test_worker.wait(20000):
                self._say(self.txt_tests, "[!] Still finishing a step on the cable; close again in a moment.",
                          T.AMBER)
                e.ignore()
                return
        self._stop_voice()
        self.act_stop()
        if self.worker is not None:
            self.worker.wait(5000)
        if getattr(self, "chart_worker", None) is not None:
            self.chart_worker.stop_flag.set()
            self.chart_worker.wait(10000)
        if self.obd_worker is not None:
            self.obd_worker.wait(3000)
        for w in [getattr(self, "case_worker", None), getattr(self, "preload", None)] + getattr(self, "lookups", []) + \
                getattr(self, "warmers", []):
            if w is not None:
                w.wait(60000)
        self.mgr.release_all()
        super().closeEvent(e)


def install_home_icon():
    """Add "Cyberdeck Home" to the app list: this app with the home PC's models (the deck's own as fallback)."""
    here = os.path.dirname(os.path.abspath(__file__))
    launcher = f"{ROOT}/start_deck.sh"
    run = (f"env CYBERDECK_HOME=1 {launcher}" if os.access(launcher, os.X_OK) else
           f"env CYBERDECK_HOME=1 sh -c 'cd \"{here}\" && exec \"{sys.executable}\" cyberdeck_gui.py'")
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "cyberdeck-home.desktop")
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Cyberdeck Home\n"
                "Comment=The cyberdeck app with the big models on the home PC (over Tailscale)\n"
                f"Exec={run}\nIcon={here}/icon_home.svg\nTerminal=false\nCategories=Utility;\n")
    os.chmod(path, 0o755)
    print(f"added {path}")


def main():
    os.makedirs(f"{ROOT}/logs", exist_ok=True)
    crash_log = open(f"{ROOT}/logs/app_crash.log", "a")
    crash_log.write(f"\n=== app started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    crash_log.flush()
    faulthandler.enable(file=crash_log, all_threads=True)
    if "--home" in sys.argv[1:] or os.environ.get("CYBERDECK_HOME") == "1":
        CFG["home"] = True
    if "--install-home-icon" in sys.argv[1:]:
        install_home_icon()
        return
    app = QApplication(sys.argv)
    bus = ErrorBus()

    def hook(etype, value, tb):
        traceback.print_exception(etype, value, tb)
        bus.error.emit(f"{etype.__name__}: {value}")

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    win = CyberdeckApp(bus)
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
