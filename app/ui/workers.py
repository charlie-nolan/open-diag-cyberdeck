"""Background threads: model calls, case context, OBD-II reads, test drives, two-way tests."""
import charm_pics
import obd_all
import os
import queue
import re
import telemetry_ref
import theme as T
import threading
import time
import urllib.error
from PyQt6.QtCore import QThread, pyqtSignal
from vehicle_id import decode_vin, nhtsa_decode
from ui.config import CFG, save_config_key
from ui.context import case_context


class PreloadWorker(QThread):
    """Loads the deck's fast model at launch, so the first question doesn't wait for it (11-02)."""

    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr

    def run(self):
        try:
            self.mgr.get("fast")
        except Exception as e:
            print(f"fast model preload: {e}")


class GenWorker(QThread):
    token = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal(str)

    def __init__(self, mgr, role, prompt, keep_history=False, max_new_tokens=None, temperature=None):
        super().__init__()
        self.mgr, self.role, self.prompt = mgr, role, prompt
        self.keep_history, self.max_new_tokens, self.temperature = keep_history, max_new_tokens, temperature

    def run(self):
        try:
            if self.role not in self.mgr.models:
                self.status.emit(f"loading {self.mgr.label(self.role)}...")
            model = self.mgr.get(self.role)
            self.status.emit(f"{self.mgr.label(self.role)} generating...")
            text = model.generate(self.prompt, self.token.emit, keep_history=self.keep_history,
                                  max_new_tokens=self.max_new_tokens, temperature=self.temperature)
        except Exception as e:
            text = f"\n[LLM error: {e}]"
            self.token.emit(text)
            self.mgr.pc_failed(self.role)
        self.done.emit(text)


class CaseContextWorker(QThread):
    """The manual lookup for a saved case, off the UI thread: on a big make it can take a minute on the deck."""
    result = pyqtSignal(str)

    def __init__(self, retriever, case, budget, **kw):
        super().__init__()
        self.retriever, self.case, self.budget, self.kw = retriever, case, budget, kw

    def run(self):
        try:
            self.result.emit(case_context(self.retriever, self.case, budget=self.budget, **self.kw))
        except Exception as e:
            print(f"manual lookup failed: {e}")
            self.result.emit("")


class WarmWorker(QThread):
    """One throwaway vector search, so the embedding model and the car's vectors are loaded before anyone asks."""

    def __init__(self, retriever):
        super().__init__()
        self.retriever = retriever

    def run(self):
        try:
            self.retriever.search_ids("engine", 1)
        except Exception as e:
            print(f"retriever warm-up: {e}")


class VinWorker(QThread):
    """NHTSA's online VIN decode off the UI thread (a slow hotspot must not freeze the screen)."""
    result = pyqtSignal(str, object)

    def __init__(self, vin):
        super().__init__()
        self.vin = vin

    def run(self):
        self.result.emit(self.vin, nhtsa_decode(self.vin, nhtsa_cache_path()))


class PicWorker(QThread):
    """CHARM page pictures from the home PC, off the UI thread (cache/charm/ keeps them for offline)."""
    done = pyqtSignal(list, str)

    def __init__(self, names):
        super().__init__()
        self.names = names

    def run(self):
        have, why = charm_pics.fetch(CFG["pic_url"], CFG["cache_dir"], self.names, db=CFG.get("kb_db"))
        self.done.emit(have, why)


class ChartWorker(QThread):
    """Asks the PC to read chart pictures (35B vision, queued there behind other GPU jobs) and polls until
    each one is read. Emits each reading as it lands."""
    result = pyqtSignal(str, dict)
    note = pyqtSignal(str)
    POLL_S, GIVE_UP_S = 15, 45 * 60

    def __init__(self, names):
        super().__init__()
        self.names = names
        self.stop_flag = threading.Event()

    def run(self):
        left, t0, said = list(self.names), time.time(), ""
        while left and not self.stop_flag.is_set():
            for n in list(left):
                try:
                    r = charm_pics.reading(CFG["pic_url"], n, db=CFG.get("kb_db"))
                except urllib.error.HTTPError as e:
                    self.note.emit(f"The PC's picture server can't read charts (it answered {e.code}; was it started "
                                   "with --no-read?). The pictures above still show.")
                    return
                except (OSError, ValueError):
                    self.note.emit("Chart reading needs the PC, and it isn't reachable now. The pictures above are "
                                   "still the manual's own; tap CHARTS again once the PC is back.")
                    return
                if r.get("state") in ("done", "skipped", "error"):
                    left.remove(n)
                    self.result.emit(n, r)
                elif r.get("state") == "queued" and n == left[0]:
                    msg = (f"{len(left)} chart pictures waiting on the PC" +
                           (": the GPU is busy with a database build, so they wait for it" if r.get("gpu_busy")
                            else f" ({r.get('ahead', 0)} other pictures ahead)") + ". About a minute each.")
                    if msg != said:
                        self.note.emit(msg)
                        said = msg
            if time.time() - t0 > self.GIVE_UP_S:
                self.note.emit(f"Stopped waiting for {len(left)} chart readings; they keep going on the PC, so tap "
                               "CHARTS later and they'll show at once.")
                return
            self.stop_flag.wait(self.POLL_S if left else 0)


def nhtsa_cache_path():
    return os.path.join(CFG["cache_dir"], "vin_nhtsa.json")


class OBDWorker(QThread):
    """One read-only snapshot. OBDLink EX (main cable): generic OBD-II codes, VIN and every PID the car offers
    (obd_all.py), plus mode 06 results on READ CAR,
    then, unless the VIN says another make, Subaru SSM through the same EX. Bare KKL cable: SSM only.
    result fires only with real data; problems go to note, so an error never lands in the Live data box."""
    result = pyqtSignal(str, str)
    note = pyqtSignal(str)
    ident = pyqtSignal(str, str, str, str)
    mode06 = pyqtSignal(str)
    read_mode06 = True

    def __init__(self, adapter, port, make=""):
        super().__init__()
        self.adapter, self.port = adapter, port or None
        self.make, self.protocol = make, ""

    def run(self):
        try:
            self._read()
        except Exception as e:
            self.note.emit(f"Read failed: {e}")

    def _ssm_note(self, note):
        self.note.emit(note)
        m = re.match(r"SSM (\S+) \(([^)]*)\) ROM (\S+)", note)
        if m:
            self.ident.emit("ssm", m.group(1), m.group(2), f"ROM {m.group(3)}")
            v = re.search(r"\bVIN ([A-HJ-NPR-Z0-9]{17})\b", note)
            if v:
                self.ident.emit("vin", v.group(1), m.group(2), f"ROM {m.group(3)}")
        return bool(m)

    def _read(self):
        if self.adapter == "kkl":
            from ssm_reader import read_ssm
            codes, live, note = read_ssm(self.port, CFG["session_db"], CFG["cache_dir"])
            self._ssm_note(note)
            self._seen_ssm(note, "KKL", "ecu_init_reply.hex")
            if live or codes:
                CFG["_live"] = ("ssm", live)
                self.result.emit(codes, live)
            return
        codes, live, vin_make = self._read_obd()
        CFG["_live"] = ("obd01", live)
        if not obd_all.may_speak_ssm(vin_make or self.make, self.protocol):
            if not (vin_make or self.make):
                self.note.emit("Type the VIN (windshield or door jamb), or pick the Make.")
            if codes or live:
                self.result.emit(codes, live)
            return
        if CFG.get("_ex_ssm_failed"):
            self.note.emit("Skipping Subaru SSM through the EX (no answer earlier). For SSM codes and live data "
                           "on a Subaru, pick the KKL cable.")
            if codes or live:
                self.result.emit(codes, live)
            return
        from ssm_reader import read_ssm_ex
        self.note.emit("Asking for Subaru SSM through the EX...")
        s_codes, s_live, note, variant = read_ssm_ex(self.port, CFG["session_db"], CFG["cache_dir"],
                                                     CFG.get("ex_ssm_variant", ""))
        ok = self._ssm_note(note)
        if not ok and (codes or live):
            CFG["_ex_ssm_failed"] = True
        if not ok:
            if not vin_make:
                self.note.emit("Type the VIN (windshield or door jamb), or pick the Make.")
        elif variant != CFG.get("ex_ssm_variant"):
            CFG["ex_ssm_variant"] = variant
            save_config_key("ex_ssm_variant", variant)
        codes = ", ".join(dict.fromkeys(c for c in (codes + ", " + s_codes).split(", ") if c))
        if s_live and s_live != "(no live data)":
            ssm_labels = {x.strip().split(" ")[0] for x in s_live.split("|")}
            extra = [x.strip() for x in live.split("|") if x.strip() and x.strip().split(" ")[0] not in ssm_labels]
            live = " | ".join([s_live] + extra)
            CFG["_live"] = ("ssm", live)
            self._seen_ssm(note, "EX", "ecu_init_reply_ex.hex")
        if codes or live:
            self.result.emit(codes, live)

    def _read_obd(self):
        """Generic OBD-II over the EX. Returns (codes, live, make from the VIN or '')."""
        try:
            import obd
        except ImportError:
            self.note.emit("python-obd not installed (pip install obd); trying SSM only")
            return "", "", ""
        from cables import ex_port
        conn, why = obd_all.connect(ex_port(self.port or ""))
        if conn is None:
            self.note.emit(f"No generic OBD-II answer ({why}). Ignition on? Adapter plugged in?")
            return "", "", ""
        self.protocol = conn.protocol_name() or ""
        vin_make = self._identify(conn)
        dtcs = conn.query(obd.commands.GET_DTC).value or []
        codes = ", ".join(c for c, _ in dtcs)
        got = obd_all.read_all(conn)
        self._seen_obd(conn, got)
        self.note.emit(f"EX: car offers {len(got['supported'])} PIDs: {' '.join(got['supported'])}"
                       + (f"; no answer from {' '.join(got['silent'])}" if got["silent"] else ""))
        if self.read_mode06:
            self._mode06(conn)
        conn.close()
        return codes, got["line"], vin_make

    def _mode06(self, conn):
        """On-board test results: all of them to cache/mode06_last.txt, failures to the case. Never stops a read."""
        try:
            lines, failed, kind = obd_all.mode06(conn)
            with open(os.path.join(CFG["cache_dir"], "mode06_last.txt"), "w") as f:
                f.write("\n".join(lines) + "\n")
            if kind == "decoded":
                self.note.emit(f"Mode 06: {len(lines)} tests, {len(failed)} failed (cache/mode06_last.txt)"
                               + "".join(f"\n  {x}" for x in failed))
            elif lines:
                self.note.emit(f"Mode 06: {len(lines)} raw frames saved to cache/mode06_last.txt (on pre-CAN cars "
                               "their meaning is the maker's own, so the deck doesn't judge them)")
            self.mode06.emit("; ".join(failed))
        except Exception as e:
            print(f"mode 06: {e}")

    def _seen_obd(self, conn, got):
        try:
            car = getattr(self, "seen_car", None)
            if not car:
                return
            telemetry_ref.record_seen(car, "EX", conn.protocol_name() or "", "obd01", got["supported"],
                                      sorted(set(got["asked"]) - set(got["supported"])))
        except Exception as e:
            print(f"telemetry seen (OBD): {e}")

    def _seen_ssm(self, note, cable, hex_name):
        try:
            m = re.search(r"\bROM (\S+)", note or "")
            kb = os.path.expanduser(CFG.get("kb_db") or "")
            path = os.path.join(CFG["cache_dir"], hex_name)
            if not (m and os.path.isfile(path) and telemetry_ref.has_tables(kb)):
                return
            init = bytes.fromhex(open(path).read().replace(" ", "").strip())
            yes, no = telemetry_ref.ssm_support(init, kb)
            telemetry_ref.record_seen({"ecu_id": m.group(1)}, cable, "SSM2", "ssm", yes, no)
        except Exception as e:
            print(f"telemetry seen (SSM): {e}")

    def _identify(self, conn):
        """Ask every car for its VIN (mode 09 PID 02: required from 2005, many older cars answer too).
        No VIN: report the protocol and OBD standard as a maker/market hint. Read-only requests only."""
        import obd

        def ask(name, force=False):
            cmd = getattr(obd.commands, name, None)
            if cmd is None or not (force or conn.supports(cmd)):
                return ""
            r = conn.query(cmd, force=force)
            if r.is_null():
                return ""
            v = r.value
            return (v.decode(errors="ignore") if isinstance(v, (bytes, bytearray)) else str(v)).strip(" \x00")
        cal = ask("CALIBRATION_ID")
        key = f"CAL {cal}" if cal else ""
        vin = ask("VIN", force=True)
        self.seen_car = {"vin": vin} if vin else {"ecu_id": key} if key else None
        if vin:
            self.ident.emit("vin", vin, "", key)
            return decode_vin(vin, CFG.get("vin_db"))["make"]
        proto = conn.protocol_name() or ""
        hint = {"J1850 PWM": "J1850 PWM: usually Ford or Mazda", "J1850 VPW": "J1850 VPW: usually GM or Chrysler"}
        text = next((h for k, h in hint.items() if k in proto), proto)
        std = ask("OBD_COMPLIANCE")
        self.ident.emit("hint", "; ".join(x for x in (text, std) if x), "", key)
        return ""


class DriveWorker(OBDWorker):
    """TEST DRIVE: the same read-only READ CAR snapshot, repeated every `every` seconds until stopped.
    Holds the cable for the whole drive, so READ CAR, TEST MODE and other programs wait."""
    sample = pyqtSignal(float, str, str)
    read_mode06 = False

    def __init__(self, adapter, port, every, make=""):
        super().__init__(adapter, port, make)
        self.every = every
        self.stop_flag = threading.Event()
        self.result.connect(lambda codes, live: self.sample.emit(time.time(), codes, live))

    def run(self):
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                self._read()
            except Exception as e:
                self.note.emit(f"Read failed: {e}")
            self.stop_flag.wait(max(1.0, self.every - (time.time() - t0)))


class TestWorker(QThread):
    """TEST MODE: holds the cable while the switch is on and runs only what a confirmed button asked for.
    Subaru actuator tests and clear memory go through ssm/ssm_tests.py (FreeSSM's checks: test-mode
    connector joined, engine off); clear codes for any car is OBD-II mode 04 through python-obd."""
    msg = pyqtSignal(str, str)
    tests = pyqtSignal(list)
    running = pyqtSignal(str)

    def __init__(self, port):
        super().__init__()
        self.adapter, self.port = "kkl", port or None
        self.q = queue.Queue()
        self.t = None
        self._last = None
        self.quitting = False

    def do(self, op, arg=None):
        if op == "quit":
            self.quitting = True
        self.q.put((op, arg))

    def emergency_stop(self):
        """STOP ALL from the GUI thread: doesn't wait in the queue behind a slow job. Writes 0x00 to every
        actuator byte (the only writes it can make) on its own thread, so the screen never freezes."""
        t = self.t
        if t is None:
            self.msg.emit("[!] Not connected to an ECU, so there's nothing the deck can switch off. "
                          "Key OFF to be sure.", T.AMBER)
            return

        def go():
            try:
                ok = t.stop()
            except Exception as e:
                ok = False
                self.msg.emit(f"[!] Stop failed ({e}).", T.RED)
            self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                          "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)
        threading.Thread(target=go, daemon=True).start()

    def run(self):
        from ssm_tests import Refused
        while True:
            try:
                op, arg = self.q.get(timeout=0.25)
            except queue.Empty:
                self._report()
                continue
            if op == "quit":
                break
            try:
                getattr(self, "_" + op)(arg)
            except Refused as e:
                self.msg.emit(f"[!] {e}", T.AMBER)
            except Exception as e:
                self.msg.emit(f"[!] {op.replace('_', ' ')} failed: {e}", T.RED)
            self._report()
        self._release()

    def _report(self):
        cur = self.t.running if self.t is not None else None
        if cur != self._last:
            if cur is None and self._last and self.t is not None and self.t.ended:
                if self.t.off_confirmed:
                    self.msg.emit(f"[✓] {self._last} {self.t.ended}; all actuators switched off.", T.OK)
                else:
                    self.msg.emit(f"[!] {self._last} {self.t.ended}, but the ECU didn't confirm the switch-off. "
                                  "Key OFF to be sure.", T.RED)
            self._last = cur
            self.running.emit(cur or "")

    def _release(self):
        if self.t is not None:
            try:
                self.t.close()
            except Exception as e:
                self.msg.emit(f"[!] Couldn't confirm the actuators stopped ({e}). Key OFF to be sure.", T.RED)
            self.t = None
            self._last = None
            self.running.emit("")
            self.tests.emit([])

    def _connect(self, _):
        from ssm_tests import open_subaru
        self._release()
        from ssm_tests import NoLink
        self.msg.emit("[*] Connecting to the Subaru engine ECU...", T.MUTED)
        try:
            self.t, variant = open_subaru("kkl", self.port, "", CFG["cache_dir"])
        except NoLink as e:
            if self.quitting:
                raise
            self.msg.emit(f"[*] {e} Trying the OBDLink EX...", T.MUTED)
            try:
                self.t, variant = open_subaru("obdlink", None, CFG.get("ex_ssm_variant", ""), CFG["cache_dir"])
            except NoLink as e2:
                raise NoLink(f"Neither cable got an SSM answer. KKL: {e} EX: {e2}")
        if variant and variant != CFG.get("ex_ssm_variant"):
            CFG["ex_ssm_variant"] = variant
            save_config_key("ex_ssm_variant", variant)
        caps = self.t.caps
        self.msg.emit(f"[✓] ECU {caps.sys_id.hex().upper()} ({caps.engine}), ROM {caps.rom_id.hex().upper()}.", T.OK)
        why = self.t.why_no_tests()
        if why:
            self.msg.emit(f"[!] {why} Clear memory still works.", T.AMBER)
            self.tests.emit([])
            return
        tm, rpm = self.t.test_mode_on(), self.t.engine_rpm()
        self.msg.emit(f"[car] Test-mode connector: {'joined' if tm else 'NOT joined' if tm is not None else 'unknown'}"
                      f" | engine: {'off' if rpm is not None and rpm <= 100 else 'RUNNING' if rpm else 'unknown'}",
                      T.OK if tm and rpm is not None and rpm <= 100 else T.AMBER)
        self.tests.emit(self.t.titles())

    def _need_ecu(self):
        from ssm_tests import Refused
        if self.t is None:
            raise Refused("Connect to the Subaru ECU first.")
        return self.t

    def _start(self, index):
        title = self._need_ecu().start(index)
        self.msg.emit(f"[TEST] {title} is running. STOP ALL ends it; it stops by itself after "
                      f"{__import__('ssm_tests').MAX_TEST_S} s.", T.AMBER)

    def _stop(self, _):
        if self.t is None:
            return
        ok = self.t.stop()
        self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                      "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)

    def _stop_all(self, _):
        ok = self._need_ecu().stop_all()
        self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                      "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)

    def _clear_memory(self, _):
        ok = self._need_ecu().clear_memory()
        self.msg.emit("[✓] ECU memory cleared (ECU confirmed). Key OFF for 10 s, then READ CAR to check." if ok else
                      "[!] The ECU didn't confirm the clear. Read the codes again to see.", T.OK if ok else T.RED)

    def _clear_codes(self, _):
        """OBD-II mode 04 through the OBDLink EX; if that cable doesn't answer, the Subaru SSM memory clear
        through the KKL cable. A safety refusal (engine running) never falls through to the other cable."""
        from ssm_tests import NoLink
        try:
            self._mode04()
        except NoLink as e:
            self.msg.emit(f"[*] {e} Trying the SSM memory clear through the KKL cable...", T.MUTED)
            if self.t is None:
                self._connect(None)
            self._clear_memory(None)

    def _mode04(self):
        from ssm_tests import NoLink, Refused
        from cables import ex_port
        self._release()
        try:
            import obd
        except ImportError:
            raise NoLink("python-obd isn't installed (pip install obd).")
        conn, why = obd_all.connect(ex_port(CFG["obd_port"] if CFG.get("obd_adapter") == "obdlink" else ""))
        if conn is None:
            raise NoLink(f"No OBD-II answer through the OBDLink EX ({why}).")
        try:
            r = conn.query(obd.commands.RPM)
            rpm = None if r.is_null() else float(getattr(r.value, "magnitude", r.value))
            if rpm is None:
                raise Refused("Couldn't read engine speed, so nothing was sent.")
            if rpm > 100:
                raise Refused(f"Engine is running ({rpm:.0f} rpm). Key ON, engine OFF.")
            conn.query(obd.commands.CLEAR_DTC)
            left = conn.query(obd.commands.GET_DTC).value or []
            if left:
                self.msg.emit(f"[!] Clear sent, but codes are still there: {', '.join(c for c, _ in left)}. "
                              "A fault that's still present comes straight back.", T.AMBER)
            else:
                self.msg.emit("[✓] Codes cleared (mode 04); the car now reports none. Emissions readiness "
                              "monitors are reset too and need a drive cycle.", T.OK)
        finally:
            conn.close()
