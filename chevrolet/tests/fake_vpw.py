"""Simulated OBDLink EX on a J1850 VPW GM truck, for offline tests of gm_live.py. Values are made up for the test;
the frame layout (3 header bytes, data, CRC) is what an ELM/STN prints with ATH1 on J1850."""


def crc(b):                       # J1850 CRC-8 (poly 0x1D, init 0xFF, inverted)
    c = 0xFF
    for x in b:
        c ^= x
        for _ in range(8):
            c = ((c << 1) ^ 0x1D) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c ^ 0xFF


def frame(*b):
    return " ".join(f"{x:02X}" for x in (*b, crc(b)))


class FakeVPW:
    VIN = b"1GNEK13T23R000000"           # test VIN, not a real car

    def __init__(self):
        self.out, self.header, self.proto, self.sent = b"", "686AF1", "0", []
        self.in_waiting = 0

    def reset_input_buffer(self):
        pass

    def close(self):
        pass

    def write(self, data):
        cmd = data.decode().strip().upper()
        self.sent.append(cmd)
        self.out = (self.answer(cmd) + "\r\r>").encode()
        self.in_waiting = len(self.out)

    def read(self, n=1):
        b, self.out = self.out[:n], self.out[n:]
        self.in_waiting = len(self.out)
        return b

    def answer(self, c):
        if c.startswith("AT") or c.startswith("ST"):
            if c == "ATI":
                return "ELM327 v1.4b"
            if c == "STI":
                return "STN2230 v5.6.19"
            if c == "ATRV":
                return "12.6V"
            if c == "ATDPN":
                return "A2" if self.proto == "0" else self.proto
            if c.startswith("ATSP"):
                self.proto = c[4:]
            if c.startswith("ATSH"):
                self.header = c[4:]
            return "OK"
        h = (0x48, 0x6B, 0x10)
        if c.startswith("22"):
            if self.header != "6C10F1":
                return "NO DATA"
            hi, lo = int(c[2:4], 16), int(c[4:6], 16)
            data = {(0x12, 0x05): [0x03], (0x19, 0x40): [0x5A], (0x11, 0x00): [0x09]}.get((hi, lo))
            return frame(0x6C, 0xF1, 0x10, 0x62, hi, lo, *data) if data else frame(0x6C, 0xF1, 0x10, 0x7F, 0x22, hi, lo, 0x31)
        if self.header != "686AF1":
            return "NO DATA"
        pid = c[2:4] if len(c) >= 4 else ""
        if c == "0100":
            return frame(*h, 0x41, 0x00, 0xBE, 0x3E, 0xB8, 0x11)
        if c == "0120":
            return frame(*h, 0x41, 0x20, 0x80, 0x00, 0x00, 0x00)
        vals = {"0C": [0x0A, 0xF0], "05": [0x7B], "0F": [0x3C], "04": [0x40], "11": [0x24], "1C": [0x01],
                "06": [0x80], "07": [0x86], "0B": [0x22], "0D": [0x00], "0E": [0x94]}
        if c.startswith("01") and pid in vals:
            return frame(*h, 0x41, int(pid, 16), *vals[pid])
        if c == "03":
            return frame(*h, 0x43, 0x01, 0x71, 0x04, 0x42, 0x00, 0x00)   # P0171, P0442 (arbitrary test codes)
        if c == "07":
            return frame(*h, 0x47, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00)
        if c == "0902":
            v = [0, 0, 0] + list(self.VIN)
            return "\r".join(frame(*h, 0x49, 0x02, i + 1, *v[i * 4:i * 4 + 4]) for i in range(5))
        return "NO DATA"
