#!/usr/bin/env python3
"""
gm_live.py: read-only live data for a GM car through the OBDLink EX (STN2230), no dealer tool needed.

What it reads (requests only; nothing is ever written, cleared or actuated):
  - generic OBD-II (SAE J1979): Mode 01 live PIDs, 02 freeze frame, 03 stored DTCs, 07 pending DTCs, 09 VIN/CALID
  - GM enhanced Mode $22 PIDs from a sourced JSON file (kb/gm_mode22_pids.json, built by
    `make_pipeline.py PROFILE gm-pids <sheet.xlsx>` from the gmtnation community sheet). Those are community
    findings, not GM data: each one is shown as "unverified" until it has been checked against the car.

Mode 04 (clear codes), $3B/$AE/$2F (writes / device control) and anything else outside ALLOWED_SERVICES raise
before a byte is sent. Protocol defaults to auto; a 2003 GM truck answers J1850 VPW (STN protocol 2).

  python3 gm_live.py scan                       identity, supported PIDs, VIN, DTCs (writes a log)
  python3 gm_live.py live --pids 0C,05,0F,ACRequestSignal,221940 --seconds 60 --csv run.csv   (Mode 22 by PCMHammer id or PID)
  python3 gm_live.py scan --fake                offline test against a simulated VPW truck
"""
import argparse
import ast
import csv
import glob
import json
import operator
import os
import re
import sys
import time

ALLOWED_SERVICES = {0x01, 0x02, 0x03, 0x07, 0x09, 0x22}      # read-only services
ALLOWED_AT = re.compile(r"^(ATZ|ATI|ATE[01]|ATH[01]|ATS[01]|ATL[01]|ATSP[0-9A-C]|ATDPN?|ATRV|ATAT[0-2]|ATST[0-9A-F]{2}|"
                        r"ATSH(?:[0-9A-F]{6}|[0-9A-F]{3})|ATCAF[01]|STI|STDI|STSN|STMFR)$")
HOST_BAUDS = [115200, 38400, 9600, 230400, 500000, 2000000]
ERRORS = ("NO DATA", "?", "BUS ERROR", "DATA ERROR", "BUFFER FULL", "STOPPED", "BUS INIT", "BUS BUSY",
          "FB ERROR", "CAN ERROR", "UNABLE TO CONNECT", "ERROR")
# Default (functional) request headers, so a Mode 22 header can be switched back without a reset.
DEFAULT_HEADER = {"1": "616AF1", "2": "686AF1", "3": "686AF1", "4": "C133F1", "5": "C133F1", "6": "7DF", "8": "7DF"}
PROTOCOLS = {"1": "J1850 PWM", "2": "J1850 VPW", "3": "ISO 9141-2", "4": "ISO 14230 5-baud", "5": "ISO 14230 fast",
             "6": "CAN 11/500", "7": "CAN 29/500", "8": "CAN 11/250", "9": "CAN 29/250"}

# SAE J1979 Mode 01 PIDs that pre-CAN GM PCMs commonly support. (name, units, equation over A,B)
MODE01 = {
    "04": ("Calculated load", "%", "A*100/255"),
    "05": ("Coolant temp", "C", "A-40"),
    "06": ("Short term fuel trim B1", "%", "(A-128)*100/128"),
    "07": ("Long term fuel trim B1", "%", "(A-128)*100/128"),
    "08": ("Short term fuel trim B2", "%", "(A-128)*100/128"),
    "09": ("Long term fuel trim B2", "%", "(A-128)*100/128"),
    "0A": ("Fuel pressure (gauge)", "kPa", "A*3"),
    "0B": ("Intake manifold pressure", "kPa", "A"),
    "0C": ("Engine RPM", "rpm", "(A*256+B)/4"),
    "0D": ("Vehicle speed", "km/h", "A"),
    "0E": ("Timing advance", "deg", "A/2-64"),
    "0F": ("Intake air temp", "C", "A-40"),
    "10": ("MAF air flow", "g/s", "(A*256+B)/100"),
    "11": ("Throttle position", "%", "A*100/255"),
    "14": ("O2 B1S1 voltage", "V", "A/200"),
    "15": ("O2 B1S2 voltage", "V", "A/200"),
    "16": ("O2 B1S3 voltage", "V", "A/200"),
    "18": ("O2 B2S1 voltage", "V", "A/200"),
    "19": ("O2 B2S2 voltage", "V", "A/200"),
    "1A": ("O2 B2S3 voltage", "V", "A/200"),
    "1C": ("OBD standard", "", "A"),
    "1F": ("Run time since start", "s", "A*256+B"),
    "21": ("Distance with MIL on", "km", "A*256+B"),
    "2F": ("Fuel level", "%", "A*100/255"),
    "33": ("Barometric pressure", "kPa", "A"),
    "42": ("Control module voltage", "V", "(A*256+B)/1000"),
    "46": ("Ambient air temp", "C", "A-40"),
}
OBD_STANDARD = {1: "OBD-II (CARB)", 2: "OBD (EPA)", 3: "OBD and OBD-II", 4: "OBD-I", 5: "not OBD", 6: "EOBD",
                7: "EOBD and OBD-II", 9: "EOBD, OBD and OBD-II", 13: "JOBD, EOBD and OBD-II"}


# ------------------------------------------------------------------ Torque-style equations, evaluated safely
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.BitAnd: operator.and_, ast.BitOr: operator.or_,
        ast.RShift: operator.rshift, ast.LShift: operator.lshift, ast.USub: operator.neg, ast.UAdd: operator.pos}


def compile_equation(eq):
    """'(A*256+B)/4', '{A:7}', 'SIGNED(A)*0.5' -> function(bytes) -> float. Only arithmetic on A..H is allowed."""
    src = re.sub(r"\{([A-H]):(\d)\}", r"((\1>>\2)&1)", eq.strip())
    src = re.sub(r"\bSIGNED16\(", "_signed16(", src, flags=re.I)
    src = re.sub(r"\bSIGNED\(", "_signed(", src, flags=re.I)
    tree = ast.parse(src, mode="eval")

    def ev(n, env):
        if isinstance(n, ast.Expression):
            return ev(n.body, env)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name) and n.id in env:
            return env[n.id]
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left, env), ev(n.right, env))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand, env))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("_signed", "_signed16") \
                and len(n.args) == 1:
            v, bits = int(ev(n.args[0], env)), (8 if n.func.id == "_signed" else 16)
            return v - (1 << bits) if v >= 1 << (bits - 1) else v
        raise ValueError(f"unsupported in equation {eq!r}: {ast.dump(n)[:60]}")

    letters = sorted({x for x in re.findall(r"\b([A-H])\b", src)})
    need = (ord(letters[-1]) - 64) if letters else 0
    ev(tree, {k: 1 for k in "ABCDEFGH"})          # reject bad equations at load time, not mid-drive

    def f(data):
        if len(data) < need:
            raise ValueError(f"needs {need} bytes, got {len(data)}")
        return float(ev(tree, {chr(65 + i): (data[i] if i < len(data) else 0) for i in range(8)}))
    f.nbytes = need
    return f


def decode_dtc(b1, b2):
    return "PCBU"[b1 >> 6] + str((b1 >> 4) & 3) + f"{b1 & 0xF:X}{b2:02X}"


# ------------------------------------------------------------------ adapter
class ReadOnlyError(Exception):
    pass


class EX:
    def __init__(self, ser, log):
        self.ser, self.log = ser, log
        self.proto = None             # STN protocol number as text, e.g. '2'
        self.header = None

    @classmethod
    def open(cls, port, log, host_baud=None):
        import serial
        for b in ([host_baud] if host_baud else HOST_BAUDS):
            try:
                ser = serial.Serial(port, b, timeout=0.05)
            except serial.SerialException as e:
                sys.exit(f"cannot open {port}: {e}")
            ex = cls(ser, log)
            time.sleep(0.1)
            ser.write(b"\r")
            time.sleep(0.2)
            ser.reset_input_buffer()
            if any("ELM" in l or "STN" in l for l in ex.at("ATI", 1.0)):
                log(f"adapter answered at host baud {b}")
                return ex
            ser.close()
        sys.exit("no ELM/STN adapter answered on any host baud")

    def _send(self, text, timeout):
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode())
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.endswith(b">"):
                    break
        lines = [l.strip() for l in buf.decode(errors="replace").replace(">", "").split("\r")]
        lines = [l for l in lines if l and l != text and l.upper() != "SEARCHING..."]
        self.log(f">> {text}   << {' | '.join(lines) if lines else '(nothing)'}")
        return lines

    def at(self, cmd, timeout=2.0):
        c = cmd.replace(" ", "").upper()
        if not ALLOWED_AT.match(c):
            raise ReadOnlyError(f"adapter command {cmd!r} is not on the read-only list")
        return self._send(c, timeout)

    def request(self, hexmsg, timeout=3.0, header=None):
        """Send one OBD request (hex, e.g. '010C' or '221205'). Returns a list of (header_hex, data_bytes)."""
        h = hexmsg.replace(" ", "").upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2})+", h):
            raise ValueError(f"bad request {hexmsg!r}")
        svc = int(h[:2], 16)
        if svc not in ALLOWED_SERVICES:
            raise ReadOnlyError(f"service ${svc:02X} is not read-only (allowed: "
                                + ", ".join(f"${s:02X}" for s in sorted(ALLOWED_SERVICES)) + ")")
        want = header or "AUTO"
        if want != self.header:
            prev, self.header = self.header, want
            if header:
                self.at("ATSH" + header)
            elif prev not in (None, "AUTO"):
                if self.proto in DEFAULT_HEADER:
                    self.at("ATSH" + DEFAULT_HEADER[self.proto])
                else:                        # 29-bit CAN etc.: reset to the adapter's defaults
                    self.at("ATZ", 3.0)
                    self.setup(self.proto or "0")
        return self.parse(self._send(h, timeout), svc + 0x40)

    def setup(self, proto="0"):
        self.header = "AUTO"
        for c in ("ATE0", "ATL0", "ATS1", "ATH1", f"ATSP{proto}", "ATAT1"):
            self.at(c)
        first = self.request("0100", 12.0)              # auto search can take several seconds
        dpn = "".join(self.at("ATDPN")).lstrip("A")
        self.proto = dpn[:1] if dpn[:1] in PROTOCOLS else None
        self.header = "AUTO"
        return first

    def parse(self, lines, resp_sid):
        """Lines with headers on -> [(sender, payload)] where payload starts at the response SID.
        J1850/ISO frames: 3 header bytes + data + 1 checksum. CAN 11-bit: 'ID PCI data' (ISO-TP joined)."""
        out, can = [], {}
        for ln in lines:
            if any(ln.upper().startswith(e) for e in ERRORS):
                continue
            tok = ln.split()
            if not tok or not all(re.fullmatch(r"[0-9A-F]{2,3}", t) for t in tok):
                continue
            if len(tok[0]) == 3:                         # CAN 11-bit
                ident, b = tok[0], [int(t, 16) for t in tok[1:]]
                if not b:
                    continue
                kind = b[0] >> 4
                if kind == 0:
                    out.append((ident, b[1:1 + (b[0] & 0xF)]))
                elif kind == 1:
                    can[ident] = [((b[0] & 0xF) << 8) | b[1], b[2:]]
                elif kind == 2 and ident in can:
                    can[ident][1] += b[1:]
                continue
            b = [int(t, 16) for t in tok]
            if len(b) < 5:
                continue
            out.append((f"{b[0]:02X}{b[1]:02X}{b[2]:02X}", b[3:-1]))
        for ident, (n, data) in can.items():
            out.append((ident, data[:n]))
        return [(s, d) for s, d in out if d and d[0] == resp_sid]

    def close(self):
        self.ser.close()


# ------------------------------------------------------------------ reads
class GMLive:
    def __init__(self, ex, pid_file=None, log=print):
        self.ex, self.log = ex, log
        self.mode22 = {}
        if pid_file and os.path.exists(pid_file):
            doc = json.load(open(pid_file))
            for e in doc.get("pids", []):
                try:
                    e["_f"] = compile_equation(e["equation"])
                except Exception as err:
                    self.log(f"pid {e.get('pid')}: equation skipped ({err})")
                    continue
                self.mode22[(e.get("key") or e["pid"]).upper()] = e
                self.mode22.setdefault(e["pid"].upper(), e)      # bare PID -> its first entry

    def supported01(self):
        sup = set()
        for base in (0x00, 0x20, 0x40):
            if base and f"{base:02X}" not in sup:
                break
            for _, d in self.ex.request(f"01{base:02X}"):
                if len(d) >= 6 and d[1] == base:
                    bits = int.from_bytes(bytes(d[2:6]), "big")
                    sup |= {f"{base + i + 1:02X}" for i in range(32) if bits >> (31 - i) & 1}
        return sup

    def dtcs(self, mode="03"):
        codes = set()
        for _, d in self.ex.request(mode):
            body = d[1:]
            if len(body) % 2 == 1:               # CAN: count byte first
                body = body[1:]
            for i in range(0, len(body) - 1, 2):
                if body[i] or body[i + 1]:
                    codes.add(decode_dtc(body[i], body[i + 1]))
        return sorted(codes)

    def vin(self):
        frames = self.ex.request("0902", 5.0)
        if not frames:
            return None
        data = []
        if len(frames) == 1:                     # CAN: one joined ISO-TP message, '49 02 01' + 17 chars
            data = frames[0][1][3:]
        else:                                    # J1850/ISO: '49 02 NN' + 4 bytes per frame
            for _, d in sorted(frames, key=lambda f: f[1][2] if len(f[1]) > 2 else 0):
                data += d[3:7]
        txt = bytes(b for b in data if 32 < b < 127).decode("ascii", "replace")
        return txt[-17:] if len(txt) >= 17 else (txt or None)

    def read01(self, pid):
        name, units, eq = MODE01[pid]
        f = compile_equation(eq)
        for sender, d in self.ex.request("01" + pid):
            if len(d) >= 2 and f"{d[1]:02X}" == pid:
                return name, f(d[2:]), units, sender
        return name, None, units, None

    def read22(self, pid):
        e = self.mode22.get(pid.upper())
        if not e:
            raise KeyError(f"{pid}: not in the Mode 22 PID file")
        pid = e["pid"]
        hi, lo = int(pid[2:4], 16), int(pid[4:6], 16)
        for sender, d in self.ex.request(pid, header=e.get("header")):
            if len(d) >= 3 and d[1] == hi and d[2] == lo:
                try:
                    return e["name"], e["_f"](d[3:]), e.get("units", ""), sender
                except ValueError:
                    break
        return e["name"], None, e.get("units", ""), None

    def read(self, pid):
        pid = pid.upper()
        return self.read01(pid) if pid in MODE01 else self.read22(pid)

    def scan(self):
        r = {"adapter": " ".join(self.ex.at("STI") or self.ex.at("ATI")), "battery": " ".join(self.ex.at("ATRV"))}
        self.ex.setup("0")
        r["protocol"] = PROTOCOLS.get(self.ex.proto, self.ex.proto)
        r["supported_01"] = sorted(self.supported01())
        if "1C" in r["supported_01"]:
            v = self.read01("1C")[1]
            r["obd_standard"] = OBD_STANDARD.get(int(v), v) if v is not None else None
        r["vin"] = self.vin()
        r["dtc_stored"] = self.dtcs("03")
        r["dtc_pending"] = self.dtcs("07")
        return r


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scan", "live"])
    ap.add_argument("--port")
    ap.add_argument("--host-baud", type=int)
    ap.add_argument("--pids", default="0C,0D,05,0F,04,11,06,07,08,09,0B,0E")
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--csv")
    ap.add_argument("--pid-file", default=os.path.expanduser("~/subaru_cyberdeck/kb/gm_mode22_pids.json"))
    ap.add_argument("--logdir", default=os.path.expanduser("~/subaru_cyberdeck/logs"))
    ap.add_argument("--fake", action="store_true", help="simulated J1850 VPW truck (tests/fake_vpw.py)")
    a = ap.parse_args()
    os.makedirs(a.logdir, exist_ok=True)
    logf = open(os.path.join(a.logdir, f"gm_live_{time.strftime('%Y%m%d_%H%M%S')}.log"), "w")

    def log(m):
        logf.write(f"{time.strftime('%H:%M:%S')} {m}\n")
        logf.flush()

    if a.fake:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
        from fake_vpw import FakeVPW
        ex = EX(FakeVPW(), log)
    else:
        port = a.port or (sorted(glob.glob("/dev/ttyUSB*")) or [None])[0]
        if not port:
            sys.exit("No /dev/ttyUSB* found. Is the EX plugged in?")
        ex = EX.open(port, log, a.host_baud)
    gm = GMLive(ex, a.pid_file, log)
    if a.cmd == "scan":
        r = gm.scan()
        for k, v in r.items():
            print(f"{k:13} {v}")
        print(f"mode22 PIDs   {len({id(e) for e in gm.mode22.values()})} loaded from {a.pid_file}" if gm.mode22 else "mode22 PIDs   none loaded")
    else:
        if not ex.proto:
            ex.setup("0")
        pids = [p.strip().upper() for p in a.pids.split(",") if p.strip()]
        w = csv.writer(open(a.csv, "w", newline="")) if a.csv else None
        if w:
            w.writerow(["t"] + pids)
        t0 = time.monotonic()
        while time.monotonic() - t0 < a.seconds:
            row, show = [round(time.monotonic() - t0, 2)], []
            for p in pids:
                try:
                    name, val, units, _ = gm.read(p)
                except KeyError as e:
                    name, val, units = p, None, str(e)
                e = gm.mode22.get(p, {}) if p not in MODE01 else {}
                tag = "" if p in MODE01 else (" (sources disagree)" if e.get("conflicts") else " (unverified)")
                if e.get("labels") and val is not None:
                    shown = e["labels"][0] if val else e["labels"][1]
                else:
                    shown = "-" if val is None else round(val, 2)
                row.append(val)
                show.append(f"{name}{tag} {shown} {units}")
            if w:
                w.writerow(row)
            print(" | ".join(show), flush=True)
    ex.close()
    print(f"log: {logf.name}")


if __name__ == "__main__":
    main()
