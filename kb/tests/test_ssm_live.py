"""The SSM live line shows what each ECU's init reply advertises, not a list tuned to one ECU.
Init replies here are synthetic: built from FreeSSM's measuring-block table, several engines and feature sets."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "app", "ssm"))

import pytest  # noqa: E402

serial = pytest.importorskip("serial")
import ssm2_capabilities as C  # noqa: E402
import ssm_reader as R  # noqa: E402

TABLES = C.load_tables()


def init_reply(titles, sys_id=b"\xa2\x10\x11"):
    flags = bytearray(48)
    for m in TABLES["measuring_blocks"]:
        if m["title"] in titles and m["cu"] & 1:
            flags[m["flagbyte"] - 1] |= 1 << (m["bit"] - 1)
    return b"\xff" + sys_id + b"\x00\x00\x00\x00\x00" + bytes(flags)


CORE = ["Engine Speed", "Vehicle Speed", "Coolant Temperature", "Battery Voltage"]
ROUGH = [f"Roughness Monitor Cylinder #{n}" for n in (1, 2, 3, 4)]


def keys(titles):
    return [k for _, k, _ in R.live_items(C.ECUCaps(init_reply(titles)))]


def test_counters_shown_when_advertised():
    k = keys(CORE + ROUGH)
    assert {"rpm", "speed_kph", "ect_c", "battery_v"} <= set(k)
    assert ["cyl1_misfires", "cyl2_misfires", "cyl3_misfires", "cyl4_misfires"] == [x for x in k if "misfire" in x]


def test_counters_hidden_when_not_advertised():
    k = keys(CORE)
    assert "rpm" in k and not [x for x in k if "misfire" in x]


def test_only_advertised_core_values():
    k = keys(["Engine Speed", "Coolant Temperature"])
    assert k == ["rpm", "ect_c"]


def test_no_flags_falls_back_to_core_without_counters():
    caps = C.ECUCaps(b"\xff\xa2\x10\x11\x00\x00\x00\x00\x00")
    k = [x for _, x, _ in R.live_items(caps)]
    assert "rpm" in k and not [x for x in k if "misfire" in x]


def test_display_order_kept():
    order = [x for _, x, _ in R.LIVE]
    k = keys(CORE + ROUGH)
    assert k == sorted(k, key=order.index)
