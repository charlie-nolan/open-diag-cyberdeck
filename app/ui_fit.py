"""Layout helpers so a window fits landscape and portrait screens at any desktop scaling (The user's monitor:
1920x1280 at 150%, so about 1280x853 or 853x1280 logical px). Shared by the Home app and the diagent GUI."""
from PyQt6.QtWidgets import QGridLayout, QLayout, QSizePolicy, QWidget


class ReflowRow(QWidget):
    """A row of buttons that wraps onto more rows when the window is narrow. Every cell gets the same width,
    so a full row fills the window like a QHBoxLayout would. Widget order is kept (left to right, then down)."""

    def __init__(self, widgets=(), spacing=6, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(spacing)
        self.grid.setVerticalSpacing(spacing)
        self.grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)   # our minimumSizeHint rules
        self.widgets = []
        self.cols = 0
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        for w in widgets:
            self.add(w)

    def add(self, w):
        self.widgets.append(w)
        self.cols = 0
        self._place(max(1, self.width()))

    def _cell(self):
        return max((w.sizeHint().width() for w in self.widgets), default=0)

    def _cols(self, width):
        """The most columns that fit: each column as wide as its widest button."""
        sp = self.grid.horizontalSpacing()
        hints = [w.sizeHint().width() for w in self.widgets]
        for n in range(len(hints), 1, -1):
            if sum(max(hints[c::n]) for c in range(n)) + sp * (n - 1) <= width:
                return n
        return 1

    def _place(self, width):
        if not self.widgets:
            return
        cols = self._cols(width)
        if cols == self.cols:
            return
        self.cols = cols
        for w in self.widgets:
            self.grid.removeWidget(w)
        for c in range(max(self.grid.columnCount(), cols)):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        for i, w in enumerate(self.widgets):
            self.grid.addWidget(w, i // cols, i % cols)
        self.updateGeometry()

    def minimumSizeHint(self):
        s = super().minimumSizeHint()
        s.setWidth(self._cell())       # one column is always possible, so never force the window wider
        return s

    def resizeEvent(self, e):
        self._place(e.size().width())
        super().resizeEvent(e)


class FitRoot(QWidget):
    """The window's content inside its QScrollArea. Word-wrapped labels give a layout height-for-width, and then
    QScrollArea sizes the content to its *preferred* height and scrolls on screens it would fit; this makes it
    scroll only below the real minimum."""

    def hasHeightForWidth(self):
        return False

    def heightForWidth(self, w):
        return -1      # QScrollArea asks the layout directly, then calls this: -1 = use the minimum size
