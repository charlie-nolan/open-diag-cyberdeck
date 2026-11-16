"""
The manual's diagnostic procedure for a trouble code, as numbered steps the models can actually follow.

Why: the procedures are long step tables (median ~3,400 chars, up to ~18,000, mostly table padding), and
the old context cut each one at 600-1,500 chars. The models saw the detecting condition and the table
header and never a single step, so the answers came from the forum-ranked card instead of the manual.
Codes whose page only says "refer to DTC P0304" got nothing at all.

Here every step row becomes one line, "refer to DTC X" pages are followed, and the steps that only ask
which codes the scan tool shows are answered in Python from the case's codes (step 18 "Does the scan
tool indicate DTC P0303 and P0304?" -> Yes -> step 23). The rest is left for the mechanic and the model.
Works for any code and any manual written in the FSM's "No. | Step | Check | Yes | No" table format.
"""
import re

CODE_RE = re.compile(r"\b[PBCU]\d{4}\b")
REFER_RE = re.compile(r"refer to DTC ([PBCU]\d{4})", re.I)
SEP_RE = re.compile(r"^\|?[\s\-:|]+\|?$")


def _clean(text):
    text = re.sub(r"<Ref\. to [^>]*>", "", text)
    text = re.sub(r"<[^>]+>", " ", text)  # html lists the converter left in cells
    text = text.replace("\\Omega", "Ω").replace(" ;", ";")
    return re.sub(r"[ \t]+", " ", text).strip(" ;")


def _sections(r, code):
    """Section bodies for code in the manual's order (CHARM's copy only when there is no FSM one)."""
    vf, args = r._veh_filter() if hasattr(r, "_veh_filter") else ("", [])
    rows = r.db.execute(
        "SELECT s.body, doc.kind FROM dtc_procedure d JOIN section s ON s.id=d.section_id "
        f"JOIN doc ON doc.id=s.doc_id WHERE d.code=?{vf} GROUP BY s.id ORDER BY doc.kind='charm', s.id",
        [code] + list(args)).fetchall()
    if any(kind != "charm" for _, kind in rows):
        rows = [x for x in rows if x[1] != "charm"]
    return [b for b, _ in rows]


def parse(bodies):
    """(header lines, {step number: (step, check, yes, no)}) from the section bodies."""
    head, steps = [], {}
    for body in bodies:
        for line in body.splitlines():
            s = line.strip()
            if not s or SEP_RE.match(s) or s.startswith("| No. |") or s.startswith("(continued"):
                continue
            if s.startswith("|"):
                cells = [_clean(x) for x in s.strip("|").split("|")]
                if len(cells) >= 5 and cells[0].isdigit():
                    steps[int(cells[0])] = tuple(cells[1:5])
                continue
            if s.startswith("### ") and ("DTC" in s and "—" in s):
                continue  # the title, repeated on every continued section
            if s.startswith("[PICTURE READ"):   # steps a vision model read from the manual's pictures (kb/car_db.py vision)
                if s not in head:
                    head.insert(0, s)           # first, so the prompt always says they are unverified
                continue
            if s != "[fig]":
                head.append(_clean(s.lstrip("# ")))
    return head, steps


def fetch(r, code, _seen=None):
    """{code, title, head, steps, via} or None when the kb has no procedure for code."""
    if not getattr(r, "db", None):
        return None
    seen = _seen or set()
    seen.add(code)
    bodies = _sections(r, code)
    if not bodies:
        return None
    head, steps = parse(bodies)
    m = REFER_RE.search(" ".join(bodies))
    if not steps and m and m.group(1) not in seen:  # "For the diagnostic procedure, refer to DTC P0304."
        target = fetch(r, m.group(1), seen)
        if target:
            target["via"] = code
            return target
    title = re.search(r"DTC [PBCU]\d{4}[^\n(]*", bodies[0])
    return {"code": code, "title": _clean(title.group(0)) if title else code, "head": head, "steps": steps,
            "via": None}


def answered(steps, codes):
    """{step: 'Yes'|'No'} for steps that only ask which codes the scan tool shows."""
    have = set(codes)
    out = {}
    for n, (_, check, yes, no) in steps.items():
        if not re.search(r"\bindicate\b|\bdisplayed\b", check, re.I):
            continue
        if re.search(r"only one DTC", check, re.I):
            out[n] = "Yes" if len(have) == 1 else "No"
            continue
        if re.search(r"any other DTC", check, re.I):
            # "other" = codes besides the ones the step's own NOTE says not to inspect (this procedure's codes)
            mine = set(CODE_RE.findall(yes + " " + no))
            out[n] = "Yes" if (have - mine if mine else len(have) > 1) else "No"
            continue
        listed = CODE_RE.findall(check)
        if listed and re.search(r"indicate DTC", check, re.I):
            out[n] = "Yes" if all(c in have for c in listed) else "No"
    return out


def _next(cell):
    m = re.search(r"Go to step (\d+)", cell)
    return int(m.group(1)) if m else None


def path_from_scan(steps, ans):
    """Follow each run of scan-answered steps (a chart can have several, e.g. step 1 "any other DTC?" and a
    later "which codes?" block); returns [(step, answer, next)] for all runs in step order."""
    def nxt_of(n):
        _, _, yes, no = steps[n]
        return _next(yes if ans[n] == "Yes" else no)
    # a run starts at an answered step that no other answered step can lead to (either branch)
    fed = {_next(cell) for n in ans for cell in steps[n][2:4]}
    out = []
    for start in sorted(n for n in ans if n not in fed):
        n, guard = start, 0
        while n in ans and guard < 50:
            guard += 1
            nx = nxt_of(n)
            out.append((n, ans[n], nx))
            n = nx
    return out


def _cut(s, n):
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"


def render(proc, codes, budget=3500):
    """The procedure as text for a prompt, under budget chars. Steps on the scan-answered path and the
    step it lands on are shown in full; if it's still too long, the other steps are shortened."""
    steps = proc["steps"]
    lines = [f"[MANUAL DIAGNOSTIC PROCEDURE: {proc['title']}"
             + (f" (the manual's page for {proc['via']} says to use this one)" if proc["via"] else "")
             + ". Primary source: follow its steps in order.]"]
    lines += [_cut(h, 300) for h in proc["head"] if not h.upper().startswith(("WIRING DIAGRAM", "CAUTION"))][:6]
    if not steps:
        return _cut("\n".join(lines), budget)
    ans = answered(steps, codes)
    path = path_from_scan(steps, ans)
    if path:
        lines.append("Answered from the scan (codes " + ", ".join(codes) + "): " + "; ".join(
            f"step {n}: {a}" + (f" -> step {nx}" if nx else "") for n, a, nx in path))
    keep = {nx for _, _, nx in path if nx and nx not in ans}  # where the scan answers lead: always in full

    def fmt(n, short):
        step, check, yes, no = steps[n]
        if short and n not in keep:
            step, yes, no = _cut(step, 90), _cut(yes, 90), _cut(no, 90)
        return f"{n}. {step} | {check} | Yes: {yes} | No: {no}"

    def brief(n):  # "2. CHECK OUTPUT SIGNAL FROM ECM. Is the voltage more than 10 V? Yes: step 7 | No: step 3"
        step, check, yes, no = steps[n]
        if n in ans:
            nxt = _next(yes if ans[n] == "Yes" else no)
            return f"{n}. {_cut(step.split(';')[0], 50)} Answered from the scan: {ans[n]}" + (f" -> step {nxt}" if nxt else "")
        go = [f"step {_next(x)}" if _next(x) and len(x) < 40 else _cut(x, 40) for x in (yes, no)]
        return f"{n}. {_cut(step.split(';')[0], 50)} {_cut(check, 55)} Yes: {go[0]} | No: {go[1]}"

    tiers = (lambda n: fmt(n, False), lambda n: fmt(n, True),
             lambda n: fmt(n, False) if n in keep else brief(n))
    for tier in tiers:
        text = "\n".join(lines + [tier(n) for n in sorted(steps)])
        if len(text) <= budget:
            return text
    # still too long: the scan path and where it lands in full, everything else as its title only, then
    # shorter titles, then only the other steps' numbers. The steps in keep are never the ones that get cut:
    # cutting the whole text at the budget used to drop exactly the step the scan leads to (it's often last).
    for tl in (60, 30):
        body = [fmt(n, False) if n in keep else f"{n}. {_cut(steps[n][0].split(';')[0], tl)}" for n in sorted(steps)]
        text = "\n".join(lines + body)
        if len(text) <= budget:
            return text
    others = [n for n in sorted(steps) if n not in keep]
    tail = [f"Other steps ({_ranges(others)}): titles left out for space; the full chart is in the manual."] if others else []
    kept = [fmt(n, False) for n in sorted(keep) if n in steps]
    text = "\n".join(lines + kept + tail)
    if len(text) <= budget:
        return text
    # the kept steps alone are too long: shorten each one evenly, still keeping every one of them
    room = max(120, (budget - len("\n".join(lines + tail))) // max(1, len(kept)) - 1)
    return "\n".join(lines + [_cut(k, room) for k in kept] + tail)


def _ranges(ns):
    """[3,4,5,9,10,12] -> '3-5, 9-10, 12'"""
    out, start, prev = [], None, None
    for n in ns:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}-{prev}" if prev != start else f"{start}")
            start = prev = n
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else f"{start}")
    return ", ".join(out)
