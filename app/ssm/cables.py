"""
Which /dev/ttyUSB* is which cable, when the KKL cable and the OBDLink EX are both plugged in.
Both used to default to /dev/ttyUSB0, so with two cables one of them always opened the wrong one.
/dev/serial/by-id names the USB chip: the EX reports itself as OBDLink/ScanTool; anything else is
taken as the KKL cable. Callers try the returned ports in order.
"""
import glob
import os
import re

EX_NAME = re.compile(r"obdlink|scantool|obd_solutions|stn\d", re.I)


def _by_id():
    out = {}
    for link in glob.glob("/dev/serial/by-id/*"):
        try:
            out[os.path.realpath(link)] = os.path.basename(link)
        except OSError:
            pass
    return out


def candidates(kind, configured=""):
    """Ports to try for kind 'kkl' or 'ex', best first. A configured port always comes first."""
    ports = sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))
    names = _by_id()
    ex = [p for p in ports if EX_NAME.search(names.get(p, ""))]
    unknown = [p for p in ports if p not in ex]
    order = ex + unknown if kind == "ex" else unknown + ex
    if configured:
        order = [configured] + [p for p in order if p != configured]
    return order or ["/dev/ttyUSB0"]


def ex_port(configured=""):
    """The EX's port: the configured one, else the one by-id names as the EX, else the best guess from
    candidates(). Never None: python-obd's own scan would send ELM commands down the KKL cable too."""
    if configured:
        return configured
    names = _by_id()
    return next((p for p in sorted(names) if EX_NAME.search(names[p])), None) or candidates("ex")[0]
