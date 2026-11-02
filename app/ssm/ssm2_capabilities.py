#!/usr/bin/env python3
"""
What does THIS Subaru ECU support?  Decode the SSM2 init (0xBF) reply against
FreeSSM's tables, and read trouble codes and switches over the K-line.

Subaru ECUs answer the 0xBF init with:
    FF | SYS ID (3 bytes) | ROM ID (5 bytes) | capability "flag bytes" (32/48/96)
Each flag-byte bit says whether one live-data value, switch, DTC block or test
is supported. FreeSSM and RomRaider both work this way, so the deck does not
need a ROM-specific table to know what it can read: the ECU tells it.

READ-ONLY. Everything here uses 0xBF (init) and 0xA8 (read addresses) through
TelemetryPoller.send_ssm2_command. Actuator tests and adjustments are listed
for reference only; this file has no code that sends them, and it should stay
that way (see README.md).

Tables: freessm_ssm2_tables.json, built from FreeSSM (GPL-3) by
build_freessm_tables.py. Gating rules copied from FreeSSM's
SSMFlagbyteDefinitionsInterface.cpp.

Usage:
    python3 ssm2_capabilities.py --init-hex "FF A1 10 01 23 12 .. .."   # decode a saved reply
    python3 ssm2_capabilities.py --live [--port /dev/ttyUSB0]            # probe the car, read DTCs
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = os.path.join(HERE, "freessm_ssm2_tables.json")

# SYS ID byte 3 -> engine, when SYS ID byte 2 is 0x10 (FreeSSM SysDescriptions)
ENGINE_SYS = {0x01: "2.5L SOHC", 0x02: "2.5L SOHC", 0x03: "2.2L SOHC", 0x04: "2.2L SOHC",
              0x05: "1.5L SOHC", 0x06: "1.6L SOHC", 0x07: "1.8L SOHC", 0x08: "2.0L SOHC",
              0x09: "2.0L DOHC", 0x0A: "2.5L DOHC", 0x0B: "2.0L DOHC Turbo",
              0x0C: "2.0L DOHC Turbo", 0x0D: "2.0L DOHC Turbo", 0x0E: "3.0L DOHC",
              0x0F: "2.0L DOHC Turbo", 0x10: "2.5L DOHC", 0x11: "2.5L DOHC Turbo",
              0x12: "3.0L DOHC", 0x13: "1.5L DOHC", 0x14: "2.0L DOHC Turbo Diesel",
              0x15: "3.6L DOHC"}


def load_tables(path=TABLES):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- scaling
def scale(raw, formula):
    """FreeSSM scaling: operators applied strictly left to right ('-128/128*100'
    means ((x-128)/128)*100). 's8'/'s16' prefix = signed. 'a=b,c=d' = lookup."""
    if "=" in formula:
        for pair in formula.split(","):
            k, _, v = pair.partition("=")
            if k.strip().isdigit() and int(k) == raw:
                return v
        return None
    x = float(raw)
    m = re.match(r"[sS](8|16)", formula)
    if m:
        bits = int(m.group(1))
        if raw >= 1 << (bits - 1):
            x = raw - (1 << bits)
        formula = formula[m.end():]
    for op, num in re.findall(r"([+\-*/])([+\-]?\d+(?:\.\d+)?)", formula):
        n = float(num)
        if op == "+":
            x += n
        elif op == "-":
            x -= n
        elif op == "*":
            x *= n
        elif n != 0:
            x /= n
    return x


# ---------------------------------------------------------------- init decode
class ECUCaps:
    def __init__(self, init_payload, tables=None):
        p = bytes(init_payload)
        if p and p[0] == 0xFF:
            p = p[1:]
        if len(p) < 8:
            raise ValueError("init reply too short (need SYS ID + ROM ID)")
        self.sys_id = p[0:3]
        self.rom_id = p[3:8]
        self.flags = p[8:]
        self.t = tables or load_tables()

    # FreeSSM tables use 1-based flag byte and bit numbers
    def bit(self, flagbyte, bit):
        i = flagbyte - 1
        return 0 <= i < len(self.flags) and bool(self.flags[i] & (1 << (bit - 1)))

    # FreeSSM interface code uses 0-based (byteindex, bitnr)
    def bit0(self, byteindex, bitnr):
        return byteindex < len(self.flags) and bool(self.flags[byteindex] & (1 << bitnr))

    @property
    def engine(self):
        if self.sys_id[1] == 0x10:
            return ENGINE_SYS.get(self.sys_id[2], "unknown")
        return "unknown"

    @property
    def obd2_format(self):
        return not self.bit0(29, 7)

    @property
    def has_test_mode(self):
        return self.bit0(11, 5)

    @property
    def has_actuator_tests(self):
        return self.has_test_mode and self.bit0(0, 0) and self.bit0(28, 6)

    @property
    def has_immobilizer(self):
        return self.bit0(28, 4)

    @property
    def has_vin(self):
        return self.bit0(36, 0)

    def measuring_blocks(self):
        return [m for m in self.t["measuring_blocks"]
                if m["cu"] & 1 and self.bit(m["flagbyte"], m["bit"])]

    def switches(self):
        return [s for s in self.t["switches"]
                if s["cu"] & 1 and self.bit(s["flagbyte"], s["bit"])]

    def dtc_address_pairs(self):
        """(current, memorized) address pairs the ECU reports, per FreeSSM."""
        pairs = []
        if not self.obd2_format:
            return [(a, a + 22) for a in range(0x8E, 0x99)]
        if self.bit0(29, 4) or self.bit0(29, 6):
            pairs += [(a, a + 32) for a in range(0x8E, 0xAE)]
        if self.bit0(28, 0):
            pairs += [(a, a + 4) for a in range(0xF0, 0xF4)]
        if len(self.flags) > 32:
            for fb, lo, hi, off in ((7, 0x123, 0x12A, 8), (6, 0x150, 0x154, 5),
                                    (5, 0x160, 0x164, 5), (4, 0x174, 0x17A, 7)):
                if self.bit0(39, fb):
                    pairs += [(a, a + off) for a in range(lo, hi + 1)]
        return pairs

    def dtc_table(self):
        return self.t["dtc_obd"] if self.obd2_format else self.t["dtc_subaru"]

    def actuator_tests_reference(self):
        if not self.has_actuator_tests:
            return []
        return [a for a in self.t["actuator_tests_REFERENCE_ONLY"] if self.bit(a["flagbyte"], a["bit"])]

    def adjustments_reference(self):
        out = []
        sid = self.sys_id.hex().upper()
        for a in self.t["adjustments_REFERENCE_ONLY"]:
            if a["cu"] != "engine":
                continue
            g = a["gate"]
            if "-" in g:
                fb, b = (int(x) for x in g.split("-"))
                if self.bit(fb, b):
                    out.append(a)
            elif g.upper() == sid:
                out.append(a)
        return out

    def report(self):
        mbs, sws = self.measuring_blocks(), self.switches()
        lines = [
            f"SYS ID {self.sys_id.hex().upper()} ({self.engine})   ROM ID {self.rom_id.hex().upper()}"
            f"   {len(self.flags)} flag bytes",
            f"DTC format: {'OBD-II P-codes' if self.obd2_format else 'Subaru 2-digit codes'}"
            f"   test mode: {self.has_test_mode}   immobilizer: {self.has_immobilizer}   VIN: {self.has_vin}",
            "", f"Live data supported ({len(mbs)}):"]
        for m in mbs:
            a = f"0x{m['addr_high']:06X}+0x{m['addr_low']:06X}" if m["addr_high"] else f"0x{m['addr_low']:06X}"
            lines.append(f"  {a:<20} {m['title']} [{m['unit']}] {m['formula']}")
        lines += ["", f"Switches supported ({len(sws)}):"]
        for s in sws:
            lines.append(f"  0x{s['addr']:06X} bit {s['bit']}  {s['title']} ({s['states']})")
        pairs = self.dtc_address_pairs()
        lines += ["", f"DTC address blocks: {len(pairs)} (current / memorized)"]
        acts = self.actuator_tests_reference()
        lines += ["", f"Actuator tests the ECU advertises ({len(acts)}), REFERENCE ONLY, the deck never runs these:"]
        lines += [f"  {a['title']}" for a in acts]
        adjs = self.adjustments_reference()
        lines += ["", f"Adjustments the ECU advertises ({len(adjs)}), REFERENCE ONLY:"]
        lines += [f"  {a['title']} [{a['unit']}] default raw {a['raw_default']}" for a in adjs]
        return "\n".join(lines)


# ---------------------------------------------------------------- live reads (A8 only)
def read_addresses(poller, addrs, chunk=32):
    """Read single bytes at addrs with 0xA8. Returns {addr: value} or {} on failure."""
    out = {}
    for i in range(0, len(addrs), chunk):
        part = addrs[i:i + chunk]
        payload = bytes([0xA8, 0x00]) + b"".join(a.to_bytes(3, "big") for a in part)
        data = poller.send_ssm2_command(payload)
        if len(data) < 1 + len(part) or data[0] != 0xE8:
            return {}
        out.update(zip(part, data[1:1 + len(part)]))
    return out


VIN_PTR = (0xDA, 0xDB, 0xDC)  # FreeSSM getVIN: these 3 bytes hold the address of the 17 VIN characters


def read_vin(poller, caps):
    """The VIN stored in the ECU, read with 0xA8 only, or '' when the ECU doesn't advertise one (most
    pre-2005 ECUs) or what comes back isn't a well-formed VIN."""
    if not caps.has_vin:
        return ""
    ptr = read_addresses(poller, list(VIN_PTR))
    if len(ptr) != 3:
        return ""
    addr = (ptr[0xDA] << 16) | (ptr[0xDB] << 8) | ptr[0xDC]
    if not 0 < addr <= 0xFFFFFF - 16:
        return ""
    data = read_addresses(poller, list(range(addr, addr + 17)))
    vin = "".join(chr(data[a]) for a in range(addr, addr + 17) if a in data)
    return vin if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin) else ""


def read_dtcs(poller, caps):
    """Returns {'current': [(code, title)], 'memorized': [...]} using caps' DTC table."""
    pairs = caps.dtc_address_pairs()
    if not pairs:   # the init reply advertises no DTC blocks: that is "nothing to read", not a failed read
        return {"current": [], "memorized": [], "note": "ECU init reply lists no DTC blocks"}
    vals = read_addresses(poller, [a for p in pairs for a in p])
    if not vals:
        return None
    defs = {}
    for d in caps.dtc_table():
        defs[(d["addr_current"], d["addr_memorized"], d["bit"])] = (d["code"], d["title"])
    res = {"current": [], "memorized": []}
    for cur, mem in pairs:
        for which, addr in (("current", cur), ("memorized", mem)):
            byte = vals.get(addr, 0)
            for b in range(8):
                if byte & (1 << b):
                    code = defs.get((cur, mem, b + 1), (f"?? 0x{addr:X} bit {b + 1}", "no FreeSSM definition"))
                    if code[0] or code[1]:
                        res[which].append(code)
    return res


def read_switches(poller, caps):
    sws = caps.switches()
    vals = read_addresses(poller, sorted({s["addr"] for s in sws}))
    if not vals:
        return None
    return [(s["title"], bool(vals[s["addr"]] & (1 << (s["bit"] - 1))), s["states"]) for s in sws]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init-hex", help="saved 0xBF reply payload as hex (starts with FF)")
    ap.add_argument("--live", action="store_true", help="probe the ECU over the KKL cable")
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--save", default="ecu_init_reply.hex", help="where --live saves the raw init reply")
    args = ap.parse_args()

    if args.init_hex:
        print(ECUCaps(bytes.fromhex(args.init_hex.replace(" ", ""))).report())
        return
    if not args.live:
        ap.print_help()
        return

    sys.path.insert(0, os.path.join(HERE, "..", "gemini_review", "recovered"))
    from telemetry_logger import TelemetryPoller

    poller = TelemetryPoller(port=args.port)
    if not poller.probe_ecu():
        print("[!] ECU not responding on K-line. Key ON?")
        raise SystemExit(1)
    with open(args.save, "w") as f:
        f.write(poller.init_raw.hex(" ").upper() + "\n")
    print(f"[+] raw init reply saved to {args.save}\n")
    caps = ECUCaps(poller.init_raw)
    print(caps.report())
    time.sleep(0.1)
    dtcs = read_dtcs(poller, caps)
    print("\nTrouble codes:")
    if dtcs is None:
        print("  read failed")
    else:
        for which in ("current", "memorized"):
            print(f"  {which}: " + (", ".join(f"{c} {t}" for c, t in dtcs[which]) or "none"))
    sw = read_switches(poller, caps)
    if sw:
        print("\nSwitches:")
        for title, on, states in sw:
            off_s, _, on_s = states.replace("\\", "/").partition("/")
            print(f"  {title}: {on_s if on else off_s}")
    poller.ser.close()


if __name__ == "__main__":
    main()
