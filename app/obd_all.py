"""
Every generic OBD-II value a car offers, through an ELM/STN adapter (OBDLink EX) and python-obd.

The car's own supported-PID bitmaps (01 00, 20, 40 ... C0) decide what is asked, for any make. Replies from
every ECU count (on CAN the transmission answers too); the engine's reply wins when two answer the same PID.
Formulas: telemetry_ref.OBD01 (tested against python-obd) plus the multi-value PIDs below. A supported PID with no
formula here is kept as raw hex, never given a guessed scaling. Read-only: only services 01 and 06 are ever sent.
"""
import os
import time

import telemetry_ref as T

STATUS_TEXT = {1: "open loop (cold)", 2: "closed loop", 4: "open loop (load or decel)",
               8: "open loop (system fault)", 16: "closed loop (feedback fault)"}
AIR_TEXT = {1: "upstream", 2: "downstream of cat", 4: "outside air or off", 8: "pump on for diagnostics"}
BITMAPS = range(0x00, 0xE1, 0x20)
SEARCH_TIMEOUT_S = 8    # waits only while the adapter is silent; a normal reply returns at once
PROTOCOL_FILE = os.path.expanduser("~/.cache/cyberdeck_ex_protocol")   # protocol id that last worked
DIRECT_ORDER = ("6", "8", "2", "1", "7", "9", "5", "3", "4")   # quick failures first; K-line bus inits take seconds
# Known but not a reading (their meaning is shown another way, or they only describe other PIDs)
NOT_VALUES = {0x01, 0x02, 0x13, 0x1C, 0x1D, 0x41, 0x4F, 0x50, 0x51}
O2_2BANK = ("B1S1", "B1S2", "B1S3", "B1S4", "B2S1", "B2S2", "B2S3", "B2S4")   # PID 13 says which are fitted
O2_4BANK = ("B1S1", "B1S2", "B2S1", "B2S2", "B3S1", "B3S2", "B4S1", "B4S2")   # PID 1D: same PIDs, other places


def may_speak_ssm(make, protocol):
    """Whether asking for Subaru SSM makes sense. make: from the VIN, else the one picked, '' when unknown.
    SSM runs on the K-line, which a J1850 car doesn't use, so the bus rules it out even with no make known."""
    return "J1850" not in protocol and make in ("", "Subaru")


def connect(port):
    """(connection, '') or (None, why). The protocol that last worked goes first (a test drive reconnects every sample),
    then the adapter's auto search, then each protocol asked directly: on a real J1850 truck the search said
    UNABLE TO CONNECT while the same 0100 on its own protocol answered. Only a real 0100 reply counts as connected."""
    import obd
    why = ""
    known = _last_protocol()
    tries = [known] * bool(known) + [None] + [p for p in DIRECT_ORDER if p != known]
    for protocol in tries:
        conn = obd.OBD(port, protocol=protocol, fast=False, timeout=SEARCH_TIMEOUT_S)
        if conn.is_connected() and _answers(conn):
            if conn.protocol_id() != known:
                _save_protocol(conn.protocol_id())
            return conn, ""
        why = why or conn.status()
        conn.close()
        time.sleep(0.3)
    return None, why


def _answers(conn):
    """A real bitmap reply. 'CAN ERROR' or 'NO DATA' on a wrong protocol also come back as messages from python-obd."""
    return any(d[:2] == b"\x41\x00" and len(d) >= 6 for _, d in ask(conn, "0100"))


def _last_protocol():
    try:
        with open(PROTOCOL_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_protocol(p):
    try:
        os.makedirs(os.path.dirname(PROTOCOL_FILE), exist_ok=True)
        with open(PROTOCOL_FILE, "w") as f:
            f.write(p)
    except OSError:
        pass      # only a speed-up: the next read searches again


def _keep(messages):
    return [(m.ecu, bytes(m.data)) for m in messages]


def ask(conn, hexcmd):
    """[(ecu, reply bytes)] for one service 01/06 request. Anything else is refused here."""
    from obd import ECU, OBDCommand
    if hexcmd[:2] not in ("01", "06"):
        raise ValueError(f"obd_all sends services 01 and 06 only, not {hexcmd}")
    return conn.query(OBDCommand(f"X{hexcmd}", hexcmd, hexcmd.encode(), 0, _keep, ECU.ALL, False), force=True).value or []


def _reply(replies, pid, n=0):
    """The data bytes of the best reply to mode 01 `pid`: the engine's when it answered."""
    from obd import ECU
    for _, d in sorted(replies, key=lambda r: r[0] != ECU.ENGINE):
        if d[:2] == bytes((0x41, pid)) and len(d) - 2 >= n:
            return d[2:]
    return None


def supported(conn):
    """(every mode 01 PID any ECU flags as supported, the bitmap PIDs that were asked)."""
    sup, asked = set(), []
    for base in BITMAPS:
        asked.append(base)
        bits = 0
        for _, d in ask(conn, f"01{base:02X}"):
            if d[:2] == bytes((0x41, base)) and len(d) >= 6:
                bits |= int.from_bytes(d[2:6], "big")
        sup |= {base + 32 - i for i in range(32) if bits >> i & 1}
        if not bits & 1:          # the last bit says whether the next bitmap exists
            break
    return sup - set(BITMAPS), asked


def _o2(pid, d, places, scale_set):
    """O2 PIDs 14-1B (narrow band), 24-2B (wide band, volts), 34-3B (wide band, current). [(label, value, unit)]"""
    place = "O2 " + places[(pid & 0x0F) - 4]
    if pid < 0x20:
        out = [(place, round(d[0] / 200, 3), "V")]
        if d[1] != 0xFF:      # FF: this sensor isn't used for trim
            out.append((place + " trim", round((d[1] - 128) * 100 / 128, 1), "%"))
        return out
    if scale_set:             # PID 4F changed the full scale of these: the fixed formula would be wrong
        return [("PID %02X" % pid, d[:4].hex().upper(), "")]
    out = [(place + " lambda", round((d[0] * 256 + d[1]) * 2 / 65536, 3), "")]
    x = d[2] * 256 + d[3]
    return out + [(place + " WR", round(x * 8 / 65536, 3), "V") if pid < 0x30 else
                  (place + " current", round(x / 256 - 128, 2), "mA")]


def _sensors(d, name, unit, width, f):
    """Multi-sensor PIDs: byte A flags which sensors are present, the values follow."""
    return [(f"{name} {i + 1}", f(d[1 + i * width:1 + (i + 1) * width]), unit) for i in range(2) if d[0] >> i & 1]


def decode(pid, d, places=O2_2BANK, scale_set=False):
    """[(label, value, unit)] for one reply's data bytes; [] when it is too short for its formula."""
    key = f"{pid:02X}"
    if key in T.OBD01:
        n, expr, unit = T.OBD01[key]
        if pid in range(0x14, 0x1C):
            return _o2(pid, d, places, scale_set) if len(d) >= 2 else []
        if len(d) < n:
            return []
        x = T.evaluate(expr, T.bytes_env(d[:n]))
        label = T.OBD01_LABELS.get(key)
        return [(label, round(x, 2), unit)] if label else [(f"PID {key}", d.hex().upper(), "")]
    if pid in range(0x24, 0x2C) or pid in range(0x34, 0x3C):
        return _o2(pid, d, places, scale_set) if len(d) >= 4 else []
    if pid == 0x03 and len(d) >= 1:
        names = ["Fuel sys", "Fuel sys 2"]
        return [(names[i], STATUS_TEXT[b], "") for i, b in enumerate(d[:2]) if b in STATUS_TEXT]
    if pid == 0x12 and len(d) >= 1 and d[0] in AIR_TEXT:
        return [("Secondary air", AIR_TEXT[d[0]], "")]
    if pid == 0x32 and len(d) >= 2:
        return [("Evap pressure", int.from_bytes(d[:2], "big", signed=True) / 4, "Pa")]
    if pid == 0x64 and len(d) >= 5:
        return [(f"Torque {n}", d[i] - 125, "%") for i, n in enumerate(("idle", "pt 1", "pt 2", "pt 3", "pt 4"))]
    if pid == 0x66 and len(d) >= 5:
        return _sensors(d, "MAF sensor", "g/s", 2, lambda b: round((b[0] * 256 + b[1]) / 32, 2))
    if pid == 0x67 and len(d) >= 3:
        return _sensors(d, "Engine temp", "°C", 1, lambda b: b[0] - 40)
    if pid == 0x68 and len(d) >= 3:
        return _sensors(d, "Intake temp", "°C", 1, lambda b: b[0] - 40)
    if pid == 0x7F and len(d) >= 5 and d[0] & 1:
        return [("Engine total run", int.from_bytes(d[1:5], "big"), "s")]
    if pid == 0x8E and len(d) >= 1:
        return [("Friction torque", d[0] - 125, "%")]
    if pid == 0xA2 and len(d) >= 2:
        return [("Cyl fuel rate", (d[0] * 256 + d[1]) / 32, "mg/stroke")]
    if pid == 0xA6 and len(d) >= 4:
        return [("Odometer", int.from_bytes(d[:4], "big") / 10, "km")]
    return [(f"PID {key}", d.hex().upper(), "")]


def _rank(pid):
    """Reading order: the values that matter most first, so a prompt cut keeps them."""
    if pid in T.OBD01_FIRST:
        return (0, T.OBD01_FIRST.index(pid))
    if pid in range(0x14, 0x1C) or pid in range(0x24, 0x2C) or pid in range(0x34, 0x3C) or pid == 0x03:
        return (1, pid)
    return (2, pid)


def monitors(conn):
    """PID 01 through python-obd's decoder: 'MIL on', stored code count, monitors not finished. '' when none."""
    import obd
    r = conn.query(obd.commands.STATUS, force=True)
    v = r.value
    if v is None:
        return []
    out = [("MIL", "on" if v.MIL else "off", ""), ("Stored DTCs", v.DTC_count, "")]
    left = [t.name.replace("_", " ").lower() for t in vars(v).values()
            if getattr(t, "available", False) and not t.complete]
    if left:
        out.append(("Monitors not done", ", ".join(sorted(left)), ""))
    return out


def read_all(conn):
    """Everything the car offers on this connection. Returns {'line', 'supported', 'asked', 'silent'}:
    line: 'RPM 750 rpm | ECT 85 °C | ...'; supported / asked: PID hex lists; silent: flagged but never answered."""
    import obd
    sup, bitmaps = supported(conn)
    places = O2_4BANK if 0x1D in sup else O2_2BANK
    scale_set = False
    if 0x4F in sup:
        d = _reply(ask(conn, "014F"), 0x4F, 3)
        scale_set = bool(d and any(d[:3]))
    got, silent = {}, []
    for pid in sorted(sup - NOT_VALUES, key=_rank):
        d = None
        for _ in range(2):       # K-line and busy CAN buses miss the odd request; a bitmap PID should answer
            d = _reply(ask(conn, f"01{pid:02X}"), pid)
            if d is not None:
                break
        if d is None:
            silent.append(pid)
            continue
        got[pid] = decode(pid, d, places, scale_set)
    vals = []
    first = [p for p in got if _rank(p)[0] == 0]
    for pid in first:
        vals += got[pid]
    r = conn.query(obd.commands.ELM_VOLTAGE, force=True)
    if not r.is_null():
        vals.append(("Volts", r.value.magnitude, "V"))
    if 0x01 in sup:
        vals += monitors(conn)
    for pid in got:
        if pid not in first:
            vals += got[pid]
    line = " | ".join(f"{a} {b} {u}".rstrip() for a, b, u in vals)
    allp = {b + i for b in bitmaps for i in range(1, 33)} - set(BITMAPS)
    return {"line": line, "supported": sorted(f"{p:02X}" for p in sup),
            "asked": sorted(f"{p:02X}" for p in allp), "silent": [f"{p:02X}" for p in silent]}


def mode06(conn):
    """On-board test results. (all lines, failed lines, 'decoded' | 'raw').
    CAN: python-obd's named results with limits. Older protocols: test ids and scaling are the maker's own and no open
    layout exists, so their frames are kept raw and never judged."""
    import obd
    if conn.protocol_id() in ("6", "7", "8", "9"):
        lines, failed = [], []
        for cmd in obd.commands[6]:
            if cmd is None or cmd not in conn.supported_commands or cmd.decode.__name__ == "pid":
                continue
            r = conn.query(cmd)
            for t in r.value.tests if not r.is_null() else []:
                s = f"{cmd.desc}: {t.desc} = {t.value:.4g} (min {t.min:.4g}, max {t.max:.4g})"
                lines.append(s)
                if not t.passed:
                    failed.append(s)
        return lines, failed, "decoded"
    send = getattr(conn.interface, "_ELM327__send", None)   # python-obd drops multi-frame service 06 replies
    if send is None:
        return [], [], "raw"
    lines = [str(x) for x in send(b"0600") if str(x).replace(" ", "")[:2] not in ("NO", "UN", "?")]
    tids = set()
    for x in lines:
        b = x.split()
        i = b.index("46") if "46" in b else -1
        if i >= 0 and len(b) >= i + 7 and b[i + 1] == "00":
            bits = int("".join(b[i + 3:i + 7]), 16)
            tids |= {32 - k for k in range(32) if bits >> k & 1}
    for tid in sorted(tids):
        lines += [str(x) for x in send(b"06%02X" % tid)]
    return lines, [], "raw"
