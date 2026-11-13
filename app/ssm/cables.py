"""
Which /dev/ttyUSB* is which cable, when the KKL cable and the OBDLink EX are both plugged in.
Both used to default to /dev/ttyUSB0, so with two cables one of them always opened the wrong one.
/dev/serial/by-id names the USB chip: the EX reports itself as OBDLink/ScanTool; anything else is
taken as the KKL cable. Callers try the returned ports in order.
On Windows and macOS there is no /dev/ttyUSB* or /dev/serial/by-id, so the ports and their USB names come from
pyserial's list_ports instead (COM3, /dev/cu.usbserial-...). Linux keeps the original lookup.
"""
import glob
import os
import re
import sys

EX_NAME = re.compile(r"obdlink|scantool|obd_solutions|stn\d", re.I)


LINUX = sys.platform.startswith("linux")


def _list_ports():
    """{port: USB name} from pyserial (Windows/macOS). Bluetooth/debug ports are left out."""
    try:
        from serial.tools import list_ports
    except (ImportError, OSError):
        return {}
    out = {}
    for p in list_ports.comports():
        if re.search(r"bluetooth|debug-console|wlan", p.device, re.I):
            continue
        out[p.device] = " ".join(x for x in (p.manufacturer, p.product, p.description) if x)
    return out


def _by_id():
    if not LINUX:
        return _list_ports()
    out = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            out[os.path.realpath(link)] = os.path.basename(link)
        except OSError:
            pass
    return out


def candidates(kind, configured=""):
    """Ports to try for kind 'kkl' or 'ex', best first. A configured port always comes first."""
    ports = all_ports()
    names = _by_id()
    ex = [p for p in ports if EX_NAME.search(names.get(p, ""))]
    unknown = [p for p in ports if p not in ex]
    order = ex + unknown if kind == "ex" else unknown + ex
    if configured:
        order = [configured] + [p for p in order if p != configured]
    return order or [default_port()]


def all_ports():
    """Every USB serial port that could be an OBD cable, on any OS."""
    if LINUX:
        return sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))
    return sorted(_list_ports())


def default_port():
    return "/dev/ttyUSB0" if LINUX else ("COM3" if sys.platform == "win32" else "/dev/cu.usbserial")


def no_cable_note(cable="OBDLink EX"):
    where = "/dev/ttyUSB*" if LINUX else "a USB serial port"
    return f"No {where} found. Is the {cable} plugged in (and its driver installed)?"


def ex_port(configured=""):
    """The EX's port: the configured one, else the one by-id names as the EX, else the best guess from
    candidates(). Never None: python-obd's own scan would send ELM commands down the KKL cable too."""
    if configured:
        return configured
    names = _by_id()
    return next((p for p in sorted(names) if EX_NAME.search(names[p])), None) or candidates("ex")[0]
