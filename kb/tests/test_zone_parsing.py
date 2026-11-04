"""Edge cases for the telemetry zone parser (kb/build_telemetry.py): parse_values, the table readers, the
number check, unit_ok and conditions_from. A bug here would store a wrong normal range under a manual's name.
    python -m pytest kb/tests/test_zone_parsing.py
Table text is synthetic, written in the layouts the manuals use. No data files needed.
"""
import os
import sqlite3
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, os.path.join(ROOT, "kb"))
import build_telemetry as B  # noqa: E402


def bounds(text, hint=None):
    return [(lo, hi, typ, B.T.unit_norm(u) if u else None) for lo, hi, typ, u, *_ in B.parse_values(text, hint)]


class ParseValues(unittest.TestCase):
    def test_plain_range(self):
        self.assertEqual(bounds("0.3 - 0.8 V"), [(0.3, 0.8, None, "V")])

    def test_word_and_en_dash_ranges(self):
        self.assertEqual(bounds("750 to 850 rpm"), [(750, 850, None, "rpm")])
        self.assertEqual(bounds("13.5–14.8 V"), [(13.5, 14.8, None, "V")])

    def test_unicode_minus(self):
        self.assertEqual(bounds("−10 - 10 %"), [(-10, 10, None, "%")])

    def test_degree_unit_spacing(self):
        self.assertEqual(bounds("80 to 95°C"), bounds("80 to 95 ° C"))
        self.assertEqual(bounds("80 to 95°C")[0][:2], (80, 95))

    def test_plus_minus(self):
        self.assertEqual(bounds("2.0 ± 0.2 ms"), [(1.8, 2.2, 2.0, "ms")])

    def test_limits_both_sides(self):
        self.assertEqual(bounds("More than 12 V"), [(12, None, None, "V")])
        self.assertEqual(bounds("4.5 V or less"), [(None, 4.5, None, "V")])
        self.assertEqual(bounds("Below 40 kPa"), [(None, 40, None, "kPa")])

    def test_reversed_range_dropped(self):
        self.assertEqual(B.parse_values("650-75 rpm"), [])
        self.assertEqual(B.parse_values("4.5 - 0.5 V"), [])

    def test_discrete_list_left_out(self):
        self.assertEqual(B.parse_values("0 or 5 V"), [])

    def test_no_bounds_no_zone(self):
        self.assertEqual(B.parse_values("Approx. 1.4 V"), [])
        self.assertEqual(B.parse_values("ON"), [])

    def test_two_ranges_in_one_cell(self):
        self.assertEqual([b[:2] for b in bounds("1.0–1.5 V / 3.8–4.3 V")], [(1.0, 1.5), (3.8, 4.3)])

    def test_condition_after_value(self):
        got = B.parse_values("750 to 850 rpm: idle")
        self.assertEqual(got[0][5], "idle")

    def test_unit_hint_used_when_cell_has_none(self):
        self.assertEqual(bounds("600-700", "rpm"), [(600, 700, None, "rpm")])
        self.assertEqual(bounds("600-700"), [(600, 700, None, None)])

    def test_other_unit_echo_dropped(self):
        # '(–450 mmHg, –17.72 inHg)' repeats the kPa value: it must not become a second zone
        self.assertEqual(len(B.parse_values("Less than –60.0 kPa (–450 mmHg, –17.72 inHg)")), 1)


class Tables(unittest.TestCase):
    def test_markdown_row_span_fill_down(self):
        rows = B.md_rows("| Item | Cond | Value |\n|---|---|---|\n"
                         "| Coolant temp | Idling | 80 - 95 °C |\n|  | 2,500 rpm | 85 - 100 °C |")
        self.assertEqual(rows[2], ["Coolant temp", "2,500 rpm", "85 - 100 °C"])

    def test_markdown_value_continuation_line(self):
        rows = B.md_rows("| Item | Cond | Value |\n|---|---|---|\n| MAF | Idling | 7.1 |\n| 7.1 | — 14.2 | x |")
        self.assertEqual(rows[-1][0], "MAF")

    def test_markdown_separator_skipped(self):
        self.assertEqual(B.md_rows("|---|:--:|\nplain text\n| a | b |"), [["a", "b"]])

    def test_charm_html_table(self):
        page = ("<table><tr><th>Parameter</th><th>Units</th><th>Typical</th></tr>"
                "<tr><td>Engine Speed</td><td>RPM</td><td>600-700</td></tr></table>")
        self.assertEqual(B.html_rows(page), [["Parameter", "Units", "Typical"], ["Engine Speed", "RPM", "600-700"]])

    def test_charm_indent_columns(self):
        page = ("Engine Speed<span class='indent-24'>\t</span>RPM<span class='indent-48'>\t</span>600-700<br>\n\n"
                "MAP<span class='indent-24'>\t</span>kPa<span class='indent-48'>\t</span>20-48<br>")
        self.assertEqual(B.html_rows(page), [["Engine Speed", "RPM", "600-700"], ["MAP", "kPa", "20-48"]])

    def test_units_column_becomes_hint(self):
        rows = [["Parameter", "Units", "Typical"], ["Engine Speed", "RPM", "600-700"]]
        (name, cell, own, ctext, hint, quote, _), = B.zone_rows(rows, "")
        self.assertEqual((name, cell, hint), ("Engine Speed", "600-700", "RPM"))
        self.assertEqual(quote, "Engine Speed | RPM | 600-700")

    def test_waveform_rows_skipped(self):
        rows = [["Item", "Value"], ["Injector signal", "waveform 2 - 4 ms"]]
        self.assertEqual(B.zone_rows(rows, ""), [])

    def test_row_condition_cell_kept(self):
        rows = [["Item", "Cond", "Value"], ["Engine speed", "Idling", "650 - 750 rpm"]]
        self.assertEqual(B.zone_rows(rows, "")[0][2], "Idling")


class NumberCheck(unittest.TestCase):
    def test_bounds_in_quote(self):
        self.assertTrue(B.numbers_quoted(600, 700, None, "Engine Speed | RPM | 600-700"))

    def test_bound_not_in_quote_rejected(self):
        self.assertFalse(B.numbers_quoted(600, 750, None, "Engine Speed | RPM | 600-700"))

    def test_negative_bound_matches_unicode_minus(self):
        self.assertTrue(B.numbers_quoted(-10, 10, None, "−10 - 10 %"))

    def test_open_bound_ignored(self):
        self.assertTrue(B.numbers_quoted(None, 40, None, "Below 40 kPa"))

    def test_plus_minus_bounds_are_computed(self):
        self.assertTrue(B.numbers_quoted(1.8, 2.2, 2.0, "2.0 ± 0.2 ms"))

    def test_parsed_values_pass_their_own_quote(self):
        for text in ("0.3 - 0.8 V", "13.5–14.8 V", "More than 12 V", "−10 - 10 %", "80 to 95°C"):
            for lo, hi, typ, *_ in B.parse_values(text):
                self.assertTrue(B.numbers_quoted(lo, hi, typ, text), text)


class Units(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(B.SCHEMA)

    def tearDown(self):
        self.db.close()

    def test_unit_fits_quantity(self):
        self.assertEqual(B.unit_ok("ect", "°C", self.db), ("ect", "°C"))
        self.assertEqual(B.unit_ok("ect", "°F", self.db), ("ect", "°F"))     # converted later, not guessed
        self.assertEqual(B.unit_ok("map", "psi", self.db), ("map", "psi"))

    def test_unit_does_not_fit(self):
        self.assertIsNone(B.unit_ok("ect", "V", self.db))
        self.assertIsNone(B.unit_ok("rpm", "kPa", self.db))
        self.assertIsNone(B.unit_ok("ect", "bogus", self.db))

    def test_no_unit_no_zone(self):
        self.assertIsNone(B.unit_ok("map", None, self.db))

    def test_voltage_goes_to_sibling(self):
        self.assertEqual(B.unit_ok("tps", "V", self.db), ("tps_v", "V"))
        self.assertEqual(B.unit_ok("tps", "%", self.db), ("tps", "%"))


class Conditions(unittest.TestCase):
    def name(self, text, page=""):
        got = B.conditions_from(text, page)
        return got[0][0] if got else None

    def test_checkable_states(self):
        self.assertEqual(self.name("Idling"), "idle")
        self.assertEqual(self.name("engine idling"), "idle")
        self.assertEqual(self.name("Ignition SW ON (Engine OFF)"), "koeo")
        self.assertEqual(self.name("Cranking"), "cranking")
        self.assertEqual(self.name("2,500 rpm"), "rpm2500")

    def test_warm_page_adds_coolant_threshold(self):
        name, _, defn, kind = B.conditions_from("Idling", "After warm-up the engine.")[0]
        self.assertEqual(name, "idle_warm")
        self.assertEqual(defn["ect"], [B.WARM_C, None])
        self.assertEqual(kind, "generic-threshold")

    def test_warm_does_not_apply_to_engine_off(self):
        self.assertEqual(self.name("Ignition SW ON (Engine OFF)", "After warm-up the engine."), "koeo")
        self.assertEqual(self.name("Cranking", "After warm-up the engine."), "cranking")

    def test_stated_rpm_gets_generic_tolerance(self):
        _, _, defn, kind = B.conditions_from("2,500 rpm")[0]
        self.assertEqual(defn["rpm"], [2250, 2750])
        self.assertEqual(kind, "generic-tolerance")

    def test_rpm_below_500_is_not_a_condition(self):
        self.assertEqual(B.conditions_from("300 rpm"), [])

    def test_not_checkable_rejects_row(self):
        for text in ('Idle speed [A/C "ON", no load]', "Engine on, A/C switch ON", "headlights on, idling",
                     "Idle with blower on", "steering wheel fully turned, idle", "idling when loaded",
                     "idle with electrical load"):
            self.assertEqual(B.conditions_from(text), [], text)

    def test_unknown_text_gives_nothing(self):
        self.assertEqual(B.conditions_from("Throttle fully closed"), [])
        self.assertEqual(B.conditions_from(""), [])


if __name__ == "__main__":
    unittest.main()
