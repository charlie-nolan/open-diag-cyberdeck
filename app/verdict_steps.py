"""
Final Diagnosis in small steps, for models that don't follow one long ranking prompt well (Qwen on the PC, the deck's
RKLLM 4B). Sim v4 (2026-11-13): Claude led correctly in 39-40 of 40 cases from one long prompt; the Qwen 80B in 25-29,
and it ignored rules a long prompt spelled out. Here the model does short jobs and Python does the ranking:

  1. candidates   the model lists the causes worth checking, from the manual's steps and the case (evidence only:
                  the opinion fields are added afterwards as candidates of their own, marked as someone's guess)
  2. checks       one short fixed-format question set per candidate: does the evidence fit, is there evidence against
                  it, does the manual point there, what is the cheapest test (and how costly)
  3. rank         Python orders them from those answers (fit, manual, evidence against, guess-only, test cost as the
                  tie-break). No symptom->cause lists: every point comes from the model's own cited answer
  4. write        the model writes the usual plan for the ranked causes, in that order

Also here: hard_stop(), the NEEDS MORE DATA answer the deck writes itself when the DATA CHECK flags the codes or
readings a diagnosis would rest on (no model call: the models diagnosed impossible data even when told not to).
Stdlib only.
"""
import re

MAX_CANDIDATES = 6

# ------------------------------------------------------------------------------------------- hard stop
_STOP_KINDS = ("code", "live")
_CHECK_FOR = {
    "code": "Re-read the codes with the scan tool set to this car (right make/model/year, engine module), and "
            "confirm {item}: {reason}",
    "live": "Re-read the live data with the engine running and the scan tool set to this car, and confirm "
            "{item}: {reason}",
    "equipment": "Confirm on the car whether it really has {item} ({reason})",
}


def hard_stop(flags):
    """NEEDS MORE DATA text for real code/live-data flags, or '' when there are none to stop on."""
    real = [f for f in flags or [] if f.get("level", "flag") == "flag"]
    if not any(f["kind"] in _STOP_KINDS for f in real):
        return ""
    why = "; ".join(f"{f['item']}" for f in real if f["kind"] in _STOP_KINDS)[:300]
    lines = [f"NEEDS MORE DATA: the deck's data check flags readings or codes a diagnosis would rest on ({why})."]
    seen = set()
    for f in real:
        t = _CHECK_FOR.get(f["kind"], "Confirm {item}: {reason}").format(item=f["item"], reason=f["reason"])
        key = (f["kind"], f["item"])
        if key not in seen:
            seen.add(key)
            lines.append(f"TEST: {t} [deck data check: {f['source']}]")
        if len(lines) > 6:
            break
    lines.append("TEST: Once the data is confirmed, add it in Intake or BRAINSTORM / CHAT and run FINAL DIAGNOSIS again.")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- 1. candidates
def candidates_prompt(top, case, ctx, most=MAX_CANDIDATES):
    return (f"{top}CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
            f"List up to {most} causes worth checking for this car, from the manual's diagnostic steps for its codes "
            "and from the case's own symptoms and readings. Include the cheap causes the manual checks first. Don't "
            "rank them and don't explain; one line each:\nCAUSE: <short name of the fault or part> | FROM: <the manual "
            "step/page or the case reading that suggests it>")


def parse_candidates(text, most=MAX_CANDIDATES):
    out = []
    for ln in re.sub(r"(?s)<think>.*?</think>", "", text or "").splitlines():
        m = re.match(r"\W*(?:\d+[.)]\s*)?CAUSE\s*:\s*(.+?)(?:\s*\|\s*FROM\s*:\s*(.*))?$", ln.strip(), re.I)
        if m and m.group(1).strip(" *"):
            name = m.group(1).strip(" *")
            if name.lower() not in {c["name"].lower() for c in out}:
                out.append({"name": name[:120], "from": (m.group(2) or "").strip()[:200], "guess": False})
        if len(out) >= most:
            break
    return out


def opinion_candidates(opinions_text):
    """The withheld opinion fields as candidates of their own, marked as someone's guess."""
    out = []
    for ln in (opinions_text or "").splitlines():
        label, _, val = ln.partition(":")
        if val.strip():
            out.append({"name": val.strip()[:160], "from": label.strip()[:80], "guess": True})
    return out


# ------------------------------------------------------------------------------------------- 2. checks
def check_prompt(top, case, ctx, cand):
    guess = (" This cause is someone's guess (an opinion field), not a finding: judge it on the evidence only."
             if cand.get("guess") else "")
    return (f"{top}CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
            f"CANDIDATE CAUSE: {cand['name']}{guess}\n"
            "Answer these five lines only, each citing the case or a manual page:\n"
            "EXPLAINS: <each fact of the case this cause explains (a code, a reading, a symptom, a condition), "
            "separated by ';', or none>\n"
            "DOESN'T EXPLAIN: <each fact of the case this cause can't explain, separated by ';', or none>\n"
            "AGAINST: yes|no - <a part verified good or recently replaced, or a reading that rules it out>\n"
            "MANUAL: yes|no - <the manual step or page that points to it, or 'none'>\n"
            "TEST: quick|moderate|major - <the cheapest test that confirms or clears it> (quick = minutes with a meter, "
            "scan tool or eyes; moderate = an hour or a gauge; major = disassembly)")


def _field(text, name, choices):
    m = re.search(rf"(?im)^\W*{name}\s*:\s*\**\s*({'|'.join(choices)})\b\**\s*[-–:]?\s*(.*)$", text or "")
    return (m.group(1).lower(), m.group(2).strip()[:300]) if m else (None, "")


def _facts(text, name):
    m = re.search(rf"(?im)^\W*{name}\s*:\s*\**\s*(.*)$", text or "")
    if not m or re.match(r"(?i)\W*(none|nothing|n/?a)\W*$", m.group(1)):
        return []
    return [x.strip(" .*") for x in m.group(1).split(";") if x.strip(" .*")]


def parse_check(text):
    """The model's check of one candidate. Fit is counted from the facts it lists, not a yes/no (small models
    answered "fits: yes" for every candidate)."""
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "")
    explains, misses = _facts(text, "EXPLAINS"), _facts(text, "DOESN'?T EXPLAIN")
    if explains or misses:
        fits = "yes" if explains and not misses else "partly" if explains else "no"
        fits_why = "explains: " + "; ".join(explains) + (" | doesn't explain: " + "; ".join(misses) if misses else "")
    else:
        fits, fits_why = _field(text, "FITS", ["yes", "partly", "no"])
    against, against_why = _field(text, "AGAINST", ["yes", "no"])
    manual, manual_why = _field(text, "MANUAL", ["yes", "no"])
    cost, test = _field(text, "TEST", ["quick", "moderate", "major"])
    return {"fits": fits, "fits_why": fits_why[:400], "explains": len(explains), "misses": len(misses),
            "against": against, "against_why": against_why,
            "manual": manual, "manual_why": manual_why, "cost": cost, "test": test,
            "parsed": all(x is not None for x in (fits, against, manual, cost))}


_FAULT_WORDS = {"sticking", "stuck", "partially", "closed", "open", "failure", "failing", "faulty", "worn", "leak",
                "leaking", "loose", "weak", "dirty", "clogged", "intermittent", "degradation", "internal", "incorrect",
                "improper", "malfunction", "damaged", "broken", "bad", "low", "high", "noise", "noisy"}


def _part_words(name):
    import answer_check as A
    stop = {"in", "of", "the", "and", "to", "on", "at", "or", "for", "from", "by", "with", "a", "an", "e.g", "eg"}
    return {w for w in re.findall(r"[a-z0-9]{2,}", (name or "").lower())} - A._GENERIC - _FAULT_WORDS - stop


def case_against(cands, case, window=6):
    """Python's own "evidence against" from the case's fields: a candidate whose part words the "verified good /
    ruled out" field names (all of them in one clause), or that the history says was just replaced or fitted (a
    replace word in the same clause near all of them,
    no "no/never" before it). The models' checks missed these (a thermostat fitted last month got "against: no")."""
    import answer_check as A
    for c in cands:
        pw = _part_words(c["name"])
        if not pw:
            continue
        for field, kind in (("ruled_out", "verified good"), ("history", "replaced")):
            hit = ""
            for clause in re.split(r"[.;,\n]", case.get(field) or ""):
                toks = re.findall(r"[a-z0-9]{2,}", clause.lower())
                if kind == "verified good":
                    if pw <= set(toks):
                        hit = clause.strip()
                else:
                    for i, t in enumerate(toks):
                        if A._REPLACED.fullmatch(t) and not (A._NEG & set(toks[:i])) and \
                                pw <= set(toks[max(0, i - window):i + window + 1]):
                            hit = clause.strip()
                if hit:
                    break
            if hit:
                c["case_against"] = (kind, hit)
                break
    return cands


# ------------------------------------------------------------------------------------------- 3. rank
_FIT = {"yes": 3, "partly": 1, "no": 0, None: 0}
_COST = {"quick": 0, "moderate": 1, "major": 2, None: 1}


def score(c):
    if c.get("explains") or c.get("misses"):
        fit = min(c["explains"], 4) - c.get("misses", 0)          # facts it explains, minus facts it can't
    else:
        fit = _FIT[c.get("fits")]
    s = fit + (2 if c.get("manual") == "yes" else 0) - (6 if c.get("against") == "yes" else 0)
    if c.get("case_against") and c.get("against") != "yes":
        s -= 6 if c["case_against"][0] == "verified good" else 3   # a new part can be the fault, a tested one rarely
    if c.get("guess"):             # the guess itself is not evidence: it needs the manual or more facts to lead
        s -= 1 if c.get("manual") == "yes" else 2
    return s


def rank(cands):
    """Highest score first; on a tie, the cheaper test first (Charlie's tie-break), then the model's own order."""
    order = {id(c): i for i, c in enumerate(cands)}
    return sorted(cands, key=lambda c: (-score(c), _COST[c.get("cost")], order[id(c)]))


def ranking_text(ranked):
    lines = []
    for n, c in enumerate(ranked, 1):
        bits = [f"fit: {c.get('fits') or '?'} ({c.get('fits_why') or '-'})",
                f"against: {c.get('against') or '?'} ({c.get('against_why') or '-'})",
                f"manual: {c.get('manual') or '?'} ({c.get('manual_why') or '-'})",
                f"test: {c.get('cost') or '?'} ({c.get('test') or '-'})"]
        if c.get("case_against"):
            bits.append(f"case says: {c['case_against'][0]} ({c['case_against'][1][:120]})")
        lines.append(f"{n}. {c['name']}" + (" [someone's guess]" if c.get("guess") else "") + " | " + " | ".join(bits))
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------- 4. write
def write_prompt(top, case, ctx, ranked, fmt, most, opinions=""):
    keep = [c for c in ranked if score(c) > 0][:most] or ranked[:1]
    return (f"{top}CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
            "RANKED CAUSES (ranked by the deck from the checks shown; keep this order):\n" + ranking_text(keep) + "\n\n"
            + ("OPINIONS (guesses, not test results): " + opinions.replace("\n", "; ") + "\n"
               "Start with one line per opinion:\nOPINION CHECK: <opinion> - <supported / not supported / untested>: "
               "<the evidence, and the test>\n\n" if opinions else "")
            + f"Write the work plan with one issue per ranked cause above, in that order ({len(keep)} issues). Use the "
            "checks' evidence and the manual text, quote real numbers, and never write part numbers or prices. Then "
            + fmt[0].lower() + fmt[1:])
