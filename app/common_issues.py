"""
/common: what a model is known for, worked out in Python from the make's database before any model writes a
word. Counts come from NHTSA owner complaints (with the mileage the owner gave), recalls, TSBs and forum fixes
already in kb/<make>.db. The model only puts these numbers into words; it never adds a problem of its own.
"""
import re
import sqlite3
import statistics

BANDS = [(0, 50000, "under 50k"), (50000, 100000, "50-100k"), (100000, 150000, "100-150k"),
         (150000, 10 ** 7, "150k+")]


# Which part of the car a forum thread is about, from the words in its title (vocabulary only: says nothing
# about causes). A title can count for more than one system.
SYSTEMS = [
    ("engine (head gaskets, timing, internals)", r"head gasket|\bhg\b|timing (belt|chain)|piston|rod knock|"
     r"compression|valve|\bcam\b|oil (leak|consumption|burn)|knock|tick|\brings?\b|block|\bheads?\b"),
    ("cooling", r"overheat|coolant|radiator|thermostat|water pump|heater core|fan\b"),
    ("ignition / misfire", r"misfire|spark|coil|ignition|plug|p030\d"),
    ("fuel / air / emissions", r"fuel|injector|idle|stall|vacuum|maf|o2|oxygen|a/?f sensor|catalytic|\bcat\b|egr|evap|"
     r"purge|p0420|p017\d|p01[0-9]\d|lean|rich"),
    ("transmission / drivetrain", r"transmission|trans\b|clutch|shift|torque converter|cv |axle|differential|diff\b|"
     r"awd|transfer|wheel bearing|drive ?shaft|limp|lock ?-?up"),
    ("brakes / ABS", r"brake|abs\b|caliper|rotor"),
    ("suspension / steering", r"strut|shock|suspension|steering|tie rod|control arm|sway|alignment|clunk"),
    ("electrical / starting / charging", r"battery|alternator|starter|electrical|wiring|ground|fuse|relay|ecu|"
     r"won.?t start|no start|crank"),
    ("exhaust", r"exhaust|muffler|manifold|heat shield|rattle"),
    ("climate / body / other", r"a/?c\b|air condition|blower|door|window|\block(?! ?-?up)|seat|sunroof|leak in|wiper"),
]


def _systems(titles):
    out = {}
    for t in titles:
        low = (t or "").lower()
        for name, rx in SYSTEMS:
            if re.search(rx, low):
                out.setdefault(name, []).append(t)
    return out


def _cut(text, n):
    """At most n characters, ending on a whole word."""
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "..."


def _scope(model, year, window, aliases):
    names = aliases.get(model.upper(), [model.upper()])
    where = [f"upper(k.model) IN ({','.join('?' * len(names))})"]
    args = list(names)
    if year:
        where.append("coalesce(k.year_min, 0) <= ? AND coalesce(k.year_max, 9999) >= ?")
        args += [year + window, year - window]
    return " AND ".join(where), args


def _component(title):
    """NHTSA's 'ENGINE AND ENGINE COOLING:ENGINE' -> 'engine and engine cooling: engine' (readable, same group)."""
    return re.sub(r"\s*:\s*", ": ", (title or "unknown").strip()).lower()


def gather(db_path, model, year=None, miles=None, aliases=None, top=5):
    """Facts for one car. Returns dict(scope=..., lines=[...]) or dict(error=...)."""
    aliases = aliases or {}
    if not model:
        return {"error": "Pick the car's model (and year) on Intake first."}
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"error": f"Can't open the manual database ({e})."}
    has_miles = db.execute("SELECT 1 FROM sqlite_master WHERE name='nhtsa_miles'").fetchone() is not None
    window = 0
    for window in (0, 1, 2):   # widen to neighbouring years only when one year has too few reports
        where, args = _scope(model, year, window, aliases)
        n = db.execute(f"SELECT count(*) FROM knowledge k WHERE kind='nhtsa_complaint' AND {where}", args).fetchone()[0]
        if n >= 25 or not year:
            break
    scope = f"{model} {year - window}-{year + window}" if year and window else f"{model} {year or '(all years)'}"
    lines = []
    sel = "k.title, k.ref" + (", m.miles" if has_miles else ", NULL")
    join = " LEFT JOIN nhtsa_miles m ON m.ref = k.ref" if has_miles else ""
    rows = db.execute(f"SELECT {sel} FROM knowledge k{join} WHERE k.kind='nhtsa_complaint' AND {where}", args).fetchall()
    groups = {}
    for title, ref, mi in rows:
        g = groups.setdefault(_component(title), {"n": 0, "miles": []})
        g["n"] += 1
        if mi:
            g["miles"].append(mi)
    total = len(rows)
    lines.append(f"NHTSA owner complaints for {scope}: {total}" + ("" if has_miles else
                 " (mileage not in this database yet)"))
    ranked = sorted(groups.items(), key=lambda x: -x[1]["n"])[:top + 3]
    for comp, g in ranked:
        m = g["miles"]
        mtxt = (f", typical mileage {statistics.median(m):,.0f} (middle half {_q(m, .25):,.0f}-{_q(m, .75):,.0f}, "
                f"from {len(m)} that gave it)") if len(m) >= 3 else ""
        lines.append(f"- {comp}: {g['n']} complaints{mtxt}")
    if miles and has_miles:
        band = next((b for b in BANDS if b[0] <= miles < b[1]), None)
        if band:
            near = {}
            for title, ref, mi in rows:
                if mi and band[0] <= mi < band[1]:
                    near[_component(title)] = near.get(_component(title), 0) + 1
            if near:
                lines.append(f"At this car's mileage ({miles:,} mi, band {band[2]}): " + "; ".join(
                    f"{c} {n}" for c, n in sorted(near.items(), key=lambda x: -x[1])[:top]))
    rec = db.execute(f"SELECT k.ref, k.title, substr(k.body, 1, 400) FROM knowledge k WHERE kind='recall' AND {where} "
                     "GROUP BY k.ref", args).fetchall()
    lines.append(f"Recalls: {len(rec)}")
    for ref, title, body in rec[:8]:
        lines.append(f"- {ref.split(' ')[0]} ({_component(title)}): {_cut(body, 220)}")
    tsb = db.execute(f"SELECT k.title FROM knowledge k WHERE kind='tsb' AND {where}", args).fetchall()
    tg = {}
    for (title,) in tsb:
        comp = _component(title.split("):", 1)[-1] if "):" in title else title)
        tg[comp] = tg.get(comp, 0) + 1
    lines.append(f"TSBs (factory service bulletins): {len(tsb)}" + ("; most by area: " + "; ".join(
        f"{c} {n}" for c, n in sorted(tg.items(), key=lambda x: -x[1])[:top]) if tg else ""))
    ff = db.execute(f"SELECT k.title, k.confirmed FROM knowledge k WHERE kind='forum_fix' AND {where}", args).fetchall()
    if ff:
        titles = [t for t, _ in ff]
        lines.append(f"Forum fixes from owners ({len(ff)} threads, {sum(1 for _, c in ff if c)} confirmed fixed), "
                     "by part of the car (from thread titles):")
        for name, ts in sorted(_systems(titles).items(), key=lambda x: -len(x[1]))[:top + 2]:
            lines.append(f"- {name}: {len(ts)} threads, e.g. " + "; ".join(_cut(t, 60) for t in ts[:3]))
        codes = {}
        for t in titles:
            for c in set(re.findall(r"\b[PBCU]\d{4}\b", (t or "").upper())):
                codes[c] = codes.get(c, 0) + 1
        if codes:
            lines.append("Codes owners asked about most: " + ", ".join(
                f"{c} ({n})" for c, n in sorted(codes.items(), key=lambda x: -x[1])[:8]))
    else:
        lines.append("Forum fixes: none in this database for this car")
    db.close()
    return {"scope": scope, "lines": lines}


def _q(xs, p):
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


PROMPT = ("You are helping a mechanic answer a phone call about a {car}{mi}, often from someone deciding whether to "
          "buy it. Below are counts worked out from NHTSA owner complaints, recalls, factory bulletins and owner "
          "forum fixes for this car. Using ONLY these facts, give the top 5 problems this car is known for, most "
          "common first. For each: what fails, at roughly what mileage (only if a mileage is given below), and what "
          "to look or ask for before buying. Name the source and its count in brackets, e.g. [NHTSA 34 complaints]. "
          "Then one line on open recalls to check. NHTSA complaints lean toward safety items, forum fixes toward "
          "repairs; say so if that skews the list. If the data is thin, say so instead of guessing. Plain sentences, "
          "short.\n\nFACTS ({scope}):\n{facts}")
