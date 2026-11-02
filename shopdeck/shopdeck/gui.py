"""Desktop window for shopdeck (PyQt6). Same screens and rules as the keyboard version in tui.py: this file only
replaces how a list, a page of text and a question are shown, so the diagnosis logic exists once.

Keyboard: arrows, PgUp/PgDn, Enter picks, Esc goes back, the letter keys shown on the buttons work as in the
terminal version, F1-F8 open the eight screens from anywhere.
Trackball: click picks a line or presses a button, the wheel scrolls, right click goes back.
"""
import html
import os
import re
import sys
import traceback

from PyQt6.QtCore import QEvent, QEventLoop, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QPushButton, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from . import tui

ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.svg")

# line attributes the screens use (tui.UI passes curses attributes; here they're plain flags)
BOLD, HEAD, GOOD, WARN, BAD, ACC, DIM, SEL = 1, 2, 4, 8, 16, 32, 64, 128
COLORS = [(WARN, "#e3b341"), (BAD, "#f85149"), (GOOD, "#3fb950"), (ACC, "#79c0ff"), (DIM, "#8b949e")]
TEXT = "#e6edf3"

STYLE = """
QMainWindow, QWidget { background: #0d1117; color: #e6edf3; }
QPushButton { background: #21262d; color: #e6edf3; border: 1px solid #30363d; border-radius: 6px;
              padding: 8px 14px; font-size: 15px; }
QPushButton:hover { background: #30363d; }
QPushButton:pressed { background: #1f6feb; }
QPushButton#nav { text-align: left; padding: 10px 12px; }
QPushButton#nav:checked { background: #1f6feb; border-color: #388bfd; }
QPushButton#back { background: #161b22; }
QListWidget, QTextBrowser { background: #010409; border: 1px solid #30363d; border-radius: 6px; }
QListWidget::item { padding: 6px 4px; border-bottom: 1px solid #161b22; }
QListWidget::item:selected { background: #1f6feb; color: #ffffff; }
QLineEdit { background: #010409; border: 1px solid #388bfd; border-radius: 6px; padding: 8px; font-size: 16px; }
QLabel#title { font-size: 20px; font-weight: bold; color: #79c0ff; }
QLabel#car { font-size: 15px; color: #8b949e; }
QLabel#flash { font-size: 15px; color: #e3b341; }
QLabel#prompt { font-size: 16px; font-weight: bold; color: #79c0ff; }
"""

NAV = tui.UI.MENU + [("n", "New ticket"), ("o", "Open a ticket"), ("s", "Settings"), ("Q", "Quit")]


class Navigate(Exception):
    """Raised out of whatever screen is open when a side button (or F-key) asks for another one."""

    def __init__(self, key):
        super().__init__(key)
        self.key = key


def _color(attr):
    for flag, color in COLORS:
        if attr & flag:
            return color
    return TEXT


def _buttons(hint, keys):
    """[(label, value)] for the hint words a button can stand for. value: a key, "#pick", "#reading" or None (back)."""
    out = []
    for tok in re.split(r"\s{2,}", hint.strip()):
        words = tok.split()
        if not words:
            continue
        first, rest = words[0], " ".join(words[1:])
        if first in ("Esc", "Esc/q", "q") and rest.startswith("back"):
            continue                                  # every screen gets its own Back button
        if first == "Enter" and rest:
            out.append((rest[0].upper() + rest[1:], "#pick"))
        elif first == "type" and "reading" in tok:
            out.append(("Enter a reading", "#reading"))
        elif len(first) == 1 and first in keys and rest:
            if first == "b" and rest.startswith("back"):
                rest = "back a step"                  # undo, not the screen's Back
            out.append((f"{rest[0].upper() + rest[1:]}   [{first}]", first))
    return out


class GUI(tui.UI):
    """tui.UI's screens, drawn in a window."""

    def __init__(self, desk):                        # no curses here: tui.UI.__init__ is not called
        self.desk = desk
        self.flash = ""
        self.B = BOLD
        self.c = {"head": HEAD, "good": GOOD, "warn": WARN, "bad": BAD, "acc": ACC, "dim": DIM, "sel": SEL}
        self.hint_spans = []
        self._loop = None
        self._res = None
        self._nav = None
        self._quitting = False
        self._mode = ""
        self._keys = ""
        self._filtering = False
        self._build()

    # ------------------------------------------------------------------ window
    def _build(self):
        w = self.win = _Window(self)
        w.setWindowTitle("Shopdeck")
        if os.path.isfile(ICON):
            w.setWindowIcon(QIcon(ICON))
        w.setStyleSheet(STYLE)
        root = QWidget()
        outer = QHBoxLayout(root)

        side = QVBoxLayout()
        self.nav = {}
        for i, (k, name) in enumerate(NAV):
            b = QPushButton((f"F{i + 1}  {name}" if k.isdigit() else name).replace("&", "&&"))
            b.setObjectName("nav")
            b.setCheckable(k.isdigit())
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, key=k: self._go(key))
            if k == "n":
                side.addSpacing(16)
            side.addWidget(b)
            self.nav[k] = b
            if k.isdigit():
                QShortcut(QKeySequence(f"F{int(k)}"), w, activated=lambda key=k: self._go(key))
        side.addStretch(1)
        outer.addLayout(side, 0)

        main = QVBoxLayout()
        top = QHBoxLayout()
        self.lbl_title = QLabel()
        self.lbl_title.setObjectName("title")
        self.lbl_car = QLabel()
        self.lbl_car.setObjectName("car")
        top.addWidget(self.lbl_title, 1)
        top.addWidget(self.lbl_car, 0)
        main.addLayout(top)

        mono = QFont("DejaVu Sans Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(13)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("type to filter")
        self.filter.textChanged.connect(self._refilter)
        self.filter.installEventFilter(w)
        main.addWidget(self.filter)
        self.stack = QStackedWidget()
        self.list = QListWidget()
        self.list.setFont(mono)
        self.list.setWordWrap(True)
        self.list.itemClicked.connect(lambda it: self._done(it.data(Qt.ItemDataRole.UserRole)))
        self.list.installEventFilter(w)
        self.list.viewport().installEventFilter(w)
        self.text = QTextBrowser()
        self.text.setFont(mono)
        self.text.setOpenLinks(False)
        self.text.installEventFilter(w)
        self.text.viewport().installEventFilter(w)
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.text)
        main.addWidget(self.stack, 1)

        self.ask_row = QWidget()
        ar = QHBoxLayout(self.ask_row)
        ar.setContentsMargins(0, 0, 0, 0)
        self.lbl_prompt = QLabel()
        self.lbl_prompt.setObjectName("prompt")
        self.lbl_prompt.setWordWrap(True)
        self.edit = QLineEdit()
        self.edit.installEventFilter(w)
        self.edit.returnPressed.connect(lambda: self._done(self.edit.text().strip()))
        self.b_ok = QPushButton("OK")
        self.b_ok.clicked.connect(lambda: self._done(self.edit.text().strip() if self._mode == "ask" else True))
        self.b_cancel = QPushButton("Cancel")
        self.b_cancel.clicked.connect(lambda: self._done(None if self._mode == "ask" else False))
        ar.addWidget(self.lbl_prompt, 2)
        ar.addWidget(self.edit, 3)
        ar.addWidget(self.b_ok)
        ar.addWidget(self.b_cancel)
        self.ask_row.hide()
        main.addWidget(self.ask_row)

        self.lbl_flash = QLabel()
        self.lbl_flash.setObjectName("flash")
        self.lbl_flash.setWordWrap(True)
        main.addWidget(self.lbl_flash)
        self.btn_row = QHBoxLayout()
        main.addLayout(self.btn_row)
        outer.addLayout(main, 1)
        w.setCentralWidget(root)
        w.resize(1280, 760)

    def _frame(self, title, hint, keys):
        d, case = self.desk, self.desk.case
        self.lbl_title.setText(title[:1].upper() + title[1:])
        codes = " ".join(case.codes[:6]) + ("…" if len(case.codes) > 6 else "")
        self.lbl_car.setText(f"{case.car}   {codes}".strip())
        for k, name in tui.UI.MENU:
            b = self.nav[k]
            try:
                st = self.status(k).strip()
            except Exception:
                st = ""
            b.setText(f"F{k}  {name}".replace("&", "&&") + (f"\n      {st.strip('[]')}".replace("&", "&&") if st else ""))
        self.lbl_flash.setText(self.flash)
        self.flash = ""
        while self.btn_row.count():
            it = self.btn_row.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        for label, val in _buttons(hint, keys):
            b = QPushButton(label.replace("&", "&&"))
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, v=val: self._button(v))
            self.btn_row.addWidget(b)
        self.btn_row.addStretch(1)
        back = QPushButton("◀  Back   [Esc]")
        back.setObjectName("back")
        back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        back.clicked.connect(lambda: self._done(None))
        self.btn_row.addWidget(back)

    def _button(self, val):
        if val == "#pick":
            self._pick()
        elif val == "#reading":
            self._done("#")
        elif self._mode == "choose":
            it = self.list.currentItem()
            self._done((val, it.data(Qt.ItemDataRole.UserRole) if it and not it.isHidden() else None))
        else:
            self._done(val)

    def _go(self, key):
        if self._loop is None:
            return
        self._nav = key
        self._loop.quit()

    def _done(self, val):
        self._res = val
        if self._loop is not None:
            self._loop.quit()

    def _wait(self, focus):
        self._res, self._nav = None, None
        focus.setFocus()
        self._loop = QEventLoop()
        self._loop.exec()
        self._loop = None
        if self._nav:
            key, self._nav = self._nav, None
            raise Navigate(key)
        return self._res

    # ------------------------------------------------------------------ keys and mouse (from _Window.eventFilter)
    def key(self, obj, ev):
        k, ch = ev.key(), ev.text()
        if obj is self.edit:
            if k == Qt.Key.Key_Escape:
                self._done(None)
                return True
            return False
        if self._mode == "confirm":
            if ch.lower() == "y" or k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._done(True)
            elif ch.lower() == "n" or k == Qt.Key.Key_Escape:
                self._done(False)
            return True
        if self._mode == "choose":
            filt = self.filter.text()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._pick()
                return True
            if k == Qt.Key.Key_Escape:
                if filt:
                    self.filter.clear()
                else:
                    self._done(None)
                return True
            if k == Qt.Key.Key_Backspace and self._filtering:
                self.filter.setText(filt[:-1])
                return True
            if ch and ch.isprintable() and not ev.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                                                Qt.KeyboardModifier.AltModifier):
                if not filt and ch in self._keys:
                    self._button(ch)
                elif not filt and ch == "q":
                    self._done(None)
                elif self._filtering and (ch != " " or filt):
                    self.filter.setText(filt + ch)
                return True
            if obj is self.filter and k in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
                QApplication.sendEvent(self.list, ev)
                return True
            return False
        if self._mode == "view":
            if k == Qt.Key.Key_Escape or (ch == "q" and "q" not in self._keys):
                self._done(None)
                return True
            if ch and ch in self._keys:
                self._done(ch)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and "#reading" in [v for _, v in
                                                                            _buttons(self._hint, self._keys)]:
                self._done("#")
                return True
            if k == Qt.Key.Key_Space:
                sb = self.text.verticalScrollBar()
                sb.setValue(sb.value() + sb.pageStep())
                return True
        return False

    def right_click(self):
        if self._mode in ("choose", "view"):
            self._done(None)
            return True
        return False

    # ------------------------------------------------------------------ the primitives tui.UI's screens call
    def view(self, title, lines, hint="↑↓ PgUp PgDn scroll   q back", keys="", top=0):
        self._mode, self._keys, self._hint = "view", keys, hint
        self.ask_row.hide()
        self.filter.hide()
        self._frame(title, hint, keys)
        parts = []
        for item in lines:
            t, a = item if isinstance(item, tuple) else (item, 0)
            if t == "":
                parts.append("<div>&nbsp;</div>")
                continue
            ind = len(t) - len(t.lstrip(" "))
            style = f"color:{_color(a)};margin-left:{ind * 0.6:.1f}em;white-space:pre-wrap;"
            if a & BOLD:
                style += "font-weight:bold;"
            parts.append(f'<div style="{style}">{html.escape(t.strip())}</div>')
        self.text.setHtml("".join(parts))
        self.stack.setCurrentWidget(self.text)
        sb = self.text.verticalScrollBar()
        QTimer.singleShot(0, lambda: sb.setValue(sb.maximum() if top >= 10 ** 6 else 0))
        return self._wait(self.text)

    def choose(self, title, items, hint=None, sel=0, keys="", filtering=True):
        if hint is None:
            hint = "Enter pick"
        self._mode, self._keys, self._filtering = "choose", keys, filtering
        self.ask_row.hide()
        self._frame(title, hint if "Enter" in hint else "Enter pick   " + hint, keys)
        self.list.clear()
        for i, it in enumerate(items):
            t, a = it if isinstance(it, tuple) else (it, 0)
            li = QListWidgetItem(t.strip())
            li.setData(Qt.ItemDataRole.UserRole, i)
            li.setForeground(QColor(_color(a)))
            if a & BOLD:
                f = li.font()
                f.setBold(True)
                li.setFont(f)
            self.list.addItem(li)
        self.filter.blockSignals(True)
        self.filter.clear()
        self.filter.blockSignals(False)
        self.filter.setVisible(filtering)
        if items:
            self.list.setCurrentRow(max(0, min(sel, len(items) - 1)))
        self.stack.setCurrentWidget(self.list)
        return self._wait(self.list)

    def _refilter(self, text):
        low = text.lower()
        first = None
        for r in range(self.list.count()):
            it = self.list.item(r)
            hide = bool(low) and low not in it.text().lower()
            it.setHidden(hide)
            if not hide and first is None:
                first = r
        if first is not None:
            self.list.setCurrentRow(first)

    def _pick(self):
        it = self.list.currentItem()
        if self._mode == "choose" and it is not None and not it.isHidden():
            self._done(it.data(Qt.ItemDataRole.UserRole))

    def ask(self, prompt, default=""):
        prev = self._mode
        self._mode = "ask"
        self.lbl_prompt.setText(prompt)
        self.edit.show()
        self.edit.setText(str(default))
        self.b_ok.setText("OK")
        self.b_cancel.setText("Cancel")
        self.lbl_flash.setText(self.flash)
        self.flash = ""
        self.ask_row.show()
        try:
            return self._wait(self.edit)
        finally:
            self.ask_row.hide()
            self._mode = prev

    def confirm(self, prompt):
        prev = self._mode
        self._mode = "confirm"
        self.lbl_prompt.setText(prompt + "?")
        self.edit.hide()
        self.b_ok.setText("Yes   [y]")
        self.b_cancel.setText("No   [n]")
        self.ask_row.show()
        try:
            return bool(self._wait(self.b_ok))
        finally:
            self.ask_row.hide()
            self._mode = prev

    def busy(self, text):
        self.lbl_flash.setText(text)
        QApplication.processEvents()

    # ------------------------------------------------------------------ running
    def run(self):
        pending = None
        while not self._quitting:
            try:
                if pending:
                    for k, b in self.nav.items():
                        b.setChecked(k == pending)
                    key, pending = pending, None
                    if self.open(key):
                        return
                    continue
                for b in self.nav.values():
                    b.setChecked(False)
                r = self.choose("menu", [f"{k}  {name}" + self.status(k) for k, name in self.MENU],
                                "Enter open   n new ticket   o open ticket   s settings   Q quit",
                                keys="12345678noQs", filtering=False)
                if r is None:
                    continue
                key = r[0] if isinstance(r, tuple) else self.MENU[r][0]
                if self.open(key):
                    return
            except Navigate as n:
                self.desk.save()
                pending = n.key
            except Exception as e:                      # a screen bug shouldn't close the shop's window
                traceback.print_exc()
                self.flash = f"something went wrong there ({type(e).__name__}: {e}); the ticket is saved"
                try:
                    self.desk.save()
                except Exception:
                    pass


class _Window(QMainWindow):
    def __init__(self, ui):
        super().__init__()
        self.ui = ui

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t == QEvent.Type.KeyPress:
            return self.ui.key(obj, ev)
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.RightButton:
            return self.ui.right_click()
        return False

    def closeEvent(self, ev):
        if self.ui._quitting:
            ev.accept()
            return
        ev.ignore()
        self.ui._go("Q")


def main(desk):
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Shopdeck")
    app.setDesktopFileName("shopdeck")
    ui = GUI(desk)
    ui.win.showMaximized()

    def go():
        try:
            ui.run()
        finally:
            desk.save()
            ui._quitting = True
            ui.win.close()
            app.quit()

    QTimer.singleShot(0, go)
    app.exec()
