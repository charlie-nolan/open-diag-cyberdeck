"""The user's palette, shared by the Home app (cyberdeck_gui.py) and the diagent GUI, so the two never drift.
Dark only, no light mode and no toggle (10-26). Briefs: handoff/HOME_THEME.md, DIAGENT_GUI.md.
Tune colours here and nowhere else."""

BG = "#0f1412"          # window background: near-black, slight green
PANEL = "#163538"       # side panels, cards, input fields (Air Corps, dark)
PANEL_2 = "#18211c"     # a step lighter than bg: main cards, the user's own lines
AIR_CORPS = "#1f4a4f"   # Noodler's Air Corps Blue-Black: borders, headers, tab bar, frames
FOREST = "#3e7a55"      # primary buttons, active tab, "your turn", focus ring
FOREST_TEXT = "#6fae86"  # forest readable as text on bg
AQUA = "#27ffc2"        # live values only
TEAL = "#00897B"        # live panel frames, secondary live elements
PURPLE = "#8b6fc0"      # sources and citations (frames, chips)
PURPLE_TEXT = "#a991d8"  # purple readable as text on bg
OK = "#7fbf8f"          # in spec (calm green)
AMBER = "#d9a441"       # out of spec / attention
RED = "#c4544a"         # stop and danger, rarely
GREY = "#6b7570"        # no spec / disabled
TEXT = "#e3e8e4"        # body text
MUTED = "#93a39a"       # secondary text

SANS = '"Noto Sans", "DejaVu Sans", "Liberation Sans", sans-serif'
MONO = '"DejaVu Sans Mono", "Noto Sans Mono", "Liberation Mono", monospace'

# status word -> colour; always shown with the word too, never colour alone
STATUS = {"ok": OK, "in spec": OK, "in_zone": OK, "low": AMBER, "high": AMBER, "out": AMBER, "out of spec": AMBER,
          "danger": RED, "stop": RED, "none": GREY, "no spec": GREY, "no_zone": GREY}


def fg(colour, bold=False):
    """A label's stylesheet: text colour (and weight)."""
    return f"color: {colour};" + (" font-weight: bold;" if bold else "")


def button(bg, fg_=TEXT, extra=""):
    """A coloured button's stylesheet (primary = FOREST, stop = RED)."""
    return f"background-color: {bg}; color: {fg_}; border-color: {bg}; {extra}".strip()


def chip(colour):
    """A small outlined chip (mode labels, statuses, citations)."""
    return f"color: {colour}; border: 1px solid {colour}; border-radius: 10px; padding: 2px 10px;"


def qss(font_px=14):
    """The application stylesheet. Text panes stay monospace (readings, part numbers, aligned tables)."""
    return f"""
    QMainWindow, QDialog, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget, QStackedWidget {{
        background-color: {BG}; }}
    QWidget {{ color: {TEXT}; font-family: {SANS}; font-size: {font_px}px; }}
    QLabel#chip {{ border: 1px solid {AIR_CORPS}; border-radius: 10px; padding: 2px 10px; }}
    QToolTip {{ background-color: {PANEL}; color: {TEXT}; border: 1px solid {AIR_CORPS}; }}
    QPushButton {{ background-color: {PANEL_2}; color: {TEXT}; border: 1px solid {AIR_CORPS};
                  padding: 9px; border-radius: 6px; min-height: 22px; }}
    QPushButton:hover {{ border-color: {FOREST}; }}
    QPushButton:focus {{ border: 2px solid {FOREST}; }}
    QPushButton:checked {{ background-color: {FOREST}; color: {TEXT}; border-color: {FOREST}; }}
    QPushButton:disabled {{ color: {GREY}; border-color: {PANEL}; }}
    QPushButton#primary {{ background-color: {FOREST}; border-color: {FOREST}; }}
    QPushButton#stop {{ color: {RED}; border-color: {RED}; background-color: {BG}; }}
    QPushButton#bubble {{ border-radius: 16px; padding: 6px 12px; }}
    QPushButton#bubble:checked {{ background-color: {FOREST}; border-color: {FOREST}; }}
    QTextEdit, QPlainTextEdit, QTextBrowser {{ background-color: {PANEL_2}; color: {TEXT}; font-family: {MONO};
                  border: 1px solid {AIR_CORPS}; border-radius: 6px; padding: 7px; }}
    QLineEdit, QComboBox, QSpinBox {{ background-color: {PANEL}; color: {TEXT}; border: 1px solid {AIR_CORPS};
                  border-radius: 6px; padding: 7px; selection-background-color: {FOREST}; }}
    QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {FOREST}; }}
    QComboBox QAbstractItemView {{ background-color: {PANEL}; color: {TEXT}; selection-background-color: {FOREST}; }}
    QGroupBox {{ border: 1px solid {AIR_CORPS}; border-radius: 6px; margin-top: 12px; padding-top: 8px;
                font-weight: bold; color: {TEXT}; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {MUTED}; }}
    QTreeWidget, QListWidget, QTableWidget {{ background-color: {PANEL_2}; border: 1px solid {AIR_CORPS};
                border-radius: 6px; alternate-background-color: {BG}; }}
    QTreeWidget::item, QListWidget::item {{ padding: 4px; }}
    QTreeWidget::item:selected, QListWidget::item:selected {{ background-color: {FOREST}; color: {TEXT}; }}
    QHeaderView::section {{ background-color: {AIR_CORPS}; color: {TEXT}; border: 0; padding: 4px; }}
    QTabWidget::pane {{ border: 1px solid {AIR_CORPS}; border-radius: 6px; }}
    QTabBar::tab {{ background-color: {PANEL}; color: {MUTED}; border: 1px solid {AIR_CORPS}; padding: 8px 14px;
                   border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:selected {{ background-color: {FOREST}; color: {TEXT}; border-color: {FOREST}; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; }}
    QScrollBar:vertical {{ background: {BG}; width: 12px; }}
    QScrollBar::handle:vertical {{ background: {AIR_CORPS}; border-radius: 5px; min-height: 30px; }}
    QScrollBar:horizontal {{ background: {BG}; height: 12px; }}
    QScrollBar::handle:horizontal {{ background: {AIR_CORPS}; border-radius: 5px; min-width: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QSplitter::handle {{ background-color: {AIR_CORPS}; }}
    QProgressBar {{ background-color: {PANEL}; border: 1px solid {AIR_CORPS}; border-radius: 6px; color: {TEXT};
                   text-align: center; }}
    QProgressBar::chunk {{ background-color: {FOREST}; border-radius: 5px; }}
    """
