"""Shared widgets, the error bus and the window style."""
import re
import theme as T
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QComboBox, QGridLayout, QLabel, QLineEdit, QPushButton, QWidget
from ui.context import log_caught


class ErrorBus(QObject):
    """Unhandled errors (any thread) end up on screen instead of PyQt6 closing the whole app."""
    error = pyqtSignal(str)


class Bubbles(QWidget):
    """Tap-to-toggle bubbles plus a typing box for anything not listed. text() joins them with '; '.
    sections: [(heading or None, [labels])]. single=True makes it pick-one."""
    changed = pyqtSignal()

    def __init__(self, sections, cols=6, other_hint="other (type)", single=False, tips=None):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.buttons = []
        self.group = QButtonGroup(self) if single else None
        if self.group:
            self.group.setExclusive(False)
        row = 0
        for head, labels in sections:
            col0 = 0
            if head:
                lbl = QLabel(head)
                lbl.setStyleSheet(f"color: {T.MUTED};")
                grid.addWidget(lbl, row, 0)
                col0 = 1
            for i, t in enumerate(labels):
                b = QPushButton(t)
                b.setObjectName("bubble")
                b.setCheckable(True)
                if tips and t in tips:
                    b.setToolTip(tips[t])
                b.clicked.connect(lambda on, b=b: self._clicked(b, on))
                self.buttons.append(b)
                grid.addWidget(b, row + i // cols, col0 + i % cols)
            row += (len(labels) + cols - 1) // cols
        self.other = QLineEdit()
        self.other.setPlaceholderText(other_hint)
        self.other.textChanged.connect(self._typed)
        grid.addWidget(self.other, row, 0, 1, cols + (1 if any(h for h, _ in sections) else 0))

    def _typed(self, t):
        if self.group is not None and t.strip():
            for o in self.buttons:
                o.setChecked(False)
        self.changed.emit()

    def _clicked(self, b, on):
        if self.group is not None and on:
            for o in self.buttons:
                if o is not b:
                    o.setChecked(False)
            self.other.clear()
        self.changed.emit()

    def text(self):
        parts = [b.text() for b in self.buttons if b.isChecked()]
        o = self.other.text().strip()
        return "; ".join(parts + ([o] if o else []))

    def set_text(self, s):
        by_label = {b.text().lower(): b for b in self.buttons}
        rest = []
        for b in self.buttons:
            b.setChecked(False)
        for item in re.split(r"[;,]\s*", s or ""):
            item = item.strip()
            if not item:
                continue
            if item.lower() in by_label and not (self.group is not None and any(b.isChecked() for b in self.buttons)):
                by_label[item.lower()].setChecked(True)
            else:
                rest.append(item)
        self.other.setText("; ".join(rest))


class PopupCombo(QComboBox):
    """A combo box that refreshes its list each time it opens."""
    def __init__(self, on_open):
        super().__init__()
        self._on_open = on_open

    def showPopup(self):
        self._on_open()
        super().showPopup()


class Field(QLineEdit):
    """A typed line with the Bubbles text()/set_text() interface (codes and checks are typed, not tapped:
    a fixed chip list ends up being one car's history)."""
    def set_text(self, s):
        self.setText(s or "")


def vehicle_table(retriever):
    """{(model, year): {engine codes}} from the kb's vehicle table (every car the manuals cover)."""
    out = {}
    db = getattr(retriever, "db", None)
    if db is not None:
        try:
            for model, year, engine in db.execute("SELECT model, year, engine FROM vehicle"):
                out.setdefault((model, year), set()).add(engine)
        except Exception as e:
            log_caught("vehicle table", e)
    return out


STYLE = T.qss()
IDLE, BUSY, ERR = T.fg(T.MUTED), T.fg(T.AMBER, bold=True), T.fg(T.RED, bold=True)
