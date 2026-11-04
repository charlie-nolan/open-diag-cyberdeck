#!/usr/bin/env python3
"""
Can the OBDLink EX talk Subaru SSM2 to the 2002 Forester?  Run this to find out.

The EX is not a raw cable: it has an STN2230 command interpreter in front of the
K-line. SSM2 happens to use the same frame layout as ISO 14230 (KWP2000) with a
4-byte header, so the STN can carry it if it is told:

    STP 23          ISO 14230-4, no automatic init (SSM2 has no 5-baud/fast init)
    STPBR 4800      K-line at 4800 baud (ATIB 48 is the deprecated fallback)
    ATSH 80 10 F0   format 0x80 -> STN adds the length byte as header byte 4
    STPCB 1         STN adds/checks the 8-bit sum checksum = the SSM2 checksum
    ATSW 00         no keep-alives (an OBD keep-alive would be garbage to SSM2)
    ATAL, ATH1      allow long replies, print the reply header

Each attempt is logged, every byte in and out, to ex_probe_<time>.log so the
result can be read back even if it fails.

READ-ONLY. The only SSM2 commands this file can send are 0xBF (ECU init / ID)
and 0xA8 (read addresses). send_ssm2_command() refuses anything else.

Usage (key ON, engine off or idling; unplug the KKL cable first):
    python3 ex_ssm2_probe.py                    # auto-find /dev/ttyUSB* speed
    python3 ex_ssm2_probe.py --port /dev/ttyUSB0 --ssm-dir app/ssm

STNSSM2 has the same send_ssm2_command()/probe_ecu()/init_raw interface as
TelemetryPoller, so ssm2_capabilities.read_dtcs() works on it unchanged.
"""
import argparse
import glob
import os
import re
import sys
import time

import serial

ALLOWED_SSM2 = {0xBF, 0xA8}          # read-only: init and address reads
HOST_BAUDS = [115200, 38400, 9600, 230400, 500000, 2000000]
HEX_LINE = re.compile(r"^[0-9A-F]+$")
STN_ERRORS = ("NO DATA", "?", "BUS ERROR", "DATA ERROR", "BUFFER FULL", "STOPPED",
              "BUS INIT", "BUS BUSY", "FB ERROR", "ACT ALERT", "LV RESET", "OUT OF MEMORY",
              "UNABLE TO CONNECT")


class Log:
    def __init__(self, path):
        self.f = open(path, "w")
        self.path = path

    def __call__(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line)
        self.f.write(line + "\n")
        self.f.flush()


class STNSSM2:
    def __init__(self, port, log, host_baud=None):
        self.port = port
        self.log = log
        self.host_baud = host_baud
        self.ser = None
        self.init_raw = b""
        self.rom_id = ""
        self.ssm_id = ""
        self.ecu_online = False
        self.last_error = ""
        self.raw = False           # True: STN adds nothing, we build the whole SSM2 frame

    # ------------------------------------------------------------- STN plumbing
    def cmd(self, text, timeout=2.0):
        """Send one STN/AT command, return the lines before the '>' prompt."""
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode())
        buf = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.endswith(b">"):
                    break
        lines = [l.strip() for l in buf.decode(errors="replace").replace(">", "").split("\r")]
        lines = [l for l in lines if l and l != text]          # drop echo if ATE1
        self.log(f">> {text}   << {' | '.join(lines) if lines else '(nothing)'}")
        return lines

    def open(self):
        bauds = [self.host_baud] if self.host_baud else HOST_BAUDS
        for b in bauds:
            try:
                try:  # app copy: exclusive, so nothing else shares the port (pyserial >= 3.3)
                    self.ser = serial.Serial(self.port, b, timeout=0.05, exclusive=True)
                except TypeError:
                    self.ser = serial.Serial(self.port, b, timeout=0.05)
            except serial.SerialException as e:
                self.log(f"cannot open {self.port}: {e}")
                return False
            time.sleep(0.1)
            self.ser.write(b"\r")
            time.sleep(0.2)
            self.ser.reset_input_buffer()
            if any("ELM" in l or "STN" in l for l in self.cmd("ATI", 1.0)):
                self.log(f"adapter answered at host baud {b}")
                return True
            self.ser.close()
        self.log("no ELM/STN adapter answered on any host baud")
        return False

    def identify(self):
        info = {}
        for c in ("ATI", "STI", "STDI", "STSN", "STMFR", "ATRV"):
            info[c] = " ".join(self.cmd(c))
        return info

    def setup(self, variant):
        """variant: dict of knobs the ladder in main() tries in turn."""
        self.cmd("ATZ", 3.0)
        time.sleep(0.5)
        for c in ("ATE0", "ATL0", "ATS0", "ATH1", "ATAL", "ATSW 00", "ATAT0", "STFAC"):  # ATAT0: fixed STPTO wait, slow 4800-baud replies
            self.cmd(c)
        self.cmd(f"STP {variant['proto']}")
        r = self.cmd("STPBR 4800")
        if not r or r[0] == "?":
            self.cmd("ATIB 48")
        self.cmd("STPBRR")
        self.raw = bool(variant.get("raw"))
        if self.raw:
            self.cmd("STPCB 0")          # no STN checksum: our frame already carries one
        else:
            self.cmd("STPCB 1")
            self.cmd("ATSH 80 10 F0")
        if variant.get("ta"):
            self.cmd("ATTA F0")
        if variant.get("filter"):
            self.cmd("STFPA 80F010, FFFFFF")
        self.cmd(f"STPTO {variant.get('timeout_ms', 500)}")
        self.cmd(f"STIP1X {variant.get('p1x', 40)}")
        self.cmd(f"STPTRQ {variant.get('p3', 20)}")
        self.cmd("STPO", 3.0)

    # ------------------------------------------------------------- SSM2 frames
    def _parse(self, lines, sent_payload):
        """Turn STN output lines into the SSM2 reply payload (after the length byte)."""
        hexes = [l.replace(" ", "") for l in lines if HEX_LINE.match(l.replace(" ", ""))]
        errs = [l for l in lines if any(e in l for e in STN_ERRORS)]
        if not hexes:
            self.last_error = errs[0] if errs else "no hex reply"
            return b""
        raw = bytes.fromhex("".join(hexes))
        # Find the ECU's frame. Header shown because ATH1: 80 F0 10 LL ...
        i = raw.find(b"\x80\xF0\x10")
        if i < 0:
            self.last_error = f"no 80 F0 10 header in {raw.hex(' ')}"
            return b""
        frame = raw[i:]
        n = frame[3]
        payload = frame[4:4 + n]
        if len(payload) < n:
            self.last_error = f"short reply: length byte {n}, got {len(payload)} ({frame.hex(' ')})"
            return b""
        # STN may or may not print the checksum byte; check it if it is there
        if len(frame) > 4 + n and frame[4 + n] != sum(frame[:4 + n]) & 0xFF:
            self.last_error = "checksum mismatch"
            return b""
        return payload

    def send_ssm2_command(self, payload: bytes, timeout_ms=500) -> bytes:
        if not payload or payload[0] not in ALLOWED_SSM2:
            raise ValueError(f"refusing SSM2 command 0x{payload[:1].hex()}: deck is read-only")
        if self.raw:
            body = bytes([0x80, 0x10, 0xF0, len(payload)]) + payload
            frame = body + bytes([sum(body) & 0xFF])
            lines = self.cmd(f"STPX d:{frame.hex().upper()}, t:{timeout_ms}, r:1",
                             timeout_ms / 1000 + 2.0)
        else:
            lines = self.cmd(f"STPX h:8010F0, d:{payload.hex().upper()}, t:{timeout_ms}, r:1",
                             timeout_ms / 1000 + 2.0)
        return self._parse(lines, payload)

    def probe_ecu(self):
        resp = self.send_ssm2_command(bytes([0xBF]), 800)
        self.init_raw = resp
        if resp and len(resp) >= 9 and resp[0] == 0xFF:
            self.ecu_online = True
            self.ssm_id = resp[1:4].hex().upper()
            self.rom_id = resp[4:9].hex().upper()
            return self.rom_id
        return ""

    def close(self):
        if self.ser and self.ser.is_open:
            self.cmd("ATPC")          # close protocol
            self.ser.close()


VARIANTS = [
    {"name": "ISO14230 no-init, defaults", "proto": 23},
    # Raw: ISO 9141 preset with no header and no checksum, so the STN sends our
    # complete SSM2 frame byte for byte (sidesteps the ELM length-byte problem).
    {"name": "raw frame, ISO9141 no-header, checksum off", "proto": 21, "raw": True},
    {"name": "ISO14230 + tester addr F0", "proto": 23, "ta": True},
    {"name": "ISO14230 + pass filter 80F010", "proto": 23, "ta": True, "filter": True},
    {"name": "ISO14230 slow ECU timing", "proto": 23, "ta": True, "timeout_ms": 1500, "p1x": 100, "p3": 60},
]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="default: first /dev/ttyUSB*")
    ap.add_argument("--host-baud", type=int)
    ap.add_argument("--ssm-dir", default=os.path.dirname(os.path.abspath(__file__)),
                    help="folder with ssm2_capabilities.py (for DTC/switch decoding)")
    ap.add_argument("--rate-test", type=int, default=20, help="timed live reads (0 = skip)")
    args = ap.parse_args()

    port = args.port or (sorted(glob.glob("/dev/ttyUSB*")) or [None])[0]
    if not port:
        sys.exit("No /dev/ttyUSB* found. Is the EX plugged in? (dmesg | grep -i ftdi)")
    log = Log(f"ex_probe_{time.strftime('%Y%m%d_%H%M%S')}.log")
    ex = STNSSM2(port, log, args.host_baud)
    if not ex.open():
        sys.exit(1)
    log(f"adapter: {ex.identify()}")

    # Generic OBD-II identity first (read-only Mode 01/09 requests): which protocol
    # answers, which OBD standard the ECU claims, and whatever Mode 09 it supports
    # (VIN 02, calibration ID 04, CVN 06, ECU name 0A). Mode 09 is optional before MY2005.
    log("=== generic OBD-II identity ===")
    ex.cmd("ATZ", 3.0)
    for c in ("ATE0", "ATH1", "ATSP0"):
        ex.cmd(c)
    ex.cmd("0100", 12.0)                 # auto-detect protocol (5-baud init can take ~5 s)
    for c in ("ATDP", "ATDPN", "011C", "0900", "0902", "0904", "0906", "090A"):
        ex.cmd(c, 5.0)

    for v in VARIANTS:
        log(f"=== attempt: {v['name']} ===")
        ex.setup(v)
        rom = ex.probe_ecu()
        if rom:
            log(f"*** SSM2 INIT OK with '{v['name']}': SYS ID {ex.ssm_id} ROM ID {rom}, "
                f"{len(ex.init_raw)} payload bytes")
            break
        log(f"init failed: {ex.last_error}")
    else:
        log("RESULT: no SSM2 init reply through the EX on any variant. Send this log back.")
        ex.close()
        return

    with open("ecu_init_reply_ex.hex", "w") as f:
        f.write(ex.init_raw.hex(" ").upper() + "\n")

    # DTCs and switches through FreeSSM's tables, 0xA8 only
    sys.path.insert(0, args.ssm_dir)
    try:
        import ssm2_capabilities as caps_mod
        caps = caps_mod.ECUCaps(ex.init_raw)
        log(caps.report())
        for chunk in (32, 8):
            vals = caps_mod.read_addresses(ex, [a for p in caps.dtc_address_pairs() for a in p], chunk)
            log(f"A8 batch of {chunk} addresses: {'OK' if vals else 'FAILED (' + ex.last_error + ')'}")
            if vals:
                break
        dtcs = caps_mod.read_dtcs(ex, caps)
        log(f"DTCs: {dtcs}")
    except ImportError:
        log(f"ssm2_capabilities.py not found in {args.ssm_dir}; skipped DTC decode")

    # Live data rate: RPM, ECT, IAT, TPS, battery, misfire counters (13 bytes)
    addrs = [0x08, 0x0E, 0x0F, 0x12, 0x15, 0x1C, 0x09, 0x0A, 0x11, 0xCE, 0xCF, 0xD8, 0xD9]
    payload = bytes([0xA8, 0x00]) + b"".join(a.to_bytes(3, "big") for a in addrs)
    ok, t0 = 0, time.monotonic()
    for _ in range(args.rate_test):
        d = ex.send_ssm2_command(payload)
        if d and d[0] == 0xE8:
            ok += 1
            last = d
    dt = time.monotonic() - t0
    if args.rate_test:
        log(f"live read: {ok}/{args.rate_test} good, {args.rate_test / dt:.1f} reads/s for {len(addrs)} bytes")
        if ok:
            log(f"last: ECT {last[1] - 40} C, RPM {((last[2] << 8) | last[3]) // 4}, "
                f"IAT {last[4] - 40} C, battery {last[6] * 8 / 100:.2f} V")
    log(f"RESULT: EX SSM2 works. Log: {log.path}")
    ex.close()


if __name__ == "__main__":
    main()
