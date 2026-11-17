"""
ANSWER CHECK: the deck's Python check of a Final Diagnosis plan after the model wrote it (no LLM). Shown in its own
colour, apart from the source check (app/faithfulness.py), which checks numbers against pages.

    notes = check(issues, case, retriever)   -> ["ISSUE 1 ...: reason", ...]   ([] when nothing to say)

Two checks on ISSUE 1, each from the case's own fields or the car's own DB, never from a cause list:
  - a part ISSUE 1 blames that the case's "verified good / ruled out" field names, or that the history says was
    replaced, installed or new (that wording is the history's, the part names are the model's own PARTS field)
  - a part ISSUE 1 blames that this car's manual never names (kb_retriever.Retriever.mentions == 0)
Stdlib only.
"""
import re

# words that describe a fault or a job, not a part: never the word that decides whether the manual names a part
_GENERIC = {"connector", "connectors", "harness", "wiring", "wire", "circuit", "repair", "kit", "damaged", "disturbed",
            "only", "assembly", "faulty", "failure", "failing", "worn", "loose", "leak", "leaking", "fault", "intermittent",
            "issue", "problem", "replace", "replacement", "check", "inspection", "test", "possible", "suspected", "side",
            "front", "rear", "left", "right", "upper", "lower", "inner", "outer", "contamination", "misalignment"}
_REPLACED = re.compile(r"\b(replac\w*|install\w*|new|fitted|rebuilt|swapped)\b", re.I)


def _words(s):
    return [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())]


def _named_in(part, text):
    """All the part name's words in one sentence of text (word order free): 'front o2 sensor' in
    'O2 sensor (front) replaced at 120k'."""
    pw = set(_words(part)) - {"the", "and", "for", "with", "assembly", "kit"}
    if not pw:
        return ""
    for sent in re.split(r"[.;\n]", text or ""):
        if pw <= set(_words(sent)):
            return sent.strip()
    return ""


_NEG = {"no", "never", "not", "without", "nor"}


def _replaced_in(part, text, window=6):
    """The history sentence that says this part was replaced/fitted: the replace word within `window` words of
    every word of the part's name, and no "no / never / not" earlier in the clause ("No record of the timing belt ever
    being replaced" is not a replacement). '' when none."""
    pw = set(_words(part)) - {"the", "and", "for", "with", "assembly", "kit"}
    if not pw:
        return ""
    for sent in re.split(r"[.;,\n]", text or ""):              # one clause at a time
        toks = re.findall(r"[a-z0-9]{2,}", sent.lower())      # 2+ letters: keeps "no"
        for i, t in enumerate(toks):
            if not _REPLACED.fullmatch(t):
                continue
            if _NEG & set(toks[:i]):                       # "no record of ... ever replaced"
                continue
            near = set(toks[max(0, i - window):i + window + 1])
            if pw <= near:
                return sent.strip()
    return ""


def _parts(issue):
    names = [p.strip(" .") for p in re.split(r"[,;\n]", issue.get("PARTS", "") or "")
             if p.strip(" .") and p.strip(" .").lower() not in ("none", "n/a", "-", "none needed")]
    return names or ([issue.get("name")] if issue.get("name") else [])


def check(issues, case, r=None):
    first = next((d for d in issues or [] if not d.get("also")), None)
    if not first:
        return []
    out = []
    good, hist = case.get("ruled_out") or "", case.get("history") or ""
    for part in _parts(first):
        s = _named_in(part, good)
        if s:
            out.append(f"ISSUE 1 blames '{part}', but the case lists it as verified good / ruled out: \"{s[:120]}\"")
            continue
        s = _replaced_in(part, hist)
        if s:
            out.append(f"ISSUE 1 blames '{part}', which the history says was replaced or fitted: \"{s[:120]}\". "
                       "Fine if the fault is that new part itself (wrong size, faulty, fitted wrong); say so.")
            continue
        if r is not None and hasattr(r, "mentions"):
            # a part is "never named" only when one of its specific words is on no page of this car's manual:
            # long descriptive names ("FL ABS sensor connector/harness disturbed") never match as a whole
            missing = []
            for w in dict.fromkeys(x for x in re.findall(r"[a-z]{4,}", part.lower()) if x not in _GENERIC):
                try:
                    if r.mentions(w, min_words=1) == 0:
                        missing.append(w)
                except Exception:
                    pass
            if missing:
                out.append(f"ISSUE 1 blames '{part}', but this car's manual never uses the word "
                           f"{', '.join(repr(m) for m in missing)}: check the car has it, or use the manual's name.")
    return out
