"""
/maintenance: the factory service schedule for the car on Intake, read in Python from CHARM's
"Maintenance > Service Intervals > Normal Service > <N> MI or <M> KM" pages (Severe Service with
/maintenance severe). No model is involved: every item shown is a line of those pages, with the page it came from.

A page lists its items under bare action lines ("Inspect", "Replace", "Rotate"); each item may be followed by an
instruction line ("Perform at mileage interval or every 30 months, whichever occurs first."). The schedule pages stop
at some mileage (often 150,000); past that, each item repeats at its own spacing in the table, and the list says so.
"""
import re

ACTIONS = {"inspect", "replace", "rotate", "adjust", "lubricate", "clean", "change", "check", "drain", "flush",
           "tighten", "service", "perform", "test", "lube"}
MI_RE = re.compile(r"(\d[\d,]*)\s*MI\b", re.I)
LOOKBACK = 25000   # 10-25: what should have been done over the past 25,000 miles


def _pages(db, vehicle_ids, kind):
    """{mileage: (path, [body parts in order])} for this car's Normal (or Severe) Service interval pages."""
    q = ",".join("?" * len(vehicle_ids))
    rows = db.execute(
        "SELECT s.path, s.part, s.body FROM section s JOIN doc d ON d.id=s.doc_id WHERE d.kind='charm' "
        f"AND s.path LIKE ? AND s.id IN (SELECT section_id FROM section_vehicle WHERE vehicle_id IN ({q})) "
        "ORDER BY s.path, s.part", [f"%Service Intervals > {kind} Service > %"] + list(vehicle_ids)).fetchall()
    pages = {}
    for path, part, body in rows:
        m = MI_RE.search(path.rsplit(" > ", 1)[-1])
        if not m:
            continue
        mi = int(m.group(1).replace(",", ""))
        p = pages.setdefault(mi, (path, {}))
        p[1].setdefault(part or 1, body)      # the same page can come from more than one vehicle id
    return {mi: (path, [parts[k] for k in sorted(parts)]) for mi, (path, parts) in pages.items()}


def _is_instruction(line):
    return line.endswith(".") or len(line.split()) > 6


def parse_page(parts):
    """[(action, item, instruction)] from one interval page's body parts (first line of each part is a heading)."""
    out, action, item, note = [], "", None, ""
    lines = []
    for body in parts:
        lines += [l.strip() for l in body.split("\n")[1:] if l.strip()]
    for line in lines:
        if line.lower() in ACTIONS:
            if item:
                out.append((action, item, note))
            action, item, note = line.capitalize(), None, ""
        elif _is_instruction(line) and item:
            note = (note + " " + line).strip()
        elif not _is_instruction(line):
            if item:
                out.append((action, item, note))
            item, note = line, ""
    if item:
        out.append((action, item, note))
    return out


def schedule(db, vehicle_ids, severe=False):
    """{mileage: [(action, item, instruction)]} and {mileage: source path}, or ({}, {})."""
    if not vehicle_ids:
        return {}, {}
    pages = _pages(db, vehicle_ids, "Severe" if severe else "Normal")
    return ({mi: parse_page(parts) for mi, (_, parts) in pages.items()},
            {mi: path for mi, (path, _) in pages.items()})


def plan(sched, miles, lookback=LOOKBACK):
    """Items due in (miles - lookback, miles] ("should have been done"), at the next service point ("due next")
    and at the two points after it ("later"). Returns dict(past, next, longer, later, next_mi, later_mi, last_mi,
    extrapolated); each entry is dict(key, action, item, note, period, at=[mileages], src=page mileage)."""
    if not sched:
        return {}
    last = max(sched)
    seen = {}
    for mi in sorted(sched):
        for action, item, note in sched[mi]:
            e = seen.setdefault((action, item), {"key": f"{action}|{item}", "action": action, "item": item,
                                                 "note": note, "at": []})
            e["at"].append(mi)
    steps = sorted(sched)
    step = min(b - a for a, b in zip(steps, steps[1:])) if len(steps) > 1 else steps[0]
    next_mi = (miles // step + 1) * step
    later_mi = [next_mi + step, next_mi + 2 * step]
    past, nxt, extrap, longer, later = [], [], miles + step > last, [], []
    for e in seen.values():
        at = e["at"]
        period = min((b - a for a, b in zip(at, at[1:])), default=at[0])
        e["period"] = period
        points = list(at)
        while points[-1] < later_mi[-1]:      # past the table: the item's own spacing
            points.append(points[-1] + period)
        src = lambda p: p if p in sched else at[-1]
        hit = [p for p in points if miles - lookback < p <= miles]
        if hit:
            past.append(dict(e, at=hit, src=src(hit[-1])))
        elif period > lookback:     # a long-interval job (timing belt, plugs...) last due before the window
            before = [p for p in points if p <= miles - lookback]
            if before:
                longer.append(dict(e, at=[before[-1]], src=src(before[-1])))
        if next_mi in points:
            nxt.append(dict(e, at=[next_mi], src=src(next_mi)))
        soon = [p for p in points if p in later_mi]
        if soon:
            later.append(dict(e, at=soon, src=src(soon[0])))
    return {"past": past, "next": nxt, "longer": longer, "later": later, "next_mi": next_mi, "later_mi": later_mi,
            "last_mi": last, "extrapolated": extrap}


GROUPS = ("past", "next", "longer", "later")


def checklist(p):
    """One row per item for the Maintenance screen, in its most urgent group: [(group, entry)], where the entry
    also carries due_text (every point it falls on, e.g. 'was due 130,000, 135,000; next 142,500')."""
    if not p:
        return []
    by_key = {}
    for g in GROUPS:
        for e in p.get(g, []):
            by_key.setdefault(e["key"], {})[g] = e
    out = []
    for g in GROUPS:
        for key, gs in by_key.items():
            if min(gs, key=GROUPS.index) != g:
                continue
            e = dict(gs[g])
            bits = []
            if "past" in gs:
                bits.append("was due " + ", ".join(f"{a:,}" for a in gs["past"]["at"]))
            if "longer" in gs:
                bits.append(f"was due {gs['longer']['at'][-1]:,}")
            if "next" in gs:
                bits.append(f"next {p['next_mi']:,}")
            elif "later" in gs:
                bits.append(f"next {gs['later']['at'][0]:,}")
            e["due_text"] = "; ".join(bits)
            out.append((g, e))
    return sorted(out, key=lambda x: (GROUPS.index(x[0]), x[1]["action"], x[1]["item"]))


def next_after(entry, done_mi):
    """When an item done at done_mi comes due again, by the schedule's spacing for it."""
    return done_mi + entry["period"] if done_mi and entry.get("period") else None


def _short(note, n=150):
    """The instruction's first sentence or two, for the list (the full text stays on the CHARM page)."""
    out = ""
    for sent in re.split(r"(?<=\.)\s+", note or ""):
        if out and len(out) + len(sent) > n:
            break
        out = (out + " " + sent).strip()
    return out


def render(p, miles, severe=False):
    """Numbered lines for the chat, and the entries in the same order (for /addmaint N)."""
    if not p:
        return "", []
    lines, order = [], []
    kind = "Severe" if severe else "Normal"
    lines.append(f"{kind} Service schedule (CHARM), car at {miles:,} mi:")
    if p["extrapolated"]:
        lines.append(f"(The schedule pages stop at {p['last_mi']:,} mi; later points repeat each item's own spacing "
                     "from those pages.)")
    lines.append(f"DUE NEXT, at {p['next_mi']:,} mi:")
    for e in sorted(p["next"], key=lambda e: (e["action"], e["item"])):
        order.append(e)
        lines.append(f"  {len(order)}. {e['action']} {e['item']}" + (f": {_short(e['note'])}" if e["note"] else ""))
    lines.append(f"SHOULD HAVE BEEN DONE in the last {LOOKBACK:,} mi (ask the customer):")
    for e in sorted(p["past"], key=lambda e: (e["action"], e["item"])):
        order.append(e)
        lines.append(f"  {len(order)}. {e['action']} {e['item']} (at {', '.join(f'{a:,}' for a in e['at'])} mi)"
                     + (f": {_short(e['note'])}" if e["note"] else ""))
    if p.get("longer"):
        lines.append("LONG-INTERVAL JOBS last due before that (ask when they were really done):")
        for e in sorted(p["longer"], key=lambda e: (e["action"], e["item"])):
            order.append(e)
            lines.append(f"  {len(order)}. {e['action']} {e['item']} (due at {e['at'][-1]:,} mi)"
                         + (f": {_short(e['note'])}" if e["note"] else ""))
    return "\n".join(lines), order
