"""Reading the car. Read-only, always: this file sends no command that changes anything in an ECU.

Two ways in:
  OBD-II through an ELM327-type adapter (OBDLink EX): stored and pending codes (modes 03/07), VIN
         (mode 09), and live data (mode 01).
  Subaru SSM2 over a plain K-line cable (KKL): ECU id (0xBF) and memory reads (0xA8) only. Trouble codes
         come from the RomRaider bit map in the kb (dtc_ssm), live data from RomRaider's parameter list.
"""
import glob
import os
import re
import time

try:
    import serial
except ImportError:            # the app still runs; READ CAR says what's missing
    serial = None

CODE_RE = re.compile(r"\b[PBCU][0-9A-F]\d{3}\b")


def ports():
    """Serial adapters, by-id names first (they say which cable is which)."""
    out = sorted(glob.glob("/dev/serial/by-id/*"))
    seen = {os.path.realpath(p) for p in out}
    out += [p for p in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")) if os.path.realpath(p) not in seen]
    return out


def looks_like_elm(port):
    return bool(re.search(r"obdlink|scantool|stn|elm|obd", port, re.I))


class LinkError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- OBD-II
DTC_FIRST = "PCBU"

# mode 01 PIDs read for the live view: name, pid, bytes, formula, unit
PIDS = [
    ("Engine speed", 0x0C, 2, lambda a, b: (256 * a + b) / 4, "rpm"),
    ("Coolant temp", 0x05, 1, lambda a, b: a - 40, "°C"),
    ("Intake air temp", 0x0F, 1, lambda a, b: a - 40, "°C"),
    ("Short term fuel trim B1", 0x06, 1, lambda a, b: (a - 128) * 100 / 128, "%"),
    ("Long term fuel trim B1", 0x07, 1, lambda a, b: (a - 128) * 100 / 128, "%"),
    ("Engine load", 0x04, 1, lambda a, b: a * 100 / 255, "%"),
    ("Manifold pressure", 0x0B, 1, lambda a, b: a, "kPa"),
    ("Mass airflow", 0x10, 2, lambda a, b: (256 * a + b) / 100, "g/s"),
    ("Throttle", 0x11, 1, lambda a, b: a * 100 / 255, "%"),
    ("Timing advance", 0x0E, 1, lambda a, b: a / 2 - 64, "°"),
    ("Vehicle speed", 0x0D, 1, lambda a, b: a, "km/h"),
    ("O2 sensor B1S1", 0x14, 1, lambda a, b: a / 200, "V"),
    ("Module voltage", 0x42, 2, lambda a, b: (256 * a + b) / 1000, "V"),
]


def decode_dtc(b1, b2):
    if b1 == 0 and b2 == 0:
        return None
    return f"{DTC_FIRST[b1 >> 6]}{(b1 >> 4) & 3}{b1 & 15:X}{b2 >> 4:X}{b2 & 15:X}"


class Elm:
    def __init__(self, port, baud=115200, timeout=6):
        if serial is None:
            raise LinkError("pyserial isn't installed (pip install pyserial)")
        self.s = serial.Serial(port, baud, timeout=0.2, exclusive=True)
        self.timeout = timeout
        self.can = False
        for cmd in ("ATZ", "ATE0", "ATL0", "ATS1", "ATH0", "ATSP0"):
            self.cmd(cmd, wait=2 if cmd == "ATZ" else 1)
        r = self.cmd("0100", wait=self.timeout)
        if "41 00" not in r:
            self.close()
            raise LinkError(f"no OBD-II answer ({r.strip()[:40] or 'nothing'}): key on?")
        self.can = self.cmd("ATDPN").strip().lstrip("A")[:1] in "6789ABC"

    def cmd(self, text, wait=1.0):
        self.s.reset_input_buffer()
        self.s.write((text + "\r").encode())
        buf, end = b"", time.time() + wait
        while time.time() < end:
            buf += self.s.read(256)
            if buf.endswith(b">") or b">" in buf[-3:]:
                break
        return buf.decode("ascii", "ignore").replace(">", "").replace("SEARCHING...", "").strip()

    def _bytes(self, resp):
        """Response lines as byte lists, CAN frame numbers ('0:') removed."""
        out = []
        for line in resp.splitlines():
            line = re.sub(r"^[0-9A-F]:\s*", "", line.strip())
            if re.fullmatch(r"([0-9A-F]{2}\s?)+", line):
                out.append([int(x, 16) for x in line.split()])
        return out

    def codes(self, mode="03"):
        resp = self.cmd(mode, wait=self.timeout)
        if "NO DATA" in resp:
            return []
        want = int(mode, 16) + 0x40
        data = []
        for b in self._bytes(resp):
            if self.can:
                data += b
            elif b and b[0] == want:
                data += b[1:]
        if self.can and data and data[0] == want:
            data = data[2:]          # 43, count
        found = []
        for i in range(0, len(data) - 1, 2):
            c = decode_dtc(data[i], data[i + 1])
            if c and c not in found:
                found.append(c)
        return found

    def vin(self):
        resp = self.cmd("0902", wait=self.timeout)
        raw = bytes(x for b in self._bytes(resp) for x in b)
        txt = "".join(chr(c) for c in raw if 48 <= c <= 90)
        m = re.search(r"[A-HJ-NPR-Z0-9]{17}", txt)
        return m.group(0) if m else ""

    def live(self):
        out = {}
        for name, pid, n, f, unit in PIDS:
            resp = self.cmd(f"01{pid:02X}", wait=2)
            for b in self._bytes(resp):
                if len(b) >= 2 + n and b[0] == 0x41 and b[1] == pid:
                    a, bb = b[2], (b[3] if n == 2 else 0)
                    out[name] = (round(f(a, bb), 2), unit)
                    break
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------- Subaru SSM2
SSM_LIVE = [("P8", "rpm"), ("P2", "C"), ("P11", "C"), ("P3", "%"), ("P4", "%"), ("P1", "%"), ("P7", "kPa"),
            ("P12", "g/s"), ("P13", "%"), ("P10", "degrees"), ("P9", "km/h"), ("P17", "V")]
SAFE_EXPR = re.compile(r"^[x0-9+\-*/(). ]+$")


class Ssm:
    """SSM2 at 4800 baud on the K-line. Commands used: 0xBF (who are you) and 0xA8 (read memory). Nothing else."""
    ALLOWED = {0xBF, 0xA8}

    def __init__(self, port, timeout=2.0):
        if serial is None:
            raise LinkError("pyserial isn't installed (pip install pyserial)")
        self.s = serial.Serial(port, 4800, timeout=0.1, exclusive=True)
        self.timeout = timeout
        r = self._send([0xBF])
        if not r or r[0] != 0xFF or len(r) < 9:
            self.close()
            raise LinkError("no SSM answer on the K-line: key on, KKL cable in?")
        self.ecu_id = bytes(r[1:4]).hex().upper()
        self.rom_id = bytes(r[4:9]).hex().upper()

    def _send(self, data):
        if data[0] not in self.ALLOWED:
            raise LinkError("refused: the deck only reads")
        pkt = [0x80, 0x10, 0xF0, len(data)] + list(data)
        pkt.append(sum(pkt) & 0xFF)
        self.s.reset_input_buffer()
        self.s.write(bytes(pkt))
        buf, end = b"", time.time() + self.timeout
        while time.time() < end:
            buf += self.s.read(64)
            i = buf.find(b"\x80\xf0\x10")          # skip the K-line echo of our own request
            if i >= 0 and len(buf) >= i + 4:
                n = buf[i + 3]
                if len(buf) >= i + 5 + n:
                    frame = buf[i:i + 5 + n]
                    if sum(frame[:-1]) & 0xFF != frame[-1]:
                        raise LinkError("SSM reply failed its checksum")
                    return list(frame[4:-1])
        return None

    def read(self, addrs):
        """Bytes at the given addresses, 20 at a time."""
        out = {}
        for i in range(0, len(addrs), 20):
            chunk = addrs[i:i + 20]
            data = [0xA8, 0x00]
            for a in chunk:
                data += [(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF]
            r = self._send(data)
            if not r or r[0] != 0xE8 or len(r) - 1 < len(chunk):
                raise LinkError("SSM read got no answer")
            out.update(zip(chunk, r[1:1 + len(chunk)]))
        return out

    def codes(self, kb):
        """(current, memorized) codes from the RomRaider bit map in the kb."""
        rows = kb.db.execute("SELECT code, tmp_addr, mem_addr, bit FROM dtc_ssm").fetchall()
        addrs = sorted({int(r[1], 16) for r in rows} | {int(r[2], 16) for r in rows if r[2]})
        vals = self.read(addrs)
        cur, mem = [], []
        for code, tmp, memaddr, bit in rows:
            if vals.get(int(tmp, 16), 0) >> bit & 1 and code not in cur:
                cur.append(code)
            if memaddr and vals.get(int(memaddr, 16), 0) >> bit & 1 and code not in mem:
                mem.append(code)
        return sorted(cur), sorted(mem)

    def live(self, kb):
        out = {}
        for pid, unit in SSM_LIVE:
            r = kb.db.execute("SELECT name, address, length, expr FROM ssm_param WHERE id=? AND ecu_id='' AND units=?",
                              (pid, unit)).fetchone()
            if not r or not SAFE_EXPR.match(r[3] or ""):
                continue
            base, n = int(r[1], 16), r[2] or 1
            try:
                v = self.read([base + k for k in range(n)])
            except LinkError:
                continue
            x = 0
            for k in range(n):
                x = (x << 8) | v[base + k]
            out[r[0]] = (round(eval(r[3], {"__builtins__": {}}, {"x": x}), 2), unit.replace("C", "°C"))
        return out

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------------------------- one call
def read_car(kb, want_live=True):
    """Try every adapter: OBD-II on ELM-type ports, SSM on the others (then each on the rest).
    Returns dict(codes, pending, vin, live, log, via)."""
    res = {"codes": [], "pending": [], "vin": "", "live": {}, "log": [], "via": ""}
    ps = ports()
    if not ps:
        res["log"].append("No cable found. Plug in the OBDLink EX or the KKL cable.")
        return res
    elm_first = sorted(ps, key=lambda p: not looks_like_elm(p))
    got = False
    for p in elm_first:
        try:
            e = Elm(p)
        except Exception as ex:
            res["log"].append(f"OBD-II on {os.path.basename(p)}: {ex}")
            continue
        try:
            res["codes"], res["pending"] = e.codes("03"), e.codes("07")
            res["vin"] = e.vin()
            if want_live:
                res["live"] = e.live()
            res["via"] = f"OBD-II ({os.path.basename(p)})"
            res["log"].append(f"OBD-II: {len(res['codes'])} stored, {len(res['pending'])} pending"
                              + (f", VIN {res['vin']}" if res["vin"] else ", no VIN (normal before about 2005)"))
            got = True
        finally:
            e.close()
        break
    for p in sorted(ps, key=looks_like_elm):
        try:
            s = Ssm(p)
        except Exception as ex:
            res["log"].append(f"SSM on {os.path.basename(p)}: {ex}")
            continue
        try:
            cur, mem = s.codes(kb)
            res["log"].append(f"SSM ECU {s.ecu_id} ROM {s.rom_id}: current {', '.join(cur) or 'none'}; "
                              f"memorized {', '.join(mem) or 'none'}")
            for c in mem + cur:
                if c not in res["codes"]:
                    res["codes"].append(c)
            if want_live and not res["live"]:
                res["live"] = s.live(kb)
            res["via"] = (res["via"] + " + " if res["via"] else "") + f"SSM ({os.path.basename(p)})"
            got = True
        except Exception as ex:
            res["log"].append(f"SSM read failed: {ex}")
        finally:
            s.close()
        break
    if not got:
        res["log"].append("Nothing answered. Key on? Both cables tried on every port.")
    return res


# ---------------------------------------------------------------------------------------------- live data in words
def explain(live):
    """Plain-language notes on live readings. These are general OBD-II rules of thumb, not the manual's
    numbers, and the screen says so."""
    def get(*names):
        for k, (v, u) in live.items():
            if any(n.lower() in k.lower() for n in names):
                return v
        return None
    out = []
    ect, rpm = get("coolant"), get("engine speed")
    if ect is not None:
        if ect < 60:
            out.append(f"Coolant {ect:g}°C: engine still cold; fuel trims and idle aren't settled yet.")
        elif ect > 105:
            out.append(f"Coolant {ect:g}°C: hot. Check cooling before anything else.")
        else:
            out.append(f"Coolant {ect:g}°C: warm.")
    st, lt = get("short term", "a/f correction"), get("long term", "a/f learning")
    if st is not None and lt is not None:
        tot = st + lt
        if abs(tot) >= 10:
            way = "adding fuel (it sees lean: air leak, low fuel pressure, or exhaust leak ahead of the front sensor)" \
                if tot > 0 else "pulling fuel (it sees rich: leaking injector, high fuel pressure, or a sensor)"
            out.append(f"Total fuel trim {tot:+.0f}%: {way}.")
        else:
            out.append(f"Total fuel trim {tot:+.0f}%: normal range.")
    v = get("voltage", "battery")
    if v is not None and rpm:
        if v < 13.2:
            out.append(f"Voltage {v:g} V running: not charging fully.")
        elif v > 14.8:
            out.append(f"Voltage {v:g} V: overcharging.")
    return out
