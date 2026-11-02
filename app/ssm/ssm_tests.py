"""
Subaru two-way tests (TEST MODE): actuator tests and clear memory, over either cable.

Ported from FreeSSM (GPL-3), SSMprotocol.cpp: startActuatorTest, stopActuatorTesting, stopAllActuators,
clearMemory, isEngineRunning, isInTestMode. Same checks, same order, same addresses:
  - Actuator tests need the ECU to advertise them in its 0xBF init reply (ECUCaps.has_actuator_tests),
    the test-mode connector joined (address 0x61 bit 5 set) and the engine not running (RPM <= 100).
  - Start: write 0x00 to every actuator byte, then keep writing the test's bit every 100 ms.
  - Stop: stop the repeats, then write 0x00 to every actuator byte.
  - Clear memory: write 0x40 to 0x60; it worked when the ECU echoes 0x40.

The deck adds two things FreeSSM doesn't have: every write is checked against an allowlist built from
this ECU's own init reply (clear memory, or an actuator byte with 0x00 or one advertised test bit), and a
test stops by itself after MAX_TEST_S. Nothing else can be written: no adjustments, no flashing, no
immobilizer or security access.
"""
import threading
import time

from ssm2_capabilities import ECUCaps, read_addresses

WRITE = 0xB8                      # single-address write; the ECU answers F8 <byte written>
CLEAR_MEMORY = (0x60, 0x40)
TEST_MODE_ADDR, TEST_MODE_MASK = 0x61, 1 << 5
RPM_HIGH, RPM_LOW = 0x0E, 0x0F    # engine speed = (high * 256 + low) / 4
ENGINE_RUNNING_RPM = 100          # FreeSSM: some ECUs don't report 0 with the engine off
REPEAT_S = 0.1                    # FreeSSM writeAddress_permanent delay
MAX_TEST_S = 30
MAX_MISSED = 3                    # repeat writes in a row with no answer before the test is called off
RPM_CHECK_S = 1.0                 # how often a running test re-checks that the engine is still off


class Refused(Exception):
    """A write the allowlist or a safety check doesn't allow. The message says why, for the screen."""


class NoLink(Refused):
    """The cable or the ECU didn't answer: worth trying the other cable (a safety refusal is not)."""


class KKLLink:
    """Bare KKL cable: TelemetryPoller after probe_ecu()."""
    def __init__(self, poller):
        self.p = poller
        self.init_raw = poller.init_raw

    def send_ssm2_command(self, payload):      # reads (read_addresses uses this name)
        return self.p.send_ssm2_command(payload)

    def write(self, payload):
        return self.p._transact(payload)

    def close(self):
        if getattr(self.p, "ser", None) is not None and self.p.ser.is_open:
            self.p.ser.close()


class EXLink:
    """OBDLink EX: ex_ssm2_probe.STNSSM2 after setup() and probe_ecu(). Writes go out as the same STPX
    frame the probe uses for reads (the probe itself stays read-only and is owned by the EX research)."""
    def __init__(self, ex):
        self.ex = ex
        self.init_raw = ex.init_raw

    def send_ssm2_command(self, payload):
        return self.ex.send_ssm2_command(payload)

    def write(self, payload, timeout_ms=500):
        ex = self.ex
        if ex.raw:
            body = bytes([0x80, 0x10, 0xF0, len(payload)]) + payload
            frame = body + bytes([sum(body) & 0xFF])
            lines = ex.cmd(f"STPX d:{frame.hex().upper()}, t:{timeout_ms}, r:1", timeout_ms / 1000 + 2.0)
        else:
            lines = ex.cmd(f"STPX h:8010F0, d:{payload.hex().upper()}, t:{timeout_ms}, r:1", timeout_ms / 1000 + 2.0)
        return ex._parse(lines, payload)

    def close(self):
        try:
            self.ex.close()
        except Exception:
            pass


class SubaruTests:
    def __init__(self, link, caps=None):
        self.link = link
        self.caps = caps or ECUCaps(link.init_raw)
        self.actuators = self.caps.actuator_tests_reference()
        self.act_addrs = sorted({a["addr"] for a in self.actuators})
        self._allowed = {CLEAR_MEMORY} | {(a, 0x00) for a in self.act_addrs} | \
                        {(a["addr"], 1 << (a["addr_bit"] - 1)) for a in self.actuators}
        self._lock = threading.Lock()         # one frame on the K-line at a time
        self._stop = threading.Event()
        self._thread = None
        self.running = None                   # title of the test running now
        self.ended = ""                       # why the last test ended, if it ended by itself
        self.off_confirmed = True             # whether the ECU confirmed the switch-off when it ended by itself

    # ------------------------------------------------------------------ what this ECU allows
    @property
    def uses_flagbytes(self):
        return (self.caps.sys_id[0] & 0xF0) == 0xA0

    def why_no_tests(self):
        if not self.uses_flagbytes:
            return f"SYS ID {self.caps.sys_id.hex().upper()} is an older ECU type the deck has no test table for."
        if not self.caps.has_actuator_tests or not self.actuators:
            return "This ECU doesn't advertise actuator tests in its init reply."
        return ""

    def titles(self):
        return [a["title"] for a in self.actuators]

    # ------------------------------------------------------------------ frames
    def _write(self, addr, value):
        """One allowlisted single-byte write. Returns the byte the ECU says it wrote, or None."""
        if (addr, value) not in self._allowed:
            raise Refused(f"write 0x{value:02X} to 0x{addr:06X} is not a test this ECU advertises")
        payload = bytes([WRITE]) + addr.to_bytes(3, "big") + bytes([value])
        with self._lock:
            reply = self.link.write(payload)
        if len(reply) >= 2 and reply[0] == 0xF8:
            return reply[1]
        return None

    def _read(self, addrs):
        with self._lock:
            return read_addresses(self.link, addrs)

    # ------------------------------------------------------------------ checks
    def test_mode_on(self):
        v = self._read([TEST_MODE_ADDR])
        return None if not v else bool(v[TEST_MODE_ADDR] & TEST_MODE_MASK)

    def engine_rpm(self):
        v = self._read([RPM_HIGH, RPM_LOW])
        return None if not v else (v[RPM_HIGH] * 256 + v[RPM_LOW]) / 4

    def _check_engine_off(self):
        rpm = self.engine_rpm()
        if rpm is None:
            raise Refused("Couldn't read engine speed from the ECU, so nothing was sent.")
        if rpm > ENGINE_RUNNING_RPM:
            raise Refused(f"Engine is running ({rpm:.0f} rpm). Key ON, engine OFF.")

    def _check_test_mode(self):
        if not self.caps.has_test_mode:
            return  # FreeSSM: the ECU would advertise the test-mode switch if it mattered
        on = self.test_mode_on()
        if on is None:
            raise Refused("Couldn't read the test-mode state from the ECU, so nothing was sent.")
        if not on:
            raise Refused("Test-mode connector isn't joined (the green connectors under the dash). "
                          "Join them with the key OFF, then key ON, engine OFF.")

    def preflight(self):
        why = self.why_no_tests()
        if why:
            raise Refused(why)
        self._check_test_mode()
        self._check_engine_off()

    # ------------------------------------------------------------------ actuator tests
    def _zero_all(self):
        ok = True
        for a in self.act_addrs:
            ok = self._write(a, 0x00) is not None and ok
        return ok

    def start(self, index, max_s=MAX_TEST_S):
        """Run one actuator test until stop() or max_s. Raises Refused when a check fails."""
        if self.running:
            self.stop()
        if not 0 <= index < len(self.actuators):
            raise Refused("No such test on this ECU.")
        self.preflight()
        act = self.actuators[index]
        addr, bit = act["addr"], 1 << (act["addr_bit"] - 1)
        if not self._zero_all():
            self._zero_all()
            raise Refused("The ECU didn't confirm the stop-all write, so the test wasn't started.")
        if self._write(addr, bit) is None:
            self._zero_all()
            raise Refused("The ECU didn't confirm the test write. Nothing is running.")
        self._stop.clear()
        self.running, self.ended = act["title"], ""
        self._thread = threading.Thread(target=self._repeat, args=(addr, bit, max_s), daemon=True)
        self._thread.start()
        return act["title"]

    def _repeat(self, addr, bit, max_s):
        end = time.monotonic() + max_s
        next_rpm = time.monotonic() + RPM_CHECK_S
        missed = 0
        while not self._stop.wait(REPEAT_S):
            if time.monotonic() >= end:
                self.ended = f"stopped by itself after {max_s} s"
                break
            if time.monotonic() >= next_rpm:  # someone started the engine mid-test: stop
                next_rpm = time.monotonic() + RPM_CHECK_S
                try:
                    rpm = self.engine_rpm()
                except Exception:
                    rpm = None
                if rpm is not None and rpm > ENGINE_RUNNING_RPM:
                    self.ended = f"stopped: the engine started ({rpm:.0f} rpm)"
                    break
            try:
                missed = 0 if self._write(addr, bit) is not None else missed + 1
            except Exception:
                missed = MAX_MISSED
            if missed >= MAX_MISSED:
                self.ended = "stopped: the ECU stopped answering"
                break
        if not self._stop.is_set():   # ended by itself: switch the actuator off here, and say if that failed
            try:
                ok = self._zero_all()
            except Exception:
                ok = False
            self.off_confirmed = ok
            self.running = None

    def stop(self):
        """Stop the running test (if any) and write 0x00 to every actuator byte. True if the ECU confirmed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self.running = None
        return self._zero_all() if self.act_addrs else True

    def stop_all(self):
        """FreeSSM stopAllActuators: some actuators start by themselves at key-on with the connector joined.
        No checks first: switching everything off is always allowed (only 0x00 writes, all allowlisted)."""
        return self.stop()

    # ------------------------------------------------------------------ clear memory
    def clear_memory(self):
        """Erase the ECU's stored codes and learned values. Engine must be off. True if the ECU confirmed."""
        if self.running:
            self.stop()
        if not self.uses_flagbytes:
            raise Refused(f"SYS ID {self.caps.sys_id.hex().upper()} is an older ECU type the deck has no clear-memory data for.")
        self._check_engine_off()
        return self._write(*CLEAR_MEMORY) == CLEAR_MEMORY[1]

    def close(self):
        try:
            if self.running:
                self.stop()
        finally:
            self.link.close()


def open_subaru(adapter, port, variant_name="", cache_dir=None):
    """Connect to the engine ECU for tests. Returns (SubaruTests, variant used) or raises Refused."""
    import glob
    import os
    from cables import candidates
    if adapter == "kkl":
        from telemetry_logger import TelemetryPoller
        ports = candidates("kkl", port)
        for attempt in range(2):  # the first try after plugging in often gets no answer
            for p in ports:
                try:
                    poller = TelemetryPoller(port=p)
                except Exception:
                    continue
                if poller.probe_ecu():
                    return SubaruTests(KKLLink(poller)), ""
                KKLLink(poller).close()
        raise NoLink(f"No SSM answer on the K-line (tried {', '.join(ports)} twice). Key ON? KKL cable plugged in?")
    import ex_ssm2_probe as ex_mod
    port = port or candidates("ex")[0]
    if not glob.glob("/dev/ttyUSB*"):
        raise NoLink("No /dev/ttyUSB* found. Is the OBDLink EX plugged in?")
    log_path = os.path.join(cache_dir or ".", "ex_ssm_tests.log")
    ex = ex_mod.STNSSM2(port, ex_mod.Log(log_path))
    if not ex.open():
        raise NoLink("OBDLink EX didn't answer on the serial port.")
    for v in sorted(ex_mod.VARIANTS, key=lambda v: v["name"] != variant_name):
        ex.setup(v)
        if ex.probe_ecu():
            return SubaruTests(EXLink(ex)), v["name"]
    err = ex.last_error
    EXLink(ex).close()
    raise NoLink(f"No SSM answer through the EX ({err}). Key ON? Is it a Subaru?")
