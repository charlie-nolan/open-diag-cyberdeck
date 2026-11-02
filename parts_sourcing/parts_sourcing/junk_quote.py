"""Junkyard price quote: the yard's posted price for the part + CHARM labor hours x your labor rate.

Pick-n-Pull prices by part type (every alternator costs the same), so the price list is short. Copy it
from their website and load it once:

    python3 -m parts_sourcing.junk_quote load prices.txt --chain "Pick-n-Pull"   # or - for stdin
    python3 -m parts_sourcing.junk_quote quote 14038AA000

Pick-n-Pull's own table pastes as tab-separated columns (Item #, Description, Type, Price, Core Deposit) and is
read as such. Free text works too: a name and a dollar amount, with a second amount marked "core" as the core
charge ("Alternator  $34.99  Core $10.00"). Lines that look like fees (admission, environmental) are kept as fees.
Pick-n-Pull's list has no admission line, so set "admission" in sourcing.json if you want it in the total.
Labor is CHARM's book time to replace the part on the car in sourcing.json (removal on the donor car
is usually quicker, so it's an upper bound).
"""
import argparse
import datetime as dt
import os
import re
import sqlite3
import sys

from . import settings
from . import yards as Y
from .interchange import normalize_pn, pn_from_raw

PRICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_list (
  chain TEXT NOT NULL, item TEXT NOT NULL, price REAL, core REAL, is_fee INTEGER NOT NULL DEFAULT 0,
  raw TEXT NOT NULL, loaded_at TEXT NOT NULL, item_no TEXT, vtype TEXT,
  PRIMARY KEY (chain, item)
) WITHOUT ROWID;
"""
PRICE_COLS = ("item_no", "vtype")
MONEY_RE = re.compile(r"\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)|\b(\d{1,4}(?:,\d{3})*\.\d{2})\b")
FEE_RE = re.compile(r"\b(admission|entry|entrance|environmental|fee)\b", re.I)   # 'feeder' isn't a fee
STOP = {"and", "the", "with", "assembly", "assy", "complete", "compl", "each", "only", "front", "rear", "left",
        "right", "replace", "remove", "install", "r&r", "w/", "for", "of", "or", "&", "all", "bulk", "type"}
KEEP2 = {"cv", "ac"}          # two-letter words that mean something (C.V. axle, A/C)
# Pick-n-Pull's abbreviations, spelled out so they meet CHARM's names
ABBREV = {"cyl": "cylinder", "trans": "transmission", "transaxle": "transmission", "exhst": "exhaust",
          "headlt": "headlight", "instr": "instrument", "mcpher": "strut", "thrtl": "throttle", "bdy": "body",
          "inj": "injection", "brk": "brake", "cbl": "cable", "contrl": "control", "shrd": "shroud",
          "susp": "suspension", "strng": "steering", "solend": "solenoid", "spdl": "spindle", "spindl": "spindle",
          "spin": "spindle", "pressr": "pressure", "reinfrcmt": "reinforcement", "brackt": "bracket",
          "brckt": "bracket", "elec": "electric", "distributr": "distributor", "automtc": "automatic",
          "regulater": "regulator", "rtr": "rotor", "multifunct": "multifunction", "punctrd": "punctured"}


def words(s):
    s = (s or "").lower()
    s = re.sub(r"\bconstant velocity( joint)?", "cv", s)                     # CHARM's name for a CV joint
    s = re.sub(r"\bw/o(?:ut)?\s+[^\s)/]+|\bwithout\s+\S+", " ", s)      # 'W/O SPIN' says what it lacks
    s = re.sub(r"\b(?:[a-z][./]){1,3}[a-z]\b\.?", lambda m: re.sub(r"[./]", "", m.group()), s)   # A/C, C.V., E.G.R.
    out = []
    for w in re.findall(r"[a-z0-9]{2,}", s):
        if len(w) < 3 and w not in KEEP2:
            continue
        w = ABBREV.get(w, w)
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in STOP:
            out.append(w)
    return out


# Words CHARM puts in a crumb to say which car or side, not what the part is: "(Manual Trans)", "Right", "(With ABS)"
QUALIFIERS = {"manual", "automatic", "auto", "trans", "speed", "with", "without", "w", "wo", "abs",
              "right", "left", "front", "rear", "one", "side", "both", "sides", "each", "upper", "lower", "inner",
              "outer", "turbo", "non", "sohc", "dohc", "sedan", "wagon", "awd", "2wd", "4wd", "mt", "at", "california",
              "federal", "canada", "model", "models", "except", "from", "to", "and", "or", "only", "type", "lh", "rh",
              "cvt", "4at", "5mt", "6mt", "replace", "remove", "install", "r", "l", "part", "parts", "ac", "a/c"}
# Price-list words that name a different system: an item carrying one the CHARM path doesn't mention is the wrong part
CONFLICT = {"heater", "transmission", "engine", "radiator", "brake", "abs", "fuel", "steering", "door", "seat",
            "window", "hood", "deck", "wiper", "headlight", "taillight", "idle", "exhaust", "intake", "truck",
            "diesel", "hybrid", "trailer", "spindle", "hub", "rotor", "solenoid", "clutch", "carburetor", "smog"}


# labor rows that aren't the replace job itself: tests, and "add" times stacked on another job
NOT_REPLACE = re.compile(r"\b(diagnos\w*|test\w*|inspect\w*|check\w*|adjust\w*|add|additional|overhaul)\b", re.I)


def is_qualifier(seg):
    seg = seg.strip()
    if seg.startswith("(") and seg.endswith(")"):
        return True
    if re.fullmatch(r"(front|rear) axle", seg.lower()):     # where it goes, not what it is ('Replace > Rear Axle')
        return True
    toks = re.findall(r"[a-z0-9&]+", seg.lower())
    return not toks or all(t in QUALIFIERS or t.isdigit() or len(t) == 1 or t == "&" for t in toks)


def split_part(op):
    """CHARM path -> (part name crumb, the crumbs above it), skipping qualifier crumbs like '(Manual Trans)'."""
    segs = [re.sub(r"\([^)]*\)", "", x).strip() if not is_qualifier(x) else x for x in (op or "").split(" > ")]
    real = [x for x in segs if x and not is_qualifier(x)]
    if not real:                                    # a job typed as just 'Rear Axle'
        last = next((x for x in reversed(segs) if re.fullmatch(r"(front|rear) axle", x.lower())), "")
        return last, []
    return real[-1], real[:-1]


def part_label(op):
    """'Starter Motor (Manual Trans)': the part's own crumb plus the qualifiers after it."""
    name, _ = split_part(op)
    segs = op.split(" > ")
    quals = [x.strip("() ") for x in segs[[x.strip() for x in segs].index(name) + 1:] if is_qualifier(x)] \
        if name in [x.strip() for x in segs] else []
    quals = [q for q in quals if q.lower() not in ("part", "parts")]
    return name + (f" ({', '.join(quals)})" if quals else "")


def parse_tsv_line(line):
    """Pick-n-Pull's table: Item #, Description, Type, Price, Core Deposit (tab-separated)."""
    f = [c.strip() for c in line.split("\t")]
    if len(f) < 4 or not re.search(r"[A-Za-z]", f[1]):
        return None
    pm = MONEY_RE.fullmatch(f[3]) or re.fullmatch(r"\$?\s*(\d+(?:\.\d{2})?)()", f[3])
    if not pm:
        return None                         # the header row, or a row with no price
    price = float((pm.group(1) or pm.group(2)).replace(",", ""))
    core = None
    if len(f) > 4 and f[4]:
        cm = re.search(r"(\d+(?:,\d{3})*(?:\.\d{2})?)", f[4])
        core = float(cm.group(1).replace(",", "")) if cm else None
    name = " ".join(f[1].split())
    return dict(item=name, price=price, core=core or None, item_no=f[0] or None, vtype=f[2] or None,
                is_fee=int(bool(FEE_RE.search(name))), raw=line[:300])


def parse_price_lines(text):
    out = []
    for line in text.splitlines():
        if "\t" in line.strip():
            r = parse_tsv_line(line.strip())
            if r:
                out.append(r)
            continue
        line = line.strip()
        if not line:
            continue
        amounts = list(MONEY_RE.finditer(line))
        if not amounts:
            continue
        name = line[:amounts[0].start()].strip(" .:-\t|$")
        if not re.search(r"[A-Za-z]", name):
            continue
        vals = [float((m.group(1) or m.group(2)).replace(",", "")) for m in amounts]
        core = None
        cm = re.search(r"core[^0-9$]*\$?\s*(\d+(?:\.\d{2})?)", line, re.I)
        if cm:
            core = float(cm.group(1))
        price = vals[0]
        out.append(dict(item=" ".join(name.split()), price=price, core=core, item_no=None, vtype=None,
                        is_fee=int(bool(FEE_RE.search(name))), raw=line[:300]))
    return out


def ensure_schema(con):
    con.executescript(PRICE_SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(price_list)")}
    for c in PRICE_COLS:                    # price lists loaded before these columns existed
        if c not in have:
            con.execute(f"ALTER TABLE price_list ADD COLUMN {c} TEXT")


def load(yards_db, chain, text):
    rows = parse_price_lines(text)
    con = Y.connect(yards_db, write=True)
    try:
        ensure_schema(con)
        con.execute("DELETE FROM price_list WHERE chain = ?", (chain,))
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        for r in rows:
            con.execute("INSERT OR REPLACE INTO price_list (chain, item, price, core, is_fee, raw, loaded_at, item_no, vtype) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (chain, r["item"], r["price"], r["core"], r["is_fee"], r["raw"], now, r["item_no"], r["vtype"]))
        con.commit()
    finally:
        con.close()
    return rows


def part_name(kb_db, pn):
    """CHARM's name path for the part ('Engine > Charging System > Alternator')."""
    con = sqlite3.connect(f"file:{os.path.expanduser(kb_db)}?mode=ro", uri=True)
    try:
        pn = normalize_pn(pn)
        r = con.execute("SELECT operation FROM labor WHERE replace(part_number,'-','') = ? LIMIT 1", (pn,)).fetchone()
        if not r:   # DBs whose all-digit numbers (GM ...) are only in the row text
            r = next(((op,) for op, raw in con.execute(
                "SELECT operation, raw FROM labor WHERE part_number IS NULL AND replace(raw,'-','') LIKE ?",
                (f"%{pn}%",)) if normalize_pn(pn_from_raw(raw)) == pn), None)
    finally:
        con.close()
    return r[0] if r else None


def match_price(yards_db, chain, op, aliases=None):
    """Best price-list item for a CHARM part path; None if nothing shares a word with the part's name.
    aliases ({"words in the CHARM name": "price list item"}, from sourcing.json price_aliases) win outright."""
    if not op or not os.path.exists(os.path.expanduser(yards_db)):
        return None
    name, above = split_part(op)
    name_w, ctx_w = set(words(name)), set(words(" ".join(above[-2:])))
    path_w = set(words(op))
    if not name_w:
        return None
    con = Y.connect(yards_db)
    try:
        items = con.execute("SELECT * FROM price_list WHERE chain = ? AND is_fee = 0", (chain,))
        cols = [d[0] for d in items.description]
        items = [dict(zip(cols, r)) for r in items.fetchall()]
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    tail = " ".join(op.split(" > ")[-2:]).lower()
    for key, want in (aliases or {}).items():
        if all(w in tail for w in key.lower().split()):
            hit = next((it for it in items if it["item"].lower() == want.lower()), None)
            if hit:
                return dict(item=hit["item"], price=hit["price"], core=hit["core"], item_no=hit.get("item_no"))
    best = None
    for it in items:
        iw = set(words(it["item"]))
        # the item must name the whole part ("Radiator Cap" isn't "RADIATOR"), and not another system
        # ("HEATER MOTOR AND FAN" isn't a radiator fan motor)
        if not iw or not name_w <= iw or (iw - path_w) & CONFLICT:
            continue
        score = (3 * len(iw & name_w) + len(iw & ctx_w) - 0.1 * len(iw - name_w - ctx_w)
                 - (2 if it.get("vtype") and it["vtype"].lower() != "car" else 0))   # Pick-Up/Van rows last
        key = (score, -len(it["item"]), -(it["price"] or 0))
        if best is None or key > best[0]:
            best = (key, it)
    if not best:
        return None
    it = best[1]
    return dict(item=it["item"], price=it["price"], core=it["core"], item_no=it.get("item_no"))


def price_lines(yards_db, chain, job_words):
    """Price list lines that carry every job word, for parts CHARM has no number for (front CV axle, headlight)."""
    if not os.path.exists(yards_db):
        return []
    con = Y.connect(yards_db)
    try:
        rows = con.execute("SELECT item, price, core FROM price_list WHERE chain = ? AND is_fee = 0", (chain,)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()
    return [r for r in rows if set(job_words) <= set(words(r[0]))]


def fees(yards_db, chain):
    try:
        con = Y.connect(yards_db)
        try:
            return con.execute("SELECT item, price FROM price_list WHERE chain = ? AND is_fee = 1", (chain,)).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return []


def labor_hours(kb_db, op, model, year):
    """CHARM book hours to replace this part on (model, year): labor rows whose path ends with the part's name."""
    if not op:
        return None
    name, above = split_part(op)
    need = words(name)
    ctx = words(" ".join(above[-2:]))
    if not need:
        return None
    r = _labor(kb_db, need, ctx, op, model, year)
    if r is None and len(need) > 1 and set(need) & KEEP2:          # 'cv axle' -> the 'Axle Shaft' rows
        r = _labor(kb_db, [w for w in need if w not in KEEP2], ctx, op, model, year)
    return r


def _labor(kb_db, need, ctx, op, model, year):
    side = "rear" if re.search(r"\brear\b", op, re.I) else "front"    # front unless the part says rear
    other = "front" if side == "rear" else "rear"
    con = sqlite3.connect(f"file:{os.path.expanduser(kb_db)}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT l.operation, l.hours FROM labor l WHERE l.hours > 0 AND "
            + " AND ".join("lower(l.operation) LIKE ?" for _ in need)
            + " AND l.section_id IN (SELECT sv.section_id FROM section_vehicle sv JOIN vehicle v ON v.id = sv.vehicle_id "
              "WHERE v.model = ? AND v.year = ?) LIMIT 300",
            [f"%{w}%" for w in need] + [model, year]).fetchall()
    finally:
        con.close()

    def name_at(op_l):
        """Index of the crumb (in the last 3) that holds the whole part name, or None."""
        segs = op_l.split(" > ")
        for i in range(len(segs) - 1, max(len(segs) - 4, -1), -1):
            if not is_qualifier(segs[i]) and set(need) <= set(words(segs[i])):
                return i
        return None

    def score(r):
        segs = r[0].lower().split(" > ")
        i = name_at(r[0].lower())
        tail = " ".join(segs[-3:])
        extra = set(words(segs[i])) - set(need)                 # 'Fuel Pump Control Unit' isn't 'Fuel Pump'
        after = [w for x in segs[i + 1:] if not is_qualifier(x) for w in words(x)]   # 'Alternator > Voltage Regulator'
        return (sum(w in tail for w in ctx) + 2 * ("replace" in tail or "r&r" in tail)
                + (side in tail) - (other in tail) + 0.5 * ("one side" in tail) - 0.5 * ("both" in tail)
                - 1.5 * len(extra) - len(after) - 0.001 * len(r[0]))
    def extra_words(r):
        """Most extra words in any of the last 3 crumbs that hold the part name
        ('Fuel Pump Control Unit > Replace > Fuel Pump Controller' counts the Control Unit)."""
        return max((len(set(words(x)) - set(need)) for x in r[0].lower().split(" > ")[-3:]
                    if not is_qualifier(x) and set(need) <= set(words(x))), default=0)
    rows = [r for r in rows if name_at(r[0].lower()) is not None      # 'axle' and 'shaft' in different crumbs don't count
            and extra_words(r) <= 1                                    # 'Starter Motor' yes, 'Fuel Pump Control Unit' no
            and not NOT_REPLACE.search(" ".join(r[0].split(" > ")[-3:]))]
    if not rows:
        return None
    op_l, hours = max(rows, key=score)
    return dict(operation=op_l, hours=hours)


def quote(part_number, cfg=None):
    cfg = cfg or settings()
    kb_db, yards_db = os.path.expanduser(cfg["kb_db"]), os.path.expanduser(cfg["yards_db"])
    chain = cfg.get("price_chain", "Pick-n-Pull")
    rate = float(cfg.get("labor_rate", 100))
    tax = float(cfg.get("sales_tax", 0))
    car = cfg.get("car", {})
    pn = normalize_pn(part_number)
    op = part_name(kb_db, pn)
    price = match_price(yards_db, chain, op, cfg.get("price_aliases"))
    lab = labor_hours(kb_db, op, car.get("model"), car.get("year"))
    fee_rows = fees(yards_db, chain) if cfg.get("include_admission", True) else []
    lines = [f"Junkyard quote for {pn} ({part_label(op) if op else 'part not in CHARM'})"]
    total = 0.0
    if price:
        part_cost = price["price"] * (1 + tax)
        total += part_cost
        lines.append(f"  {chain} price list '{price['item']}'"
                     + (f" (item {price['item_no']})" if price.get("item_no") else "") + f": ${price['price']:.2f}"
                     + (f" + {tax * 100:.1f}% tax" if tax else ""))
        if price["core"]:
            lines.append(f"  core charge ${price['core']:.2f} (refunded when you bring the old part back)")
    else:
        lines.append(f"  not on the {chain} price list loaded (ask at the counter)")
    fee_rows = [(n, a) for n, a in fee_rows if a and re.search(r"admission|entry|entrance", n, re.I)]
    if cfg.get("include_admission", True) and not fee_rows and cfg.get("admission"):
        fee_rows = [("Admission (sourcing.json)", float(cfg["admission"]))]
    for name, amount in fee_rows:
        total += amount
        lines.append(f"  {name}: ${amount:.2f}")
    if lab:
        total += lab["hours"] * rate
        lines.append(f"  labor {lab['hours']:g} h x ${rate:.0f}/h = ${lab['hours'] * rate:.2f} "
                     f"(CHARM: {lab['operation']}; book time to replace, pulling is usually quicker)")
    else:
        lines.append("  no CHARM labor time found for this part on this car")
    if price:
        lines.append(f"  TOTAL ${total:.2f}" + (" (core not included)" if price["core"] else ""))
    else:
        lines.append(f"  TOTAL unknown: part price not found" + (f", labor alone ${total:.2f}" if lab else ""))
    return dict(part_number=pn, part=op, price=price, labor=lab, total=round(total, 2) if price else None,
                text="\n".join(lines))


def quote_job(job, cfg):
    """Quote by job words alone ('cv axle'): best price-list line + CHARM labor for that name on the car.
    For parts CHARM's parts pages skip, like the Forester's front CV axles."""
    kb_db, yards_db = os.path.expanduser(cfg["kb_db"]), os.path.expanduser(cfg["yards_db"])
    chain = cfg.get("price_chain", "Pick-n-Pull")
    rate = float(cfg.get("labor_rate", 100))
    car = cfg.get("car", {})
    op = " ".join(job.split()).title()
    price = match_price(yards_db, chain, op, cfg.get("price_aliases"))
    lab = labor_hours(kb_db, op, car.get("model"), car.get("year"))
    lines = [f"Quote by name '{job}' (no part number)"]
    total = 0.0
    if price:
        total += price["price"]
        lines.append(f"  {chain} '{price['item']}': ${price['price']:.2f}"
                     + (f" + ${price['core']:.2f} core" if price["core"] else ""))
    if lab:
        total += lab["hours"] * rate
        lines.append(f"  labor {lab['hours']:g} h x ${rate:.0f}/h = ${lab['hours'] * rate:.2f} (CHARM: {lab['operation']})")
    else:
        lines.append("  no CHARM labor time found under that name")
    lines.append(f"  TOTAL ${total:.2f}" if price else "  TOTAL unknown: no price list line")
    return "\n".join(lines) if (price or lab) else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("load", help="load a price list copied from the yard's website")
    lp.add_argument("file", help="text file, or - for stdin")
    lp.add_argument("--chain", default=None)
    lp.add_argument("--dry-run", action="store_true")
    qp = sub.add_parser("quote", help="price + labor for part numbers")
    qp.add_argument("parts", nargs="*")
    qp.add_argument("--job", help='words for the job instead of part numbers, e.g. "alternator"')
    a = ap.parse_args(argv)
    cfg = settings()
    if a.cmd == "load":
        text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8", errors="replace").read()
        chain = a.chain or cfg.get("price_chain", "Pick-n-Pull")
        rows = parse_price_lines(text) if a.dry_run else load(cfg["yards_db"], chain, text)
        for r in rows:
            print(f"  {'fee ' if r['is_fee'] else ''}{r['item']}: ${r['price']:.2f}"
                  + (f" core ${r['core']:.2f}" if r["core"] else ""))
        print(f"{len(rows)} lines {'read' if a.dry_run else 'loaded for ' + chain}")
        return 0
    parts = list(a.parts)
    if a.job:
        from .interchange import parts_for_job
        car = cfg.get("car", {})
        found = parts_for_job(os.path.expanduser(cfg["kb_db"]), a.job, car.get("model"), car.get("year"), limit=40)
        jw = set(words(a.job))
        kb = os.path.expanduser(cfg["kb_db"])
        own = [f for f in found                     # the job names the part, not its area or its labor path
               if jw <= set(words(split_part(f[1])[0])) and jw <= set(words(split_part(part_name(kb, f[0]) or f[1])[0]))]
        on_list = price_lines(os.path.expanduser(cfg["yards_db"]), cfg.get("price_chain", "Pick-n-Pull"), jw)
        if on_list:
            print(f"{cfg.get('price_chain', 'Pick-n-Pull')} price list lines for '{a.job}':")
            for it, pr, core in on_list[:8]:
                print(f"  {it}: ${pr:.2f}" + (f" + ${core:.2f} core" if core else ""))
        jq = quote_job(a.job, cfg)
        if jq:
            print(jq)
        if not own:
            print(f"No CHARM part numbers for '{a.job}' on the {car.get('year')} {car.get('model')}. "
                  "Check the yard's price list by hand.")
            return 1
        parts += [pn for pn, _op, _price in own[:3]]
    if not parts:
        ap.error("give part numbers or --job")
    for p in parts:
        print(quote(p, cfg)["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
