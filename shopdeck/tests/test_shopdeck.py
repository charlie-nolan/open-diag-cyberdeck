"""python3 -m unittest discover -s tests   (from the shopdeck folder)

Most tests need the knowledge database: set SHOPDECK_KB=/path/to/subaru.db, else kb/subaru.db in this checkout.
They check behaviour across several cars and codes, not one car's case."""
import os
import tempfile
import unittest

os.environ.setdefault("SHOPDECK_HOME", tempfile.mkdtemp(prefix="shopdeck-test-"))

from shopdeck import chart as C
from shopdeck import repair as R
from shopdeck.case import Case, Note

KB_PATH = os.environ.get("SHOPDECK_KB", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "kb", "subaru.db"))
HAVE_KB = os.path.isfile(KB_PATH)


class Limits(unittest.TestCase):
    def test_single_bounds(self):
        l = C.parse_limit("Is the voltage more than 10 V?")
        self.assertEqual((l.op, l.lo, l.unit), (">", 10, "V"))
        self.assertTrue(l.judge(10.5) and not l.judge(10))
        l = C.parse_limit("Is the resistance less than 1 Ω?")
        self.assertEqual((l.op, l.lo), ("<", 1))

    def test_ranges(self):
        l = C.parse_limit("Is the voltage between 0.5 and 4.5 V?")
        self.assertEqual((l.op, l.lo, l.hi, l.unit), ("between", 0.5, 4.5, "V"))
        self.assertTrue(l.judge(0.5) and l.judge(4.5) and not l.judge(4.6))
        l = C.parse_limit("Is the value equal to or more than 0.85 and equal to less than 1.15?")
        self.assertEqual((l.op, l.lo, l.hi), ("between", 0.85, 1.15))

    def test_no_limit_for_changes(self):
        self.assertIsNone(C.parse_limit("Does the voltage change by shaking harness and connector?"))
        self.assertIsNone(C.parse_limit("Is there poor contact in connector?"))

    def test_readings(self):
        l = C.Limit("<", 1, unit="kΩ")
        self.assertEqual(C.parse_reading("0.4 2M", l), [0.4, 2000])
        self.assertEqual(C.parse_reading("12 13.1 -0.5", C.Limit(">", 10, unit="V")), [12, 13.1, -0.5])


class OlderLayout(unittest.TestCase):
    """1999-2001 manuals: '10AA2 : CHECK ...' steps, YES/NO either printed as labels or lost (pictures)."""
    def test_labels_and_order(self):
        lines = ["10AA1 : CHECK ANY OTHER DTC.", ": Does the scan tool indicate DTC P0101?",
                 ": Inspect DTC P0101.", "NOTE: no need to inspect P0301.", ": Go to step 10AA2.",
                 "### 10AA2 : CHECK OUTPUT SIGNAL.", "- 1) Turn ignition switch to ON.",
                 "- CHECK : *Is the voltage more than 10 V?*", "- YES : Go to step 10AA3.", "- NO : Repair harness.",
                 "10AA3 : CHECK INJECTOR.", ": Is the resistance between 5 and 20 \\Omega ? : Go to step 10AB1. : Replace injector."]
        steps, problems = C.parse_labelled(lines)
        self.assertIn(C.ORDER, problems)
        self.assertEqual(steps[1].no.step, 2)
        self.assertIn("P0301", steps[1].yes.text)
        self.assertEqual((steps[2].yes.step, steps[2].no.kind, steps[2].instructions), (3, "action", ["1) Turn ignition switch to ON."]))
        self.assertEqual(steps[3].limit.op, "between")
        self.assertIsNone(steps[3].yes.step)          # another chart's step is not a step in this one

    def test_unclear_step_is_not_walked(self):
        steps, problems = C.parse_labelled(["10B1 : CHECK X.", ": Is it more than 1 V?", ": Go to step 10B2."])
        self.assertTrue(steps[1].broken)              # kept as page text for the mechanic, never guessed
        self.assertEqual(steps[1].question, "")
        self.assertIn(": Go to step 10B2.", steps[1].raw)


class Quote(unittest.TestCase):
    def test_math_and_missing(self):
        j = R.Job("Replace part", "test")
        j.labor.append(R.LaborLine("Replace", 1.5, "book"))
        j.parts.append(R.PartLine("Gasket", "X1", 2, 3.25, "x"))
        j.parts.append(R.PartLine("Bolt", "", 4, None, "x"))
        q = R.quote([j], 120, tax_rate=0.1, supplies_rate=0.05)
        self.assertEqual((q.labor, q.parts, q.tax, q.supplies), (180.0, 6.5, 0.65, 9.0))
        self.assertEqual(q.total, 196.15)
        self.assertEqual(len(q.missing), 1)

    def test_prices(self):
        self.assertEqual(R.price_of("$1,169.95"), 1169.95)
        self.assertIsNone(R.price_of(""))


class Tickets(unittest.TestCase):
    def test_round_trip(self):
        c = Case(model="Impreza", year=1999, codes=["P0420"])
        c.notes.append(Note("reused gasket", "reused"))
        j = R.Job("Job", "why", procedures=[1])
        j.parts.append(R.PartLine("P", "N", 1, None, "s"))
        c.jobs.append(j)
        c.save()
        d = Case.load(c.path)
        self.assertEqual((d.model, d.codes, d.notes[0].kind, d.jobs[0].parts[0].unit_price), ("Impreza", ["P0420"], "reused", None))

    def test_empty(self):
        self.assertTrue(Case().empty)
        self.assertFalse(Case(customer="x").empty)


@unittest.skipUnless(HAVE_KB, f"no knowledge database at {KB_PATH}")
class WithKB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shopdeck.kb import KB
        cls.kb = KB(KB_PATH)

    def veh(self, model, year, engine_has=""):
        for v in self.kb.vehicles(model, year):
            if engine_has in v.engine:
                return v
        self.skipTest(f"{year} {model} not in this database")

    def charts_for(self, veh):
        q = ",".join("?" * len(veh.ids))
        codes = [r[0] for r in self.kb.db.execute(
            f"SELECT DISTINCT code FROM dtc_procedure WHERE vehicle_id IN ({q})", veh.ids)]
        return {c: C.load(self.kb, veh, c) for c in codes}

    def test_charts_load_on_many_cars(self):
        """Every code's chart loads (walkable or flagged) on a spread of models and years, without errors."""
        seen = 0
        for model in self.kb.models():
            years = self.kb.years(model)
            for year in {years[0], years[-1]}:
                for v in self.kb.vehicles(model, year):
                    for code, ch in list(self.charts_for(v).items())[:40]:
                        if ch and ch.ok:
                            self.assertTrue(ch.steps and ch.first() in ch.steps, f"{v.label} {code}")
                            self.assertEqual(C.check(ch.steps), [], f"{v.label} {code}")
                        seen += 1
        self.assertGreater(seen, 0)

    def test_walkable_charts_lead_somewhere(self):
        """In walkable charts, every goto points at a step that exists, and every step can end."""
        v = self.veh("Forester", 2002)
        ok = 0
        for code, ch in self.charts_for(v).items():
            if not (ch and ch.ok):
                continue
            ok += 1
            for st in ch.steps.values():
                for out in (st.yes, st.no):
                    if out.kind == "goto":
                        self.assertIn(out.step, ch.steps, f"{code} step {st.n}")
        self.assertGreaterEqual(ok, 60)

    def test_scan_steps(self):
        """'Any other DTC' steps answer from the codes: codes the chart itself covers don't count as others."""
        v = self.veh("Forester", 2002)
        tried = 0
        for code, ch in self.charts_for(v).items():
            if not (ch and ch.ok):
                continue
            st = ch.steps[ch.first()]
            if not C.re.search(r"any other DTC", st.question, C.re.I):
                continue
            covered = set(C.CODE_RE.findall(st.yes.text + " " + st.no.text))
            self.assertEqual(st.scan_answer([code], {code}), "No", code)
            if covered:
                self.assertEqual(st.scan_answer(sorted(covered), {code}), "No", code)
            self.assertEqual(st.scan_answer([code, "P1999"], {code}), "Yes", code)
            tried += 1
        self.assertGreater(tried, 5)

    def test_walk_records_how(self):
        v = self.veh("Forester", 2002)
        for code, ch in self.charts_for(v).items():
            if not (ch and ch.ok):
                continue
            w = C.Walk(ch, [code])
            for _ in range(30):
                st = w.step()
                if not st:
                    break
                if st.limit:
                    lim = st.limit
                    val = (lim.lo + (lim.hi or lim.lo)) / 2 if lim.op == "between" else lim.lo + (1 if ">" in lim.op else -1)
                    ans, _ = w.measure(str(val))
                    self.assertEqual(ans, "Yes", f"{code} step {st.n}: {val} vs {lim.describe()}")
                else:
                    w.answer("No")
            for a in w.trail:
                self.assertIn(a.how, ("scan", "measured", "mechanic"))
            n = len(w.trail)
            if n and any(a.how != "scan" for a in w.trail):
                self.assertTrue(w.undo())
                self.assertLess(len(w.trail), n)

    def test_reused_part_lookup(self):
        """A reused part leads to the manual lines that say to fit a new one, on more than one car."""
        from shopdeck.desk import Desk
        found = 0
        for model, year in (("Forester", 2002), ("Impreza", 2004), ("Legacy", 2000)):
            vehs = self.kb.vehicles(model, year)
            if not vehs:
                continue
            d = Desk({"kb_dir": "", "make": "", "labor_rate": 100, "tax_rate": 0, "supplies_rate": 0,
                      "out_dir": tempfile.mkdtemp()}, kb_path=KB_PATH)
            d.veh = vehs[0]
            hits = d.reuse_check("exhaust gasket")
            for sec, lines in hits:
                for line in lines:
                    self.assertRegex(line.lower(), r"gasket")
            found += bool(hits)
        self.assertGreater(found, 0)

    def test_torque_and_new_parts_from_text(self):
        v = self.veh("Forester", 2002)
        secs = [s for s in R.procedure_candidates(self.kb, v, "spark plug", limit=10)]
        self.assertTrue(secs)
        tq = [t for s in secs for part in self.kb.topic_sections(s.id) for t in R.torque_lines(part)]
        self.assertTrue(any("N·m" in val or "N m" in val for _, val in tq), tq[:3])

    def test_work_order(self):
        from shopdeck.desk import Desk
        out = tempfile.mkdtemp()
        d = Desk({"kb_dir": "", "make": "", "labor_rate": 100, "tax_rate": 0, "supplies_rate": 0, "out_dir": out},
                 kb_path=KB_PATH)
        v = self.kb.vehicles("Forester", 2002)[0]
        d.set_car(v)
        d.set_codes(["P0420", "p0420", "junk"])
        self.assertEqual(d.case.codes, ["P0420"])
        job = d.add_job(d.job_from_text("replace front exhaust pipe", "test"))
        for s in R.procedure_candidates(d.kb, v, job.title, limit=1):
            job.procedures.append(s.id)
        path = d.work_order()
        with open(path) as f:
            html = f.read()
        self.assertIn("Repair instructions", html)
        self.assertIn("p.", html)


if __name__ == "__main__":
    unittest.main()
