"""Trouble-code system from the code and its own description: the same rule for every make."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from build_subaru_diag_db import module_for  # noqa: E402


def test_generic_groups():
    assert module_for("P0301") == "engine"
    assert module_for("P0171") == "engine"
    assert module_for("P0700") == "transmission"
    assert module_for("P0841") == "transmission"
    assert module_for("P0965") == "transmission"
    assert module_for("P2700") == "transmission"
    assert module_for("P2096") == "engine"
    assert module_for("P0A80") == "hybrid"
    assert module_for("P3400") == "engine"


def test_manufacturer_codes_are_not_guessed():
    assert module_for("P1700") is None
    assert module_for("P1750", "Solenoid Valve Range/Performance, Shift Solenoid A") == "transmission"
    assert module_for("P1101", "Mass Air Flow Sensor In Range But Not Correct") is None
    assert module_for("P1740", "Torque converter clutch slip") == "transmission"
    assert module_for("P3000") is None
    assert module_for("P1491", "Idle air control at idle") is None
    assert module_for("P1702", "AT diagnosis input signal circuit") == "transmission"


def test_other_letters_and_bad_input():
    assert module_for("C0035") == "chassis"
    assert module_for("B1000") == "body"
    assert module_for("U0100") == "network"
    assert module_for("P03") is None
    assert module_for("") is None
