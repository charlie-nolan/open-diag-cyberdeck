"""Offline tests with a synthetic mini kb (same tables as kb/subaru.db) and synthetic Row52 HTML.
The fitment rows below are made up for the test, not real CHARM data.

    cd /path/to/parts_sourcing && python3 -m unittest discover -s tests -v
"""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parts_sourcing import alert_mail, interchange, junk_quote, qr, sourcing, yard_paste, yard_sync, yards  # noqa: E402
from parts_sourcing.sheet import render  # noqa: E402

KB_SCHEMA = """
CREATE TABLE vehicle (id INTEGER PRIMARY KEY, model TEXT NOT NULL, chassis TEXT, year INTEGER NOT NULL, engine TEXT,
  transmission TEXT, market TEXT NOT NULL DEFAULT 'USDM');
CREATE TABLE section (id INTEGER PRIMARY KEY, doc_id INTEGER, path TEXT, body TEXT);
CREATE TABLE section_vehicle (vehicle_id INTEGER, section_id INTEGER, PRIMARY KEY (vehicle_id, section_id));
CREATE TABLE labor (id INTEGER PRIMARY KEY, section_id INTEGER NOT NULL, operation TEXT NOT NULL, hours REAL,
  part_number TEXT, price TEXT, raw TEXT NOT NULL);
"""
VEHICLES = [  # (model, year, engine)
    ("Forester", 2002, "F4-2.5L SOHC"), ("Forester", 2004, "F4-2.5L SOHC"), ("Forester", 2004, "F4-2.5L DOHC Turbo"),
    ("Impreza", 2002, "F4-2.5L SOHC"), ("Impreza", 2002, "F4-2.0L DOHC Turbo"), ("Impreza", 2005, "F4-2.5L SOHC"),
    ("Outback", 1998, "F4-2.5L DOHC"), ("Legacy", 2001, "F4-2.5L SOHC"),
]
GASKET = [("Forester", 2002), ("Forester", 2004, "F4-2.5L SOHC"), ("Impreza", 2002, "F4-2.5L SOHC"),
          ("Impreza", 2005, "F4-2.5L SOHC"), ("Legacy", 2001)]


def make_kb(path):
    con = sqlite3.connect(path)
    con.executescript(KB_SCHEMA)
    vid = {}
    for m, y, e in VEHICLES:
        vid[(m, y, e)] = con.execute("INSERT INTO vehicle(model,year,engine) VALUES (?,?,?)", (m, y, e)).lastrowid
    sid = 0
    for (m, y, e), v in vid.items():
        sid += 1
        con.execute("INSERT INTO section VALUES (?,1,?,?)", (sid, f"Exhaust > Parts {m} {y} {e}", "x"))
        con.execute("INSERT INTO section_vehicle VALUES (?,?)", (v, sid))
        if any(g[0] == m and g[1] == y and (len(g) == 2 or g[2] == e) for g in GASKET):
            num = "14038AA000" if m != "Legacy" else "14038AA001"   # Legacy lists a sibling revision
            con.execute("INSERT INTO labor(section_id,operation,part_number,price,raw) VALUES (?,?,?,?,?)",
                        (sid, "Exhaust > Exhaust Manifold > Gasket", num, "9.80", "x"))
        if (m, y) == ("Forester", 2002):        # alternator part + book time
            con.execute("INSERT INTO labor(section_id,operation,part_number,price,raw) VALUES (?,?,?,?,?)",
                        (sid, "Engine > Charging System > Alternator", "23700AA62A", "389.00", "x"))
            con.execute("INSERT INTO labor(section_id,operation,hours,raw) VALUES (?,?,?,?)",
                        (sid, "Engine > Charging System > Alternator > Replace", 1.2, "x"))
            con.execute("INSERT INTO labor(section_id,operation,hours,raw) VALUES (?,?,?,?)",
                        (sid, "Engine > Charging System > Alternator > Voltage Regulator > Replace", 1.6, "x"))
        if (m, y) == ("Forester", 2002):        # labor rows that must not win: tests, add-ons, a bigger component
            for op_, h in [("Fuel System > Fuel Pump Control Unit > Replace > Fuel Pump Controller", 1.4),
                           ("Fuel System > Fuel Pump Control Unit > Replace > Fuel Pump Controller", 1.4),
                           ("Fuel System > Fuel Pump > Replace", 0.9),
                           ("Fuel System > Fuel Pump Relay Control Unit > Replace", 0.5),
                           ("Starting System > Diagnose/Test > Starter Draw, Test", 0.3),
                           ("Starting System > Starter Motor > Replace", 1.1),
                           ("Drive Axles > Axle Shaft Assembly > Axle Shaft > Front Axle > One Side", 1.0),
                           ("Drive Axles > Axle Shaft Assembly > Axle Shaft > Front Axle > Both Sides", 1.7),
                           ("Drive Axles > Axle Shaft Assembly > Axle Shaft > Rear Axle > One Side", 0.8),
                           ("Drive Axles > Axle Shaft Assembly > Axle Shaft > To R&R Inner Joint and/or Boot, Add > Each", 0.5),
                           ("Drive Axles > Constant Velocity Joint Boot > One Side > One", 1.5),
                           ("Differential > Differential Assembly > Replace > Rear Axle", 1.8),
                           ("Differential > Differential Assembly > Replace > Front Axle", 4.9),
                           ("Transaxle > Transaxle Assembly > Replace", 6.0),
                           ("Starting System > Starter Motor > To R&R Starter Motor Seals, Add > Each", 0.2)]:
                con.execute("INSERT INTO labor(section_id,operation,hours,raw) VALUES (?,?,?,?)", (sid, op_, h, "x"))
        if (m, y) == ("Forester", 2002):        # intake gasket filed under the same chapter
            con.execute("INSERT INTO labor(section_id,operation,part_number,price,raw) VALUES (?,?,?,?,?)",
                        (sid, "Engine, Cooling and Exhaust > Intake Manifold > Gasket", "14035AA383", "5.92", "x"))
        if (m, e) == ("Impreza", "F4-2.0L DOHC Turbo"):
            con.execute("INSERT INTO labor(section_id,operation,part_number,price,raw) VALUES (?,?,?,?,?)",
                        (sid, "Exhaust > Exhaust Manifold > Gasket", "14038AA050", "12.00", "x"))
    con.commit()
    con.close()


def card(vin, year, mm, yard, loc, city, row, added):
    return (f'<div class="vehicle"><a href="/Vehicle/Index/{vin}"><img src="x.jpg"></a>'
            f'<a href="/Vehicle/Index/{vin}"><strong>{year} {mm}</strong></a>'
            f'<a href="/Yards/Detail/{loc}">{yard}</a><span>{city}, Washington 98000</span>'
            f'<div>VIN: {vin}</div><div>Row: <span>{row}</span></div><div>Added to yard: {added}</div></div>')


PAGE = "<html><body>" + "".join([
    card("JF1SF63551H700001", 2002, "Subaru Forester", "PICK-n-PULL Lynnwood", 54, "Lynnwood", "12", "Oct 10, 2026"),
    card("JF1SG63614H700002", 2004, "Subaru Forester", "PICK-n-PULL Lynnwood", 54, "Lynnwood", "13", "Oct 11, 2026"),
    card("JF1SG69684H700003", 2004, "Subaru Forester", "PICK-n-PULL Tacoma", 61, "Lakewood", "4", "Oct 1, 2026"),
    card("JF1GD29662G700004", 2002, "Subaru Impreza", "PICK-n-PULL Tacoma", 61, "Lakewood", "5", "Sep 30, 2026"),
    card("JF1GD70652L700005", 2002, "Subaru Impreza", "PICK-n-PULL Arlington", 70, "Arlington", "8", "Oct 2, 2026"),
    card("JF1GG67575H700006", 2005, "Saab 9-2X", "PICK-n-PULL Arlington", 70, "Arlington", "9", "Oct 3, 2026"),
    card("4S3BH675017700007", 2001, "Subaru Legacy", "PICK-n-PULL Tumwater", 80, "Tumwater", "2", "Oct 4, 2026"),
    card("2HNYD2H64DH000042", 2013, "Acura MDX", "PICK-n-PULL Lynnwood", 54, "Lynnwood", "32", "Oct 15, 2026"),
    card("SALAG2D46CA000043", 2012, "Land Rover LR4", "PICK-n-PULL Lynnwood", 54, "Lynnwood", "33", "Oct 15, 2026"),
]) + "<div>1 of 1</div></body></html>"

VPIC = {  # made-up decodes in vPIC's field names
    "JF1SG63614H700002": {"DisplacementL": "2.5", "EngineCylinders": "4", "ValveTrainDesign": "Single Overhead Cam (SOHC)", "Turbo": ""},
    "JF1SG69684H700003": {"DisplacementL": "2.5", "EngineCylinders": "4", "ValveTrainDesign": "Dual Overhead Cam (DOHC)", "Turbo": "Yes"},
    "JF1GD70652L700005": {"DisplacementL": "2.0", "EngineCylinders": "4", "ValveTrainDesign": "", "Turbo": "Yes"},
    "JF1GG67575H700006": {"DisplacementL": "2.5", "EngineCylinders": "4", "ValveTrainDesign": "", "Turbo": ""},
}


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.kb = os.path.join(cls.tmp, "subaru.db")
        cls.ydb = os.path.join(cls.tmp, "yards.db")
        make_kb(cls.kb)
        rows = yard_sync.parse_row52(PAGE)
        con = yards.connect(cls.ydb, write=True)
        run = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        yard_sync.store(con, rows, run)
        yard_sync.store_decodes(con, {k: dict(v, VIN=k) for k, v in VPIC.items()})
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,NULL)", (run, "row52", 1, len(rows), 7, 0))
        con.commit()
        con.close()
        cls.cfg = {"kb_db": cls.kb, "yards_db": cls.ydb, "home_zip": "98101"}

    def test_parse(self):
        rows = yard_sync.parse_row52(PAGE)
        self.assertEqual(len(rows), 9)
        r = rows[0]
        self.assertEqual((r["year"], r["make"], r["model"], r["yard"], r["location_id"], r["row"], r["added"]),
                         (2002, "Subaru", "Forester", "PICK-n-PULL Lynnwood", 54, "12", "2026-10-10"))
        self.assertEqual((rows[-1]["make"], rows[-1]["model"]), ("Land Rover", "LR4"))

    def test_engine_parse(self):
        e = interchange.Engine.from_charm("F4-2.0L DOHC Turbo")
        self.assertEqual((e.cyl, e.disp, e.cam, e.turbo), (4, 2.0, "DOHC", True))
        self.assertFalse(e.compatible(interchange.Engine.from_charm("F4-2.5L SOHC")))

    def test_interchange(self):
        ic = interchange.lookup(self.kb, "14038-AA000")
        self.assertEqual(ic.part_number, "14038AA000")
        self.assertIn("Forester 2.5L SOHC 2002, 2004", ic.summary())
        self.assertEqual({f.match for f in ic.fits if f.model == "Legacy"}, {"family"})
        self.assertNotIn("14038AA050", ic.numbers)    # different family (AA05x)

    def test_yard_counts(self):
        ic = interchange.lookup(self.kb, "14038AA000")
        rep = yards.report(self.ydb, self.kb, ic)
        by = {y.yard: y for y in rep.yards}
        lyn = by["PICK-n-PULL Lynnwood"]
        # 2002 Forester: one engine that year -> likely; 2004 decoded SOHC -> likely
        self.assertEqual(lyn.count("likely"), 2)
        tac = by["PICK-n-PULL Tacoma"]
        # 2004 turbo Forester excluded; 2002 Impreza undecoded and two engines that year -> possible
        self.assertEqual((tac.count("likely"), tac.count("possible")), (0, 1))
        arl = by["PICK-n-PULL Arlington"]
        # WRX (2.0 turbo) excluded; Saab 9-2X 2.5 via alias -> possible
        self.assertEqual([(c.model, c.level) for c in arl.cars], [("9-2X", "possible")])
        self.assertEqual(by["PICK-n-PULL Tumwater"].count("family"), 1)
        self.assertEqual(rep.yards[0].yard, "PICK-n-PULL Lynnwood")
        self.assertIn("2 likely", yards.format_report(rep))

    def test_links_and_sheet(self):
        links = qr.links_for("14038AA000")
        self.assertTrue(any("rockauto.com" in ln.url and "14038AA000" in ln.url for ln in links))
        s = sourcing("14038AA000", cfg=self.cfg)
        self.assertIn("likely", s["text"])
        page = render([s], "2002 Subaru Forester")
        if qr.available():
            self.assertGreaterEqual(page.count("<svg"), 4)
            self.assertIn("PICK-n-PULL Lynnwood: 2004 Forester, row 13", page)   # yard QR for the best car

    def test_parts_for_job(self):
        rows = interchange.parts_for_job(self.kb, "exhaust manifold gasket", "Forester", 2002)
        self.assertEqual([r[0] for r in rows], ["14038AA000"])      # not the intake gasket
        rows = interchange.parts_for_job(self.kb, "intake manifold gasket", "Forester", 2002)
        self.assertEqual([r[0] for r in rows], ["14035AA383"])

    def test_paste(self):
        text = """2002 Subaru Forester
PICK-n-PULL Lynnwood
Lynnwood, Washington 98037
VIN: JF1SF63551H700001
Row: 12
Added to yard: Oct 10, 2026

2005 Subaru Outback
PICK-n-PULL Lynnwood
VIN: 4S4BP61C057300009  Row: 7  Added to yard: Oct 12, 2026
2003 Subaru Forester row 21
"""
        cars = yard_paste.parse_text(text, "PICK-n-PULL Lynnwood")
        self.assertEqual([(c["year"], c["model"], c["row"]) for c in cars],
                         [(2002, "Forester", "12"), (2005, "Outback", "7"), (2003, "Forester", "21")])
        self.assertEqual(cars[1]["vin"], "4S4BP61C057300009")
        self.assertEqual(cars[1]["added"], "2026-10-12")
        self.assertTrue(cars[2]["vin"].startswith("MANUAL-"))

    def test_full_paste_drops_missing(self):
        db = os.path.join(self.tmp, "yards_full.db")
        con = yards.connect(db, write=True)
        t1, t2 = "2026-10-15T00:00:00+00:00", "2026-10-16T00:00:00+00:00"
        first = yard_paste.parse_text("2002 Subaru Forester row 1\n2004 Subaru Forester row 2\n", "Y1")
        yard_sync.store(con, first, t1, full_yards={"Y1"})
        yard_sync.store(con, first[:1], t2, full_yards={"Y1"})          # row 2 car was crushed
        con.execute("INSERT INTO sync_log VALUES (?,?,?,?,?,?,NULL)", (t2, "paste-full", None, 1, 1, 1))
        con.commit()
        con.close()
        ic = interchange.lookup(self.kb, "14038AA000")
        rep = yards.report(db, self.kb, ic)
        self.assertEqual([c.year for y in rep.yards for c in y.cars], [2002])

    def test_junk_quote(self):
        text = """Admission $2.00
Alternator $34.99 Core $10.00
Exhaust Manifold $29.99
Alternator Bracket $9.99
"""
        rows = junk_quote.load(self.ydb, "Pick-n-Pull", text)
        self.assertEqual([r["is_fee"] for r in rows], [1, 0, 0, 0])
        cfg = dict(self.cfg, car={"model": "Forester", "year": 2002}, labor_rate=100)
        q = junk_quote.quote("23700AA62A", cfg)
        self.assertEqual(q["price"]["item"], "Alternator")
        self.assertEqual(q["labor"]["hours"], 1.2)
        self.assertEqual(q["total"], 34.99 + 2.00 + 120.0)
        g = junk_quote.quote("14038AA000", cfg)      # a gasket isn't the manifold
        self.assertIsNone(g["price"])

    def test_junk_quote_pick_n_pull_table(self):
        tsv = ("Item #\tDescription\tType\tPrice\tCore Deposit\n"
               "601\tALTERNATOR\tCar\t$32.99\t$9.99\n"
               "605\tSTARTER SOLENOID\tCar\t$11.99\t$0.99\n"
               "490-1\tAXLE C.V.\tCar\t$41.99\t$1.99\n"
               "447-2\tAXLE SHAFT - 4x4\tPick-Up/Van\t$61.99\t$3.99\n"
               "495-1\tSTRUT MCPHER ASSY W/O SPIN\tCar\t$43.99\t$3.99\n"
               "495-2\tSTRUT MCPHER ASSY W/SPINDL\tCar\t$52.99\t$2.99\n"
               "327-1\tMANIFOLD EXHAUST\tCar\t$64.99\t$3.99\n"
               "541-1\tBRAKE MASTER CYL OR BOOSTER\tCar\t$33.99\t$3.99\n"
               "560-4B\t(BULK) WHEEL ALUMINUM\tCar\t$34.99\t$0.00\n"
               "422\tPULSE AIR FEEDER(CHRYS TYP)\tCar\t$19.99\t$0.00\n")
        rows = junk_quote.parse_price_lines(tsv)
        self.assertEqual(len(rows), 10)                       # header skipped
        alt = rows[0]
        self.assertEqual((alt["item"], alt["price"], alt["core"], alt["item_no"], alt["vtype"]),
                         ("ALTERNATOR", 32.99, 9.99, "601", "Car"))
        self.assertIsNone(rows[8]["core"])                    # $0.00 core = none
        self.assertFalse(any(r["is_fee"] for r in rows))      # FEEDER isn't a fee
        junk_quote.load(self.ydb, "PnP test", tsv)

        def m(op):
            r = junk_quote.match_price(self.ydb, "PnP test", op)
            return r and r["item"]
        self.assertEqual(m("Engine > Charging System > Alternator"), "ALTERNATOR")
        self.assertEqual(m("Drive Axles > Front > CV Axle"), "AXLE C.V.")
        self.assertEqual(m("Suspension > Front > Strut"), "STRUT MCPHER ASSY W/O SPIN")
        self.assertEqual(m("Exhaust > Exhaust Manifold"), "MANIFOLD EXHAUST")
        self.assertIsNone(m("Exhaust > Exhaust Manifold > Gasket"))
        self.assertEqual(m("Brakes > Master Cylinder"), "BRAKE MASTER CYL OR BOOSTER")
        # CHARM crumbs that only say which car ("(Manual Trans)", "Right") aren't the part name
        self.assertEqual(m("Starting System > Starter > (Manual Trans)"), None)   # no STARTER line in this list
        self.assertEqual(m("Brakes > Master Cylinder > (With ABS)"), "BRAKE MASTER CYL OR BOOSTER")
        self.assertEqual(m("Exhaust > Exhaust Manifold > Right"), "MANIFOLD EXHAUST")
        self.assertIsNone(m("Front Suspension > Strut Mount > Left"))          # a mount isn't the strut
        self.assertIsNone(m("Cooling > Radiator > Radiator Cap"))
        self.assertEqual(junk_quote.split_part("Engine > Starter Assembly (Manual Trans) > 5 Speed"),
                         ("Starter Assembly", ["Engine"]))
        self.assertEqual(junk_quote.match_price(self.ydb, "PnP test", "Brakes > Front > Axle Shaft",
                                                {"axle shaft": "AXLE C.V."})["item"], "AXLE C.V.")
        cfg = dict(self.cfg, car={"model": "Forester", "year": 2002}, labor_rate=100, price_chain="PnP test",
                   admission=2.0)
        q = junk_quote.quote("23700AA62A", cfg)
        self.assertEqual(q["total"], 32.99 + 2.0 + 120.0)     # admission from sourcing.json, core apart
        self.assertIn("(item 601)", q["text"])
        g = junk_quote.quote("14038AA000", cfg)               # gasket: not on the list, labor unknown
        self.assertIsNone(g["total"])
        self.assertIn("TOTAL unknown", g["text"])

    def test_labor_picks_the_replace_job(self):
        def h(op):
            r = junk_quote.labor_hours(self.kb, op, "Forester", 2002)
            return r and r["hours"]
        self.assertEqual(h("Part > Fuel Pump"), 0.9)
        self.assertEqual(h("Part > Starter > (Manual Trans)"), 1.1)
        self.assertIsNone(h("Part > Fuel Pump Relay"))
        self.assertEqual(h("Cv Axle"), 1.0)                 # front, one side
        self.assertEqual(h("Axle"), 1.0)                    # not the differential's 'Rear Axle' crumb
        self.assertEqual(h("Drive Axles > Axle Shaft > Rear"), 0.8)
        self.assertEqual(h("Cv Boot"), 1.5)
        self.assertEqual(h("Rear Axle"), 0.8)
        self.assertEqual(junk_quote.part_label("Fuel Pump > Part"), "Fuel Pump")      # only the Control Unit row: too many extra words
        self.assertEqual(junk_quote.split_part("Radiator > Part > (Without AC)")[0], "Radiator")

    def test_alert_mail(self):
        body = ("Cno3117,\nThere are new vehicles matching your saved search Subie parts at Row52.\n\n"
                "2001 Subaru Forester\nPICK-n-PULL Lynnwood\nLynnwood, Washington Row: 17\nAdded: Oct 06, 2026\n\n"
                "To modify this Alert, or to stop receiving these emails, login to My Account\n\n"
                "Need a part from one of the vehicles? Post a Parts Wanted listing at: row52.com/partswanted\n")
        raw = ("Message-ID: <alert-1@row52.com>\nFrom: alerts@row52.com\nSubject: New vehicles\n"
               "Content-Type: text/html; charset=utf-8\n\n"
               + "".join(f"<p>{ln}</p>" for ln in body.splitlines())).encode()
        db = os.path.join(self.tmp, "alerts.db")
        con = yards.connect(db, write=True)
        con.execute(alert_mail.SEEN_SCHEMA)
        mid, cars, new = alert_mail.handle(con, raw, "2026-10-17T00:00:00+00:00")
        self.assertEqual((mid, new, len(cars)), ("<alert-1@row52.com>", True, 1))
        c = cars[0]
        self.assertEqual((c["year"], c["model"], c["yard"], c["row"], c["added"]),
                         (2001, "Forester", "PICK-n-PULL Lynnwood", "17", "2026-10-06"))
        self.assertFalse(alert_mail.handle(con, raw, "2026-10-17T01:00:00+00:00")[2])   # used once
        self.assertEqual(con.execute("SELECT count(*) FROM yard_vehicle").fetchone()[0], 1)
        con.close()

    def test_engine_codes_and_b9(self):
        e = interchange.Engine.from_charm("EJ251")
        self.assertEqual((e.disp, e.cam, e.turbo), (2.5, "SOHC", False))
        self.assertEqual(interchange.norm_model("B9"), interchange.norm_model("B9 Tribeca"))


if __name__ == "__main__":
    unittest.main()
