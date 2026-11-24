"""Two-way tests screen (Subaru SSM2 actuator tests, clear memory, STOP ALL)."""
import theme as T
from PyQt6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget, QMessageBox, QPushButton, QTextEdit, QVBoxLayout,
    QWidget)
from ui.config import CFG
from ui.workers import TestWorker
from ui.widgets import BUSY, ERR, IDLE


class TwoWayTestsScreen:
    def _tests_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.b_testmode = QPushButton("TEST MODE: OFF")
        self.b_testmode.setCheckable(True)
        self.b_testmode.setToolTip("Two-way tests make parts of the car move. Off every time the deck starts.")
        self.b_testmode.clicked.connect(self.act_test_mode)
        self.lbl_testmode = QLabel("Reading only. Switch TEST MODE on to run tests or clear codes.")
        self.lbl_testmode.setStyleSheet(IDLE)
        self.lbl_testmode.setWordWrap(True)
        top.addWidget(self.b_testmode)
        top.addWidget(self.lbl_testmode, 1)
        lay.addLayout(top)

        sub = QGroupBox("SUBARU (SSM: KKL cable, or the EX if the KKL doesn't answer)")
        sl = QGridLayout(sub)
        how = QLabel("Join the green test-mode connectors under the dash with the key OFF, then key ON, engine OFF.")
        how.setWordWrap(True)
        sl.addWidget(how, 3, 0, 1, 2)
        self.b_t_connect = QPushButton("CONNECT TO SUBARU ECU")
        self.b_t_connect.clicked.connect(lambda: self.test_worker and self.test_worker.do("connect"))
        self.lst_tests = QListWidget()
        self.lst_tests.setToolTip("The actuator tests this ECU says it supports (from its own init reply)")
        self.b_t_start = QPushButton("RUN SELECTED TEST")
        self.b_t_start.clicked.connect(self.act_run_test)
        self.b_t_clearmem = QPushButton("CLEAR SUBARU ECU MEMORY")
        self.b_t_clearmem.clicked.connect(self.act_clear_memory)
        sl.addWidget(self.b_t_connect, 0, 0)
        sl.addWidget(self.b_t_clearmem, 0, 1)
        sl.addWidget(self.lst_tests, 1, 0, 1, 2)
        sl.addWidget(self.b_t_start, 2, 0, 1, 2)
        lay.addWidget(sub, 2)

        gen = QHBoxLayout()
        self.b_t_clearcodes = QPushButton("CLEAR CODES (OBD-II mode 04 on the EX; KKL fallback on a Subaru)")
        self.b_t_clearcodes.clicked.connect(self.act_clear_codes)
        gen.addWidget(self.b_t_clearcodes)
        lay.addLayout(gen)

        self.b_t_stop = QPushButton("■ STOP ALL ACTUATORS")
        self.b_t_stop.setMinimumHeight(64)
        self.b_t_stop.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT}; font-size: 20px; font-weight: bold;")
        self.b_t_stop.clicked.connect(self.act_stop_all)
        lay.addWidget(self.b_t_stop)

        self.txt_tests = QTextEdit()
        self.txt_tests.setReadOnly(True)
        lay.addWidget(self.txt_tests, 1)
        self.stack.addWidget(w)
        self._show_test_mode()

    def _show_test_mode(self, running=""):
        on = self.test_worker is not None
        self.b_testmode.setChecked(on)
        self.b_testmode.setText("TEST MODE: ON" if on else "TEST MODE: OFF")
        self.b_testmode.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT}; font-weight: bold;" if on else "")
        if running:
            self.lbl_testmode.setText(f"RUNNING: {running}")
            self.lbl_testmode.setStyleSheet(ERR)
        elif on:
            self.lbl_testmode.setText("TEST MODE on. Each action asks you first. The deck holds the cable.")
            self.lbl_testmode.setStyleSheet(BUSY)
        else:
            self.lbl_testmode.setText("Reading only. Switch TEST MODE on to run tests or clear codes.")
            self.lbl_testmode.setStyleSheet(IDLE)
        for b in (self.b_t_connect, self.b_t_clearmem, self.b_t_clearcodes, self.b_t_stop):
            b.setEnabled(on)
        self.b_t_start.setEnabled(on and self.lst_tests.count() > 0)
        if not on:
            self.lst_tests.clear()

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def act_test_mode(self):
        if self.test_worker is not None:
            tw = self.test_worker
            if tw.isRunning():
                self.b_testmode.setEnabled(False)
                self._say(self.txt_tests, "[*] Switching TEST MODE off (finishing the current step)...", T.MUTED)
                tw.finished.connect(self._test_mode_off)
                tw.do("quit")
            else:
                self._test_mode_off()
            return
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            self._say(self.txt_tests, "[!] The cable is busy (READ CAR or another program). Try again when it's done.", T.AMBER)
            self._show_test_mode()
            return
        if not self._confirm("Turn TEST MODE on?",
                             "Two-way tests make parts of the car work: fuel pump, fans, solenoids, valves.\n\n"
                             "Key ON, engine OFF. Parking brake on, in Park or neutral. Hands and tools clear of "
                             "the fans and belts.\n\nTurn TEST MODE on?"):
            self._show_test_mode()
            return
        tw = TestWorker(CFG["obd_port"] if CFG.get("obd_adapter") == "kkl" else "")
        tw.msg.connect(lambda t, c: self._say(self.txt_tests, t, c))
        tw.tests.connect(self._show_tests)
        tw.running.connect(self._show_test_mode)
        self.test_worker = tw
        tw.start()
        self.b_obd.setEnabled(False)
        self._say(self.txt_tests, "[*] TEST MODE on. READ CAR and other programs wait until you switch it off.", T.AMBER)
        self._show_test_mode()

    def _test_mode_off(self):
        self.test_worker = None
        self.b_testmode.setEnabled(True)
        self._say(self.txt_tests, "[✓] TEST MODE off. The cable is back for READ CAR.", T.OK)
        self.b_obd.setEnabled(True)
        self._show_test_mode()

    def _show_tests(self, titles):
        self.lst_tests.clear()
        self.lst_tests.addItems(titles)
        self.b_t_start.setEnabled(self.test_worker is not None and bool(titles))

    def act_run_test(self):
        item = self.lst_tests.currentItem()
        if self.test_worker is None or item is None:
            self._say(self.txt_tests, "[!] Pick a test in the list first.", T.AMBER)
            return
        if self.worker is not None and self.worker.isRunning():
            self._say(self.txt_tests, "[!] A model is still answering. Wait for it (or press STOP), then run the test.",
                      T.AMBER)
            return
        import ssm_tests
        if self._confirm(f"Run {item.text()}?",
                         f"{item.text()} will be switched on until you press STOP ALL "
                         f"(it stops by itself after {ssm_tests.MAX_TEST_S} s).\n\nKey ON, engine OFF, hands clear. Run it?"):
            self.test_worker.do("start", self.lst_tests.row(item))

    def act_stop_all(self):
        if self.test_worker is not None:
            self.test_worker.emergency_stop()

    def act_clear_memory(self):
        if self.test_worker is not None and self._confirm(
                "Clear the Subaru ECU memory?",
                "This erases the stored trouble codes and the ECU's learned values (idle, fuel trims). "
                "Write the codes down first. The engine may idle rough until it relearns.\n\n"
                "Key ON, engine OFF. Clear it?"):
            self.test_worker.do("clear_memory")

    def act_clear_codes(self):
        if self.test_worker is not None and self._confirm(
                "Clear the trouble codes?",
                "OBD-II mode 04 clears the stored codes, freeze frame and the emissions readiness monitors "
                "(an inspection will fail until a drive cycle sets them again). Write the codes down first.\n\n"
                "It goes through the OBDLink EX. If the EX doesn't answer on a Subaru, it clears the ECU memory "
                "through the KKL cable instead, which also resets learned idle and fuel values.\n\n"
                "Key ON, engine OFF. Clear them?"):
            self.test_worker.do("clear_codes")
