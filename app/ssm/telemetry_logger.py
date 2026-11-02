#!/usr/bin/env python3
"""
SSM2 live-data poller for the 2002 Forester (EJ251, K-line, 4800 baud) over a
bare FT232 "KKL" cable on /dev/ttyUSB0.

Recovered from the user's Gemini history (version of 2026-10-11 22:21, the one
that read the ECU) with the parameter addresses corrected against the
RomRaider v370 logger definitions in kb/subaru.db (table ssm_param):

    param                     Gemini used   correct   (what Gemini was really reading)
    A/F Correction #1 (STFT)  0x22          0x09      Knock Correction Advance
    A/F Learning #1  (LTFT)   0x23          0x0A      Atmospheric Pressure (~101 kPa -> "-21%")
    Roughness Monitor Cyl #2  0xD0          0xCF      A/F Correction #3 (stuck near 128)
    Roughness Monitor Cyl #3  0xD2          0xD8      Rear O2 Heater Voltage
    Roughness Monitor Cyl #4  0xD4          0xD9      AWD / inhibitor switch bits

So the "LTFT -22%" and the Cyl3/Cyl4 "misfire" totals from October were not
real fuel-trim or misfire data. RPM, coolant temp, MAP and timing addresses
were right.

Reads only: the logger sends 0xBF (ECU init / identify) and 0xA8 (read
addresses). The TEST MODE writes live in ssm_tests.py, which uses _transact().

Not every parameter is supported by every ECU. The init response carries
capability bytes; whether this 2002 ECU reports the roughness monitors is
unverified, so treat zero/constant roughness values with suspicion.

Usage:  python3 telemetry_logger.py            (probe + print live values)
        from telemetry_logger import TelemetryPoller   (same API as before)
"""
import os
import time
import sqlite3
import threading

import serial

SESSION_DB = os.path.expanduser("~/subaru_cyberdeck/session_active.db")

# (column, address, byte length, conversion) from RomRaider logger_STD_EN_v370
PARAMS = [
    ("ect_c",               0x000008, 1, lambda b: b[0] - 40),
    ("stft_pct",            0x000009, 1, lambda b: round((b[0] - 128) * 100 / 128, 1)),
    ("ltft_pct",            0x00000A, 1, lambda b: round((b[0] - 128) * 100 / 128, 1)),
    ("map_kpa",             0x00000D, 1, lambda b: float(b[0])),          # x*10 hPa = x kPa
    ("rpm",                 0x00000E, 2, lambda b: ((b[0] << 8) | b[1]) // 4),
    ("speed_kph",           0x000010, 1, lambda b: b[0]),
    ("timing_advance_deg",  0x000011, 1, lambda b: round((b[0] - 128) / 2, 1)),
    ("iat_c",               0x000012, 1, lambda b: b[0] - 40),
    ("tps_pct",             0x000015, 1, lambda b: round(b[0] * 100 / 255, 1)),
    ("front_o2_v",          0x000016, 2, lambda b: round(((b[0] << 8) | b[1]) / 200, 3)),
    ("battery_v",           0x00001C, 1, lambda b: round(b[0] * 8 / 100, 2)),
    ("knock_corr_deg",      0x000022, 1, lambda b: round((b[0] - 128) / 2, 1)),
    ("cyl1_misfires",       0x0000CE, 1, lambda b: b[0]),
    ("cyl2_misfires",       0x0000CF, 1, lambda b: b[0]),
    ("cyl3_misfires",       0x0000D8, 1, lambda b: b[0]),
    ("cyl4_misfires",       0x0000D9, 1, lambda b: b[0]),
]
COLUMNS = [p[0] for p in PARAMS]


def calc_ssm2_checksum(packet: bytes) -> int:
    return sum(packet) & 0xFF


# Reads: 0xBF = ECU init / identify, 0xA8 = read single addresses, 0xA0 = read a memory block.
# send_ssm2_command() refuses everything else. The only writes (0xB8, TEST MODE) go through ssm_tests.py,
# which checks each one against the tests this ECU advertises.
ALLOWED_SSM2 = {0xBF, 0xA8, 0xA0}
ECU_REPLY_WAIT = 1.0   # s to wait for the first reply byte after our request (ECUs can be slow right after key-on)


class TelemetryPoller:
    def __init__(self, port="/dev/ttyUSB0", baudrate=4800, db_path=SESSION_DB):
        self.port = port
        self.baudrate = baudrate
        self.db_path = db_path
        self.running = False
        self.thread = None
        self.ser = None
        self.ecu_online = False
        self.ssm_id = ""
        self.rom_id = ""
        self.capabilities = b""
        self.init_raw = b""
        self.latest = {}
        self._db_ready = False

    def _init_db_wal(self):
        # Only the logger needs the session DB. A one-off read (READ CAR) must not fail because
        # ~/subaru_cyberdeck is missing or read-only, so this runs when polling starts, not in __init__.
        if self._db_ready:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._db_ready = True
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cols = ", ".join(f"{c} REAL" for c in COLUMNS)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS telemetry_bus (
                frame_id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL, {cols}
            );
        """)
        conn.commit()
        conn.close()

    def connect_serial(self) -> bool:
        if self.ser is not None and self.ser.is_open:
            self.ser.close()  # probing twice must not leak an open port
        try:
            kw = dict(port=self.port, baudrate=self.baudrate, bytesize=serial.EIGHTBITS,
                      parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.25)
            try:  # exclusive: a second program (or a second deck thread) can't share the K-line
                self.ser = serial.Serial(exclusive=True, **kw)
            except TypeError:  # pyserial older than 3.3
                self.ser = serial.Serial(**kw)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            return True
        except (serial.SerialException, FileNotFoundError):
            return False

    def _byte_time(self, n):
        return n * 10.0 / self.baudrate  # 8N1 = 10 bits per byte on the wire

    def _read(self, n, timeout):
        self.ser.timeout = timeout
        return self.ser.read(n)

    def send_ssm2_command(self, payload: bytes) -> bytes:
        if not payload or payload[0] not in ALLOWED_SSM2:
            raise ValueError(f"refusing SSM2 command 0x{payload[:1].hex()}: reads only here; writes go through ssm_tests.py")
        return self._transact(payload)

    def _transact(self, payload: bytes) -> bytes:
        """Send one SSM2 frame and return the reply payload. No command check: callers do that.
        Only ssm_tests.py calls this directly, after its own allowlist (TEST MODE writes)."""
        if not self.ser or not self.ser.is_open:
            return b""
        if len(payload) > 255:
            return b""

        body = bytes([0x80, 0x10, 0xF0, len(payload)]) + payload
        packet = body + bytes([calc_ssm2_checksum(body)])

        try:
            self.ser.reset_input_buffer()
            self.ser.write(packet)

            # K-line is half duplex: the cable echoes what we sent. The echo takes as long as the
            # packet takes on the wire (103 bytes = 215 ms at 4800 baud), so the timeout scales with it.
            if self._read(len(packet), self._byte_time(len(packet)) + 0.15) != packet:
                return b""

            # Find the reply header, skipping up to 8 stray bytes (K-line glitches at key-on).
            win = b""
            first = True
            while not win.endswith(b"\x80\xF0\x10"):
                b = self._read(1, ECU_REPLY_WAIT if first else 0.05)
                first = False
                if not b or len(win) > 10:
                    return b""
                win += b
            len_byte = self._read(1, 0.05)
            if not len_byte:
                return b""
            n = len_byte[0] + 1
            rest = self._read(n, self._byte_time(n) + 0.1)
            if len(rest) < n:
                return b""
            frame = b"\x80\xF0\x10" + len_byte + rest
            if calc_ssm2_checksum(frame[:-1]) != frame[-1]:
                return b""
            return rest[:-1]
        except (serial.SerialException, OSError):
            return b""  # unplugged mid-read

    def probe_ecu(self, tries=3) -> str:
        """Init the ECU. Returns the ROM ID hex string, or '' if no answer. Retries: one garbled
        frame at key-on should not read as 'no ECU'."""
        if not self.connect_serial():
            return ""
        resp = b""
        for _ in range(tries):
            resp = self.send_ssm2_command(bytes([0xBF]))
            if resp and len(resp) >= 9 and resp[0] == 0xFF:
                break
            time.sleep(0.2)
        self.init_raw = resp  # full init payload, for subaru_ssm/ssm2_capabilities.py
        # Response: 0xFF, SSM ID (3 bytes), ROM ID (5 bytes), capability bytes
        if resp and len(resp) >= 9 and resp[0] == 0xFF:
            self.ecu_online = True
            self.ssm_id = resp[1:4].hex().upper()
            self.rom_id = resp[4:9].hex().upper()
            self.capabilities = resp[9:]
            return self.rom_id
        return ""

    def _build_request(self) -> bytes:
        addrs = b""
        for _, addr, length, _ in PARAMS:
            for k in range(length):
                addrs += (addr + k).to_bytes(3, "big")
        return bytes([0xA8, 0x00]) + addrs

    def read_once(self) -> dict:
        data = self.send_ssm2_command(self._build_request())
        expected = 1 + sum(p[2] for p in PARAMS)
        if len(data) < expected or data[0] != 0xE8:
            return {}
        out, i = {}, 1
        for col, _, length, conv in PARAMS:
            out[col] = conv(data[i:i + length])
            i += length
        return out

    def _poll_worker(self):
        self._init_db_wal()
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        cur = conn.cursor()
        placeholders = ", ".join("?" for _ in range(len(COLUMNS) + 1))
        sql = f"INSERT INTO telemetry_bus (timestamp, {', '.join(COLUMNS)}) VALUES ({placeholders})"
        while self.running:
            frame = self.read_once()
            if frame:
                self.latest = frame
                cur.execute(sql, [time.time()] + [frame[c] for c in COLUMNS])
                conn.commit()
            time.sleep(0.1)
        conn.close()
        if self.ser and self.ser.is_open:
            self.ser.close()

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._poll_worker, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=1.0)


if __name__ == "__main__":
    poller = TelemetryPoller()
    print("[*] Probing Subaru ECU on /dev/ttyUSB0 (4800 baud)...")
    rom = poller.probe_ecu()
    if not rom:
        print("[!] ECU not responding on K-line. Key ON, engine off or running?")
        raise SystemExit(1)
    print(f"[+] SSM ID {poller.ssm_id}  ROM ID {rom}  ({len(poller.capabilities)} capability bytes)")
    poller.start()
    print("[*] Polling (Ctrl+C to stop)...")
    try:
        while True:
            time.sleep(1)
            if poller.latest:
                print("  ".join(f"{k}={v}" for k, v in poller.latest.items()))
    except KeyboardInterrupt:
        poller.stop()
