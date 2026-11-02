"""Reading the car (read-only). FakeCar answers from a scenario file; RealCar uses the main app's SSM2 reader
for Subarus (ssm_reader.read_ssm: only 0xBF init and 0xA8 reads are ever sent) and python-obd mode 03 + mode 01
for everything else. Not run on a car yet (milestone 1 is tested on scenarios)."""


class FakeCar:
    def __init__(self, spec):
        self.spec = spec or {}
        self.cable = self.spec.get("cable", "")

    # simulated time for watch_live: "watch": {key: {"unit": u, "values": [...]}} plays one value per sample
    def clock(self):
        return getattr(self, "_t", 0.0)

    def wait(self, s):
        self._t = self.clock() + max(s, 0.5)
        self._i = getattr(self, "_i", 0) + 1

    def sample(self, keys):
        """Raw readings per source for this sample; None once every changing series has been played."""
        from .watch import raw_for
        w, i = self.spec.get("watch", {}), getattr(self, "_i", 0)
        moving = [x for x in w.values() if not x.get("hold")]
        if not moving or i >= max(len(x["values"]) for x in moving):
            return None
        groups = {}
        for k in keys:
            ser = w.get(k)
            if ser:
                src, raw = raw_for(k, ser["values"][min(i, len(ser["values"]) - 1)], ser["unit"])
                if src:
                    groups.setdefault(src, []).append(raw)
        return groups

    def read_live(self, key):
        """(value, unit) the simulated car reports for a canonical telemetry key, or None."""
        v = self.spec.get("live_values", {}).get(key)
        return (float(v[0]), v[1]) if v else None

    def identify(self):
        i = self.spec.get("ident", {})
        return {"vin": i.get("vin"), "ecu": i.get("ecu"), "ssm": bool(i.get("ecu")), "cable": self.cable,
                "notes": ["(simulated car)"]}

    def read(self):
        return {"codes": list(self.spec.get("codes", [])), "live": self.spec.get("live", ""),
                "note": self.spec.get("note", "(simulated car)")}


class NoCar:
    can_scan = False      # no cable: steps about stored codes are asked of the user

    def identify(self):
        return {"notes": ["No cable read: identification from what was typed."]}

    def read(self):
        return {"codes": None, "live": "", "note": "No cable read in this run (codes as typed at the start)."}


class RealCar:
    LIVE_PIDS = [("RPM", "RPM", ""), ("SPEED", "Speed", " km/h"), ("COOLANT_TEMP", "ECT", " C"),
                 ("SHORT_FUEL_TRIM_1", "STFT1", " %"), ("LONG_FUEL_TRIM_1", "LTFT1", " %"),
                 ("INTAKE_PRESSURE", "MAP", " kPa"), ("THROTTLE_POS", "TPS", " %"),
                 ("CONTROL_MODULE_VOLTAGE", "Volts", " V")]

    def __init__(self, make, cfg):
        self.make, self.cfg = make, cfg
        self.cable = "KKL" if (make or "") == "Subaru" else "EX"

    def clock(self):
        import time
        return time.time()

    def wait(self, s):
        import time
        time.sleep(s)

    def sample(self, keys):
        """One live read as raw readings per source, for telemetry_ref.normalize()."""
        try:
            import telemetry_ref
        except ImportError:
            return None
        res = self.read()
        source = "ssm" if self.cable == "KKL" else "obd01"
        raws = telemetry_ref.from_live_text(res.get("live", ""), source)
        return {source: raws} if raws else None

    def read_live(self, key):
        """One live value through app/telemetry_ref.py (canonical units), or None when this read can't give it."""
        try:
            import telemetry_ref
        except ImportError:
            return None
        res = self.read()
        source = "ssm" if self.cable == "KKL" else "obd01"
        for r in telemetry_ref.normalize(telemetry_ref.from_live_text(res.get("live", ""), source), source):
            if r["key"] == key:
                return r["value"], r["unit"]
        return None

    def identify(self):
        """Mode 09 VIN first (read-only), then an SSM2 init on the K-line (only Subaru ECUs answer it)."""
        out = {"notes": []}
        try:
            import obd
            conn = obd.OBD(self.cfg.get("obd_port") or None, fast=False, timeout=2)
            if conn.is_connected():
                r = conn.query(obd.commands.VIN, force=True)
                v = r.value if not r.is_null() else ""
                v = (v.decode(errors="ignore") if isinstance(v, (bytes, bytearray)) else str(v or "")).strip(" \x00")
                if v:
                    out["vin"] = v
                else:
                    out["notes"].append(f"No VIN over OBD-II ({conn.protocol_name() or 'protocol unknown'})")
            conn.close()
        except Exception as e:
            out["notes"].append(f"OBD-II VIN read failed: {e}")
        if not out.get("vin") and (self.make or "Subaru") == "Subaru":
            import re
            import ssm_reader
            codes, live, note = ssm_reader.read_ssm(self.cfg.get("obd_port", ""), self.cfg["session_db"],
                                                    self.cfg["cache_dir"])
            m = re.search(r"\bROM ([0-9A-Fa-f ]{10,})", note or "")
            if m:
                import os
                self.cable = "KKL"
                out.update(ecu=re.sub(r"[^0-9A-F]", "", m.group(1).upper()), ssm=True, cable="KKL",
                           init_hex=os.path.join(self.cfg["cache_dir"], "ecu_init_reply.hex"))
            out["notes"].append(note)
        return out

    def read(self):
        if (self.make or "").lower() == "subaru":
            import ssm_reader
            codes, live, note = ssm_reader.read_ssm(self.cfg.get("obd_port", ""), self.cfg["session_db"],
                                                    self.cfg["cache_dir"])
            if codes or live:
                return {"codes": [c.strip() for c in codes.split(",") if c.strip()], "live": live, "note": note}
        return self._obd()

    def _obd(self):
        try:
            import obd
        except ImportError:
            return {"codes": None, "live": "", "note": "python-obd not installed"}
        conn = obd.OBD(self.cfg.get("obd_port") or None, fast=False, timeout=2)
        try:
            if not conn.is_connected():
                return {"codes": None, "live": "", "note": f"no OBD-II connection ({conn.status()})"}
            r = conn.query(obd.commands.GET_DTC)
            codes = [c for c, _ in (r.value or [])]
            live = []
            for pid, label, unit in self.LIVE_PIDS:
                v = conn.query(getattr(obd.commands, pid))
                if not v.is_null():
                    mag = getattr(v.value, "magnitude", v.value)
                    live.append(f"{label} {mag:.1f}{unit}" if isinstance(mag, float) else f"{label} {mag}{unit}")
            return {"codes": codes, "live": " | ".join(live), "note": "generic OBD-II (mode 03 + mode 01)"}
        finally:
            conn.close()
