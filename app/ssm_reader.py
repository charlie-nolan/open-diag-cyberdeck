"""
Read-only Subaru SSM2 for the Intake tab, through either cable:
  - OBDLink EX (main path): the STN2230 carries SSM2 frames on the K-line (app/ssm/ex_ssm2_probe.py,
    a copy of research/obdlink_ex/ex_ssm2_probe.py; re-copy it when that thread updates it).
  - bare KKL cable (fallback): app/ssm/telemetry_logger.py talks to the K-line directly.

ECUCaps + read_dtcs give the trouble codes the ECU itself says it supports; TelemetryPoller's table
decodes the live values. Only 0xBF (init) and 0xA8 (read) are ever sent. Nothing here writes to the ECU.
"""
import glob
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssm"))

# Live values worth showing in the case. Roughness counters are left out until the
# saved init reply confirms this 2002 ECU really supports them (see ssm/README.md).
LIVE = [("RPM", "rpm", ""), ("Speed", "speed_kph", " km/h"), ("ECT", "ect_c", " C"), ("STFT1", "stft_pct", " %"), ("LTFT1", "ltft_pct", " %"),
        ("MAP", "map_kpa", " kPa"), ("Timing", "timing_advance_deg", " deg"), ("Knock corr", "knock_corr_deg", " deg"),
        ("IAT", "iat_c", " C"), ("TPS", "tps_pct", " %"), ("O2 front", "front_o2_v", " V"), ("Volts", "battery_v", " V")]


def _read_ecu(link, poller, cache_dir, hex_name):
    """Codes, live line and note from an ECU that has answered the SSM2 init. link sends the commands."""
    from ssm2_capabilities import ECUCaps, read_dtcs, read_vin
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, hex_name), "w") as f:
        f.write(link.init_raw.hex(" ").upper() + "\n")
    caps = ECUCaps(link.init_raw)
    dtcs = read_dtcs(link, caps)
    if dtcs is None:
        codes, dtc_note = "", "DTC read failed (ECU answered the init but not the code read; try again)"
    else:
        cur = [c for c, _ in dtcs["current"]]
        mem = [c for c, _ in dtcs["memorized"] if c not in cur]
        codes = ", ".join(cur + mem)
        dtc_note = f"current: {', '.join(cur) or 'none'}; memorized: {', '.join(mem) or 'none'}"
        if dtcs.get("note"):
            dtc_note += f" ({dtcs['note']})"
    frame = poller.read_once()
    live = " | ".join(f"{label} {frame[k]}{unit}" for label, k, unit in LIVE if k in frame) or "(no live data)"
    try:
        vin = read_vin(link, caps)
    except Exception:
        vin = ""
    vin_note = f" VIN {vin}." if vin else (" This ECU doesn't store a VIN." if not caps.has_vin else "")
    return codes, live, f"SSM {link.ssm_id} ({caps.engine}) ROM {link.rom_id}. {dtc_note}.{vin_note}"


def read_ssm(port, session_db, cache_dir):
    """Bare KKL cable. Returns (codes, live, note). codes: 'P0303, P0304' (current + memorized)."""
    try:
        from telemetry_logger import TelemetryPoller
    except ImportError as e:
        return "", "", f"SSM code missing ({e}). pip install pyserial; app/ssm/ must be present."

    from cables import candidates
    ports = candidates("kkl", port)
    err = ""
    for attempt in range(2):  # the first try after plugging in often gets no answer
        for p in ports:
            poller = None
            try:
                poller = TelemetryPoller(port=p, db_path=session_db)
                if poller.probe_ecu():
                    return _read_ecu(poller, poller, cache_dir, "ecu_init_reply.hex")
            except Exception as e:
                err = f" ({p}: {e})"
            finally:
                if poller is not None and getattr(poller, "ser", None) is not None and poller.ser.is_open:
                    poller.ser.close()
    return "", "", (f"No answer on the K-line (tried {', '.join(ports)} twice){err}. Key ON? "
                    "Your user needs to be in the dialout group.")


def read_ssm_ex(port, session_db, cache_dir, variant_name=""):
    """SSM2 through the OBDLink EX. Returns (codes, live, note, variant that worked or '').
    Not proven on a car yet: every byte goes to cache/ex_ssm_last.log so a failure can be read back."""
    try:
        import ex_ssm2_probe as ex_mod
        from telemetry_logger import TelemetryPoller
    except ImportError as e:
        return "", "", f"SSM code missing ({e}). pip install pyserial; app/ssm/ must be present.", ""
    from cables import candidates
    port = port or candidates("ex")[0]
    if not glob.glob("/dev/ttyUSB*") and not port.startswith("/dev/ttyACM"):
        return "", "", "No /dev/ttyUSB* found. Is the OBDLink EX plugged in?", ""
    os.makedirs(cache_dir, exist_ok=True)
    log = ex_mod.Log(os.path.join(cache_dir, "ex_ssm_last.log"))
    ex = ex_mod.STNSSM2(port, log)
    try:
        if not ex.open():
            return "", "", "OBDLink EX didn't answer on the serial port.", ""
        variants = sorted(ex_mod.VARIANTS, key=lambda v: v["name"] != variant_name)  # last good one first
        for v in variants:
            ex.setup(v)
            if ex.probe_ecu():
                poller = TelemetryPoller(port=port, db_path=session_db)  # used only for its live-value table
                poller.send_ssm2_command = ex.send_ssm2_command
                codes, live, note = _read_ecu(ex, poller, cache_dir, "ecu_init_reply_ex.hex")
                return codes, live, note, v["name"]
        return "", "", f"No SSM answer through the EX ({ex.last_error}). Not a Subaru, or key off.", ""
    except Exception as e:
        return "", "", f"SSM over the EX failed: {e}", ""
    finally:
        try:
            ex.close()
        except Exception:
            pass
