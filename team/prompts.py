"""Team mode prompts. Rule 2: format and schema only; never a car, a code or a cause as an example.

home_verdict() is the main app's Final Diagnosis prompt (cyberdeck_gui.CyberdeckApp.act_verdict, PC case: up to 5
issues, no maintenance block). It is copied, not imported, because it is built inline in the GUI method; check it
against act_verdict when that changes.
"""

VERDICT_FORMAT = ("SUMMARY: <2-3 sentences: how many problems, and the order to work in>\n"
                  "=== ISSUE 1: <short name> ===\n"
                  "WHY HERE: <why it's ranked here>\n"
                  "FOR: <evidence that fits>\n"
                  "AGAINST: <evidence that doesn't fit, or none>\n"
                  "TEST: <the test that confirms or clears it, and what each result means>\n"
                  "REPAIR: <the job if confirmed, in a few words>\n"
                  "PARTS: <part names only, comma separated; no actions or steps; or none>\n"
                  "=== ISSUE 2: <short name> ===\n(same fields)\n")


def home_verdict(case, ctx, most=5):
    return (f"CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
            "Write a ranked work plan for this car. Cover every code in the case.\n"
            f"- Up to {most} ISSUES, only as many as the evidence supports; one is right when one cause "
            "explains everything. Separate faults (or a second part that also has to be bought) are separate "
            "issues.\n"
            "- Order: the likeliest and cheapest/easiest to confirm first. Say why each one sits where it does.\n"
            "- Every idea the mechanic states in his notes gets its own issue with the evidence for and "
            "against it, even if you rank it low.\n"
            "- A part he says he replaced or checked is never issue 1. List it last, as a recheck, only if "
            "the evidence still points at it, and say why.\n"
            "- Use only the case, live data and manual text above, and quote the real numbers. Never write "
            "part numbers or prices: the deck adds them from its database.\n\n"
            "Write exactly this format:\n"
            "SUMMARY: <2-3 sentences: how many problems, and the order to work in>\n"
            "=== ISSUE 1: <short name> ===\n"
            "WHY HERE: <why it's ranked here>\n"
            "FOR: <evidence that fits>\n"
            "AGAINST: <evidence that doesn't fit, or none>\n"
            "TEST: <the test that confirms or clears it, and what each result means>\n"
            "REPAIR: <the job if confirmed, in a few words, e.g. replace front brake pads>\n"
            "PARTS: <part names only, comma separated, e.g. thermostat, radiator cap; "
            "no actions or steps; or none>\n"
            "=== ISSUE 2: <short name> ===\n(same fields)\n"
            "Keep each issue under 120 words.")


RULES = (
    "Rules (the shop's, not negotiable):\n"
    "1. Source rank: the factory manual's diagnostic procedure first, then TSBs/recalls, then owner reports and "
    "videos (ideas only), then the general web. A number from a forum, video or web page never stands in for a "
    "manual spec. If no source gives a spec, write that none was found; never estimate one.\n"
    "2. Every factual claim ends with the evidence id(s) it rests on, in square brackets, e.g. [M3] or [O1, W2]. "
    "Facts taken from the CASE itself (codes, live data, the mechanic's notes) cite (case). "
    "Ids come only from the teamkb tools' replies. A web page you want to cite must be fetched with teamkb "
    "fetch_page first; a page you only saw in search results can't be cited. Reasoning of your own is allowed "
    "but must be marked (own judgement). A claim with a missing or wrong id is dropped.\n"
    "3. Live-data meanings in the CASE were worked out by the deck in Python; use them instead of judging raw "
    "numbers yourself.\n"
    "4. Nothing may be sent to the car. Tests are for the mechanic to do with the car in front of him.\n"
    "5. Don't assume anything the case doesn't say. If the evidence can't separate two causes, say which test "
    "would.\n")


def _tools(pack_id, web, local=False, db=None):
    db = bool(pack_id) if db is None else db
    lines = ["Your tools: the teamkb MCP server (this car only, read-only): " +
             ("search_manual (one procedure per call), dtc_procedure, specs, " if db else "") +
             "card (counted forum/TSB fixes), owner_reports, youtube_cache, read_evidence" +
             (", fetch_page, youtube_transcript" if web else "") + ". "
             + ("Database text (manual, CHARM, cards, owner reports) is capped at about 40k characters per member per "
                "case; search freely within it." if db else "")]
    if pack_id:
        lines.append(f"The manual text the shop's own model was given is evidence {pack_id} (read_evidence).")
    elif not db:
        lines.append("You do not get the factory manual's text in this run; the coordinator checks the manual.")
    if web:
        lines.append("Start with the databases; go outside them when they don't settle a point or you're stuck, and "
                     "keep your token budget in mind. A fact from outside the databases needs at least two "
                     "independent sources that agree, each fetched and cited.")
        lines.append("Web search is allowed: cite only pages fetched with fetch_page. YouTube: search it for repair "
                     "and diagnosis videos matching this car (make, model, year, engine), its codes, the symptoms and "
                     "the live-data readings; pull the captions of the best few (at most 3) with youtube_transcript "
                     "and cite the id plus the timestamp of the caption line that says it. A video is ideas only.")
    elif not local:
        lines.append("Web search is OFF for this run; do not search or fetch the web.")
    if not local:
        lines.append("You may write and run small scripts in the current folder (it is deleted after the run), "
                     "e.g. to work through the live data. Nothing else on this machine is yours to read or change.")
    return "\n".join(lines)


def diagnose(case, pack_id, web, local=False):
    return ("You are one of several independent master technicians diagnosing the same car. The others will "
            "check your work, so be specific and cite everything.\n\n"
            f"CASE (what the shop's scan tool and the mechanic recorded):\n{case}\n\n{RULES}\n"
            f"{_tools(pack_id, web, local)}\n\n"
            "Time box: you have about 5 minutes. Do at most 10 lookups or fetches (the trouble codes' procedures, "
            "owner reports, the few pages or videos that matter most), then stop researching and write your "
            "diagnosis in exactly this format, up to 5 issues, the likeliest and cheapest-to-confirm first, each "
            "issue under 150 words, every claim cited:\n\n" + VERDICT_FORMAT)


def crosscheck(case, me, dx, base):
    others = "\n\n".join(f"--- DIAGNOSIS BY {w.upper()} ---\n{t}" for w, t in dx.items() if w != me and t)
    mine = dx.get(me) or "(yours failed to arrive)"
    return ("Cross-check round. Below is the case, your own diagnosis, the other technicians' diagnoses"
            + (" and the shop's local model's verdict (BASELINE)" if base else "") + ".\n\n"
            f"CASE:\n{case}\n\n--- YOUR DIAGNOSIS ---\n{mine}\n\n{others or '(no other diagnoses)'}\n\n"
            + (f"--- BASELINE ---\n{base}\n\n" if base else "") + RULES +
            "\nYou may use the teamkb tools again (read_evidence shows any id the others cited), at most 5 calls; "
            "you have about 3 minutes. Reply in exactly "
            "these sections, under 400 words in all:\n"
            "AGREE: <points from the others you accept, with their ids>\n"
            "WRONG: <claims you think are wrong or unsupported, whose, and why, with ids; check the ids they "
            "cite actually say what they claim>\n"
            "TEST FIRST: <the cheapest test that would separate the leading causes, and why>\n"
            "CHANGED MY MIND: <what you now rank differently, or nothing>\n")


def verdict(case, pack_id, pack, dx, cross, base, cited=""):
    parts = [f"--- {w.upper()} DIAGNOSIS ---\n{t}" for w, t in dx.items() if t]
    parts += [f"--- {w.upper()} CROSS-CHECK ---\n{t}" for w, t in cross.items() if t]
    if base:
        parts.append(f"--- BASELINE (shop's local model, given the same manual text) ---\n{base}")
    return ("You are the lead technician. Your team has diagnosed this car; weigh their work against the factory "
            "manual and write the shop's final work plan.\n\n"
            f"CASE:\n{case}\n\n[{pack_id}] MANUAL PACK (the manual text for this case):\n{pack or '(none)'}\n\n"
            + ("\n\n".join(parts) or "(no team diagnoses arrived; work from the case and the manual)") + "\n\n"
            + (f"EVIDENCE THE TEAM CITED (the text behind each id, so you can check it says what they claim):\n"
               f"{cited}\n\n" if cited else "")
            + RULES +
            "\nCheck each kept claim against the cited text above (read_evidence for anything not shown), "
            "and to look up anything the team missed. Drop any claim whose id is missing or doesn't say what is "
            "claimed. When members disagree, the manual decides; when the manual is silent, say so.\n"
            "- Cover every code in the case; up to 5 issues, only as many as the evidence supports.\n"
            "- A part the mechanic says he replaced or checked is never issue 1.\n"
            "- Never write part numbers or prices: the deck adds them from its database.\n\n"
            "Write exactly this format, every claim cited, each issue under 150 words:\n" + VERDICT_FORMAT +
            "=== TEAM NOTES ===\n<who argued what, where they disagreed, what you dropped and why (with the ids)>\n")


def round_question(case, question, board, web, db=True, local=False):
    return ("Brainstorm round. You are one of several technicians working this car together.\n\n"
            f"CASE:\n{case}\n\n" + (f"CURRENT BOARD:\n{board}\n\n" if board else "") +
            f"THE MECHANIC ASKS:\n{question}\n\n{RULES}\n{_tools(None, web, local, db)}\n\n"
            "Answer in under 300 words: your hypotheses for what he asked, each with FOR, AGAINST and the cheapest "
            "test, every claim cited.")


def board(case, question, prev, answers, cited=""):
    parts = "\n\n".join(f"--- {w.upper()} ---\n{t}" for w, t in answers.items() if t)
    return ("Merge the team's answers into the hypothesis board.\n\n"
            f"CASE:\n{case}\n\n" + (f"PREVIOUS BOARD:\n{prev}\n\n" if prev else "") +
            f"QUESTION:\n{question}\n\n{parts or '(no answers arrived)'}\n\n"
            + (f"EVIDENCE THE TEAM CITED:\n{cited}\n\n" if cited else "") + f"{RULES}\n"
            "Check cited ids with read_evidence and the manual tools. Write:\n"
            "BOARD:\n- <hypothesis> | FOR: <...> | AGAINST: <...> | CHEAPEST TEST: <...>\n"
            "DISAGREEMENTS: <where members differ and what would settle it>\n"
            "ANSWER: <2-4 sentences answering the mechanic>\n")


def ask_member(task, web, deep, db=True, local=False):
    """A member's brief for one of the main app's own tasks (chat, debate, verdict, repair guide...)."""
    box = ("Time box: about 5 minutes and at most 10 lookups." if deep else
           "Time box: about 2 minutes and at most 4 lookups; this is a quick answer the mechanic is waiting for.")
    return ("You are one of several technicians on a team answering the shop app's request below. The lead "
            "technician will check your work and write the final answer, so be specific and cite everything.\n\n"
            f"{RULES}\n{_tools(None, web, local, db)}\n{box}\n\n"
            "Answer the TASK in the format and length it asks for, every claim cited.\n\n=== TASK ===\n" + task)


def ask_lead(task, answers, cross, cited, deep):
    parts = [f"--- {w.upper()} ---\n{t}" for w, t in answers.items() if t]
    parts += [f"--- {w.upper()} CROSS-CHECK ---\n{t}" for w, t in cross.items() if t]
    return ("You are the lead technician. Your team answered the shop app's request below; weigh their answers "
            "against the factory manual (your teamkb tools) and write the final answer.\n\n"
            + ("\n\n".join(parts) or "(no team answers arrived; answer from the task and your own lookups)")
            + "\n\n" + (f"EVIDENCE THE TEAM CITED:\n{cited}\n\n" if cited else "") + RULES +
            "\nDrop any claim whose id is missing or doesn't say what is claimed; when members disagree, the manual "
            "decides. Write the answer to the TASK exactly in the format and length the TASK asks for (the app "
            "parses it), every claim cited. Then a line '=== TEAM NOTES ===' followed by "
            + ("who argued what, where they disagreed, and what you dropped and why." if deep else
               "one or two lines: where the members disagreed or what you dropped, or 'none'.")
            + "\n\n=== TASK ===\n" + task)
