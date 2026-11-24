"""Pure string functions of app/cyberdeck_gui.py (verdict parsing, repair and part names) and the case-key
bookkeeping. Needs PyQt6 importable (no display: offscreen); skipped without it.
    python -m pytest kb/tests/test_gui_pure.py
The verdict texts are synthetic and cover several systems, not one car's fault.
"""
import inspect
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "app"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import cyberdeck_gui as G  # noqa: E402
except ImportError as e:
    G, WHY = None, str(e)


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class ParseVerdict(unittest.TestCase):
    PLAN = """<think>scratch work the model should not show</think>SUMMARY: Lean condition at idle on both banks.
=== ISSUE 1: Intake leak ===
WHY HERE: both banks lean, trims drop with rpm
FOR: smoke test showed smoke at the boot
AGAINST: none
TEST: smoke test the intake
REPAIR: Replace intake boot and clamp
PARTS: intake boot, clamp
=== ALSO DO: air filter ===
REPAIR: Replace air filter
PARTS: none
"""

    def test_summary_and_issues(self):
        summary, issues = G.CyberdeckApp.parse_verdict(self.PLAN)
        self.assertEqual(summary, "Lean condition at idle on both banks.")
        self.assertEqual([d["name"] for d in issues], ["Intake leak", "air filter"])
        self.assertEqual([d["also"] for d in issues], [False, True])
        self.assertEqual(issues[0]["TEST"], "smoke test the intake")
        self.assertEqual(issues[0]["PARTS"], "intake boot, clamp")

    def test_thinking_removed(self):
        summary, _ = G.CyberdeckApp.parse_verdict(self.PLAN)
        self.assertNotIn("scratch", summary)

    def test_markdown_decoration_tolerated(self):
        text = self.PLAN.replace("=== ISSUE 1: Intake leak ===", "**=== ISSUE 1: Intake leak ===**") \
                        .replace("TEST:", "- **TEST**:")
        _, issues = G.CyberdeckApp.parse_verdict(text)
        self.assertEqual(issues[0]["name"], "Intake leak")
        self.assertEqual(issues[0]["TEST"], "smoke test the intake")

    def test_format_ignored_gives_no_issues(self):
        summary, issues = G.CyberdeckApp.parse_verdict("Probably a vacuum leak, check the hoses.")
        self.assertEqual(issues, [])
        self.assertEqual(summary, "Probably a vacuum leak, check the hoses.")


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class RepairFromVerdict(unittest.TestCase):
    def test_root_cause_named(self):
        self.assertEqual(G.repair_from_verdict("1. ROOT CAUSE: failed ignition coil.\n2. WHAT RULES OUT: plugs fine."),
                         "ignition coil")
        self.assertEqual(G.repair_from_verdict("**1.** Timing belt jumped a tooth\n**2.** head gasket ruled out"),
                         "timing belt")

    def test_negated_part_skipped(self):
        self.assertEqual(G.repair_from_verdict("1. Not the O2 sensor. The fuel injector on cyl 3 is stuck.\n2. x"),
                         "fuel injector")
        self.assertIsNone(G.repair_from_verdict("1. The front O2 sensor reads normal. Vacuum leak at the boot.\n2. x"))

    def test_section_two_ignored(self):
        self.assertIsNone(G.repair_from_verdict("1. Wiring fault at the connector.\n2. Ruled out: thermostat, water pump"))

    def test_first_named_wins(self):
        self.assertEqual(G.repair_from_verdict("1. The water pump leaks, which overheated the head gasket.\n2. -"),
                         "water pump")

    def test_nothing_known(self):
        self.assertIsNone(G.repair_from_verdict("no useful verdict"))


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class PartsFromJob(unittest.TestCase):
    def test_cases(self):
        cases = {
            "Replace exhaust manifold gasket, clean mating surfaces, torque to spec.": ["exhaust manifold gasket"],
            "Replace spark plugs and ignition coil": ["spark plugs", "ignition coil"],
            "Clean and inspect throttle body": ["throttle body"],
            "Replace timing belt (belt, tensioner, idlers) and water pump": ["timing belt", "water pump"],
            "Replace the #3 fuel injector if needed": ["fuel injector"],
            "Check and replace PCV valve / hose": ["PCV valve", "hose"],
            "14038AA000": ["14038AA000"],
            "wheel bearing": ["wheel bearing"],
        }
        for job, want in cases.items():
            self.assertEqual(G.parts_from_job(job), want, job)

    def test_empty(self):
        self.assertEqual(G.parts_from_job(""), [])
        self.assertEqual(G.part_from_job(None), "")


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class CaseKeys(unittest.TestCase):
    def test_summary_reads_only_case_or_car_keys(self):
        src = inspect.getsource(G.Session.case_summary) + inspect.getsource(G.Session.telemetry_zones)
        read = set(re.findall(r'self\.get\("(\w+)"\)', src)) | set(re.findall(r'\("(\w+)", "[^"]', src))
        read |= {"drive_log", "drive_log_short"}
        app = G.CyberdeckApp
        self.assertFalse(read - set(app.CASE_KEYS) - set(app.CAR_KEYS),
                         "case_summary reads keys a new case never clears")

    def test_no_key_is_both(self):
        self.assertFalse(set(G.CyberdeckApp.CASE_KEYS) & set(G.CyberdeckApp.CAR_KEYS))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class ConfigMigration(unittest.TestCase):
    def test_old_defaults_dropped_once_and_choices_kept(self):
        data = {"home_fast": "qwen3:8b", "home_deep": "my-own-model", "home_think": "qwen3:30b-a3b-thinking-2507-q4_K_M"}
        self.assertTrue(G.migrate_config(data))
        self.assertEqual(data, {"home_deep": "my-own-model", "config_version": G.CONFIG_VERSION})
        data["home_fast"] = "qwen3:8b"
        self.assertFalse(G.migrate_config(data))
        self.assertEqual(data["home_fast"], "qwen3:8b")


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class Screens(unittest.TestCase):
    def test_page_numbers_follow_the_nav_order(self):
        self.assertEqual([n for n, _ in G.SCREENS], list(G.PAGE))
        for i, (name, label) in enumerate(G.SCREENS):
            self.assertEqual(G.PAGE[name], i)
            self.assertTrue(label.startswith(f"{i + 1}. "), label)


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class CaseMake(unittest.TestCase):
    def test_make_from_case_then_vin_never_a_default(self):
        home = sys.modules[G.case_make.__module__]
        orig = home.decode_vin
        home.decode_vin = lambda vin, db=None: {"make": {"1GN": "Chevrolet", "JF2": "Subaru"}.get(vin[:3], "")}
        try:
            self.assertEqual(G.case_make({"make": "Toyota", "vin": "JF2XXXXXXXXXXXXXX"}), "Toyota")
            self.assertEqual(G.case_make({"vin": "1GNXXXXXXXXXXXXXX"}), "Chevrolet")
            self.assertEqual(G.case_make({"vin": "JF2XXXXXXXXXXXXXX"}), "Subaru")
            self.assertEqual(G.case_make({"vin": "ZZZXXXXXXXXXXXXXX"}), "")
            self.assertEqual(G.case_make({}), "")
        finally:
            home.decode_vin = orig


@unittest.skipIf(G is None, "cyberdeck_gui not importable (PyQt6 missing?)")
class TeamdeckSwap(unittest.TestCase):
    def test_window_builds_its_model_manager_from_cyberdeck_gui(self):
        init = G.CyberdeckApp.__init__
        self.assertIs(init.__globals__, vars(G))
        self.assertIn("ModelManager", init.__code__.co_names)
