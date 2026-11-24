"""Case context for the models: manual hits, confirmed fixes, community data, parts names, retriever."""
import charm_pics
import glob
import json
import os
import procedures
import re
import sqlite3
import sys
from PyQt6.QtGui import QImage, QPixmap
from retrieval import Retriever as LegacyRetriever
from vehicle_id import decode_vin, kb_path_for
from ui.config import CFG, ROOT


class NullRetriever:
    """No knowledge DB for this make (or no make picked): codes, chat and verdict still work, without manuals."""
    db = None

    def context_for(self, *a, **k):
        return ""

    def __getattr__(self, name):
        return lambda *a, **k: []


def kb_status(make):
    """(path, exists, plain message) for a make's knowledge DB."""
    if not make:
        return "", False, "No car picked yet: choose a Make, or type the VIN, to load its manuals."
    path, ok = kb_path_for(make, CFG["kb_dir"])
    if ok:
        return path, True, f"Manuals: {os.path.basename(path)}"
    return path, False, (f"No database for {make} yet ({os.path.basename(path)} is not in {CFG['kb_dir']}). "
                         "Codes, chat and verdict still work, without manual pages, specs or parts.")


def ecu_vins():
    """{ECU id: VIN} typed in for cars that don't report a VIN themselves."""
    try:
        with open(os.path.join(CFG["cache_dir"], "ecu_vins.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember_ecu_vin(ecu, vin):
    known = ecu_vins()
    if known.get(ecu) == vin:
        return
    known[ecu] = vin
    try:
        os.makedirs(CFG["cache_dir"], exist_ok=True)
        with open(os.path.join(CFG["cache_dir"], "ecu_vins.json"), "w") as f:
            json.dump(known, f, indent=1)
    except OSError:
        pass


def case_make(v):
    """The make of a saved case: as saved, else from its VIN, else '' so Intake asks for it. Never guessed."""
    if v.get("make"):
        return v["make"]
    if v.get("vin"):
        return decode_vin(v["vin"], CFG.get("vin_db"))["make"]
    return ""


def use_make(make):
    """Point CFG at a make's knowledge DB. Returns the plain status message."""
    CFG["make"] = make or ""
    CFG["kb_db"], _, msg = kb_status(make)
    return msg


def find_embed_model(cfg):
    """Folder of Qwen3-Embedding-0.6B for vector search, or '' (keyword search only). Never downloads."""
    want = (cfg.get("embed_model") or "").strip()
    if want.lower() == "off":
        return ""
    if want:
        return os.path.expanduser(want)
    hubs = [os.environ.get("HF_HUB_CACHE", ""), os.path.join(os.environ.get("HF_HOME", ""), "hub"),
            os.path.expanduser("~/.cache/huggingface/hub"), f"{ROOT}/models/hf_cache/.cache/huggingface/hub"]
    folders = [f"{ROOT}/models/qwen3emb"] + [d for h in hubs if h for d in sorted(
        glob.glob(os.path.join(h, "models--Qwen--Qwen3-Embedding-0.6B", "snapshots", "*")), reverse=True)]
    return next((d for d in folders if os.path.isfile(os.path.join(d, "config.json"))), "")


def squeeze(t):
    """Markdown table rules ("|------|") waste the small models' context."""
    return re.sub(r"-{4,}", "---", t)


FIG_W = 460


def fsm_hits(r, text, k=3, chars=900):
    """search_fsm, plus each hit's manual figures: [(text, [figure files])]. The files are
    images/<doc id>/<name>.webp next to the kb (kb/ship_images.py makes them), in the section's own
    order, which is the order of its "[fig]" marks. A file that isn't there is skipped when shown."""
    kb = os.path.expanduser(CFG.get("kb_db") or "")
    if not all(hasattr(r, a) for a in ("search_ids", "_row", "_fmt", "db")) or not os.path.isfile(kb):
        return [(h, []) for h in r.search_fsm(text, k=k, chars=chars)]
    img_dir = os.path.join(os.path.dirname(kb), "images")
    out, seen = [], set()
    for sid in r.search_ids(text, k * 3):
        row = r._row(sid)
        if row is None:
            continue
        path, page, body, kind = row
        base = path.split(" — ")[0]
        if base in seen:
            continue
        seen.add(base)
        figs = []
        meta = r.db.execute("SELECT doc_id, images FROM section WHERE id=?", (sid,)).fetchone()
        if meta and meta[1]:
            try:
                figs = []
                for n in json.loads(meta[1]):
                    if charm_pics.is_charm(n):
                        figs.append(n)
                        continue
                    f = os.path.join(img_dir, str(meta[0]), os.path.splitext(n)[0] + ".webp")
                    if not os.path.isfile(f):
                        f = charm_pics.fsm_figure(kb, CFG["cache_dir"], meta[0], n) or f
                    figs.append(f)
            except (ValueError, TypeError):
                figs = []
        out.append((squeeze(r._fmt(path, page, body, chars, kind)), figs))
        if len(out) == k:
            break
    return out


def log_caught(where, e):
    """For handlers that degrade instead of crashing (a field tool keeps going): say what was caught."""
    print(f"[{where}] {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def load_figure(path):
    """QPixmap of a figure, or a null one. Pillow decodes it if Qt has no WebP plugin."""
    pm = QPixmap(path)
    if pm.isNull():
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("L")
                qi = QImage(im.tobytes(), im.width, im.height, im.width, QImage.Format.Format_Grayscale8)
                pm = QPixmap.fromImage(qi.copy())
        except Exception as e:
            log_caught(f"figure {os.path.basename(path)}", e)
    if not pm.isNull() and pm.width() > FIG_W:
        pm = pm.scaledToWidth(FIG_W)
    return pm


def spaced(text):
    """A blank line between every line of a result, so each bit of info stands apart."""
    return re.sub(r"\n\s*\n*", "\n\n", (text or "").strip())


JOB_VERBS = ("replace", "replacing", "install", "reinstall", "remove", "renew", "repair", "rebuild", "clean", "inspect",
             "check", "test", "tighten", "retighten", "torque", "retorque", "re-torque", "adjust", "reseal",
             "resurface", "fit", "swap", "change", "flush", "bleed", "refill", "top up", "lubricate", "reconnect",
             "secure", "order", "buy", "fix", "machine", "cut", "drain", "fill", "reset", "clear", "verify")


def parts_from_job(job):
    """The parts a repair line names, for the parts search: 'Replace exhaust manifold gasket, clean mating
    surfaces, torque to spec.' -> ['exhaust manifold gasket']; 'Replace spark plugs and ignition coil' ->
    ['spark plugs', 'ignition coil']. Text without an action word comes back as it is (already a part name or
    a part number)."""
    if not job:
        return []
    v = "|".join(re.escape(x) for x in JOB_VERBS)
    t = re.sub(r"\([^)]*\)", " ", job.strip())
    t = re.sub(r"(?i)^\s*(?:(?:%s)\b\s*(?:(?:and|&|,|/)\s*)?)+" % v, "", t)
    t = re.split(r"[,;:.]|\s+(?:and then|then|and)\s+(?=(?:%s)\b)" % v, t, maxsplit=1)[0]
    t = re.sub(r"(?i)\s+(?:if|as needed|per|with|to spec|on|at|for|from|in)\b.*$", "", t)
    out = []
    for n in re.split(r"(?i)\s+(?:and|&)\s+|\s*/\s*", t):
        n = re.sub(r"(?i)^(?:the|a|an|new|both|all)\s+", "", n.strip())
        n = re.sub(r"(?i)^(?:#\s*\d+|no\.\s*\d+|cylinder\s*\d+)\s+", "", n).strip(" .-")
        if n:
            out.append(n)
    return out or [job.strip()]


def part_from_job(job):
    """The first part a repair line names (see parts_from_job)."""
    p = parts_from_job(job)
    return p[0] if p else ""


def confirmed_fixes(r, text, k=3, chars=400):
    """Forum fixes (knowledge kind forum_fix) the owner confirmed, for this car's model and years, closest to
    text first. Every make DB has them (make_full step e). [] when none or on any error."""
    try:
        ids = r.knowledge_ids(text, k=k * 4, kinds=["forum_fix"])
        out = []
        for i in ids:
            row = r.db.execute("SELECT model, year_min, title, body, url, confirmed FROM knowledge WHERE id=?",
                               (i,)).fetchone()
            if not row or not row[5]:
                continue
            model, yr, title, body, url, _ = row
            body = " ".join((body or "").split())
            body = body if len(body) <= chars else body[:chars].rsplit(" ", 1)[0] + " …"
            out.append(f"- {model} {yr or ''}: {title or ''} {url or ''}\n  {body}")
            if len(out) == k:
                break
        return out
    except Exception as e:
        print(f"confirmed fixes skipped: {e}")
        return []


def community_block(r, text, codes, cap, raw=True):
    """Community reports for a case, below the manual and the cards (10-31): what owners extracted threads
    say (comm_* tables, when the make has them), then the closest unchecked threads (kb/<make>.threads.db). Every
    line keeps its link. Empty when the make has neither, or on any error."""
    path = getattr(r, "kb_path", None)
    if not path or cap < 200:
        return ""
    try:
        import community_ref
        model, year, embed = getattr(r, "model", None), getattr(r, "year", None), getattr(r, "embed", None)
        code = codes[0] if codes else None
        lines = []
        try:
            c = community_ref.community(path, code=code, text=text, model=model, year=year, embed=embed, top=4)
        except sqlite3.Error:
            c = {"causes": []}
        for x in c["causes"]:
            lines.append(f"- {x['cause']}: {x['threads']} threads, {x['op_confirmed']} confirmed fixed by the owner "
                         f"({x['car_match']})" + (f" {x['urls'][0]}" if x["urls"] else ""))
        if lines:
            lines.insert(0, "[COMMUNITY REPORTS: from owner threads; they suggest what to test, the manual confirms]")
        if not raw:
            fx = confirmed_fixes(r, text, k=3, chars=max(150, cap // 5))
            if fx:
                lines.append("[CONFIRMED OWNER FIXES: same model; the owner reported this fixed it]")
                lines += fx
        t = (community_ref.raw_threads(path, text=text, code=code, model=model, year=year, embed=embed, k=2,
                                       excerpt=max(200, min(1200, cap // 3))) if raw else {"threads": []})
        if t["threads"]:
            lines.append("[OWNER THREADS: unchecked posts, ideas only, never a spec or a step]")
            for x in t["threads"]:
                lines.append(f"- {x['title']} ({x['car_match']}) {x['url'] or ''}\n  "
                             + " ".join(re.sub(r"\[S\d+[^\]]*\]", " ", x["excerpt"]).split()))
        out, used = [], 0
        for i, ln in enumerate(lines):
            if used + len(ln) + 1 > cap:
                break
            out.append(ln)
            used += len(ln) + 1
        while out and out[-1].startswith("["):
            out.pop()
        return "\n".join(out)
    except Exception as e:
        print(f"community lookup skipped: {e}")
        return ""


def case_context(r, text, budget=2500, scraped=True, verified_only=False):
    """Manual text for a case, manual first (10-19). Each code's diagnostic procedure comes from
    procedures.py as whole numbered steps (the old cut kept ~600-1500 chars of a 3-18k step table, so the
    models never saw a step), with the steps that only ask which codes are set answered from the case.
    Codes the manual handles in one procedure (several codes can point to one chart) share it. Then the
    card's likely causes (forum/TSB, secondary), then the best other manual page. scraped=False leaves out
    everything not from a manual (cards, TSB/complaint/forum snippets). verified_only=True keeps the manual first
    and adds only fixes owners confirmed (the card's causes, extracted community fixes), not the unchecked
    threads: chat uses that since 11-01 (it dismissed real ideas for not being on the tree)."""
    codes = list(dict.fromkeys(c.upper() for c in re.findall(r"\b[PBCU]\d{4}\b", text.upper())))
    if not codes or not hasattr(r, "card") or not getattr(r, "db", None):
        if scraped or not hasattr(r, "search_fsm"):
            comm = community_block(r, text, [], budget // 6, raw=not verified_only) if scraped else ""
            if verified_only and hasattr(r, "search_fsm"):
                manual = "\n\n".join(r.search_fsm(text, k=2))[:max(0, budget - len(comm))]
            else:
                manual = r.context_for(text, budget=budget - len(comm))
            return "\n\n".join(x for x in (manual, comm) if x)
        return "\n\n".join(r.search_fsm(text, k=2))[:budget]
    head = (f"[CODES TOGETHER] {', '.join(codes)}: this car has all of them at once; treat them as one problem."
            if len(codes) > 1 else "")
    defs, procs, seen = [], [], set()
    for code in codes:
        first = (r.dtc(code, chars=0) or "").split("\n", 1)[0]
        if first.startswith("[DTC]"):
            defs.append(first)
        p = procedures.fetch(r, code)
        key = p["title"] if p else code
        if key in seen:
            continue
        seen.add(key)
        procs.append((p, code))
    share = int(budget * 0.85 / max(1, len(procs)))
    blocks = []
    for p, code in procs:
        if p:
            blocks.append(procedures.render(p, codes, share))
        else:
            raw = squeeze(re.sub(r"(?m)^\|?[\s\-:|]+\|?$|^\[DTC\].*$", "", r.dtc(code, chars=share) or ""))
            if raw.strip():
                blocks.append("[MANUAL TEXT for " + code + "]\n" + raw)
    cards = []
    for code in (codes if scraped else []):
        c = r.card(code)
        if c:
            c = c.split("Manual procedure outline:", 1)[0].strip()
            cards.append(c[:max(200, budget // (6 * len(codes)))])
    cards = (["[REPORTED CAUSES: owner-reported forum and TSB fixes counted for this car. Supporting evidence only, "
              "ranked by how often owners reported them, not by the manual. The manual's procedure above decides the "
              "order of work]"] + cards if cards else [])
    query = " ".join([text] + [d.split(":", 1)[-1] for d in defs])
    pages = [h for h in r.search_fsm(query, k=2) if not any(c in h.split("\n", 1)[0] for c in codes)][:1]
    parts = [x for x in [head] + defs + blocks + cards + pages if x]
    comm = (community_block(r, query, codes, budget // 6, raw=not verified_only)
            if scraped else "")
    out, used = [], len(comm)
    for part in parts:
        part = _fit(part, budget - used)
        if part:
            out.append(part)
            used += len(part) + 2
    return "\n\n".join(out + ([comm] if comm else []))


def cited_pages(ctx, plan):
    """The FSM DATA blocks a first-pass plan used: every code's diagnostic procedure and definition, plus each other
    block whose heading words (or a number from its text) the plan repeats. Pass 2 of the blind verdict reads these
    instead of all of FSM DATA."""
    import faithfulness
    plan_l = (plan or "").lower()
    plan_nums = set(re.findall(r"\d+(?:\.\d+)?", plan_l))
    keep = []
    for label, text in faithfulness.pages(ctx or "")[1:]:
        lab = label.lower()
        if lab.startswith(("manual diagnostic procedure", "dtc", "codes together", "manual text for", "picture read")):
            keep.append(text.strip())
            continue
        words = {w for w in re.findall(r"[a-z]{5,}", lab)} - {"repair", "diagnosis", "testing", "inspection",
                                                               "description", "operation", "manual"}
        nums = {n for n in re.findall(r"\d+\.\d+|\d{2,}", text) if not re.fullmatch(r"(19|20)\d\d", n)}
        if sum(w in plan_l for w in words) >= 2 or len(nums & plan_nums) >= 2:
            keep.append(text.strip())
    return "\n\n".join(keep) or "(none cited)"


def _fit(text, room):
    """text cut to room chars at a line end (else a word end, marked '…'), never mid-word."""
    if len(text) <= room:
        return text
    if room < 40:
        return ""
    cut = text[:room]
    nl = cut.rfind("\n")
    if nl > room // 2:
        return cut[:nl]
    return cut[:room - 1].rsplit(" ", 1)[0] + "…"


def make_retriever(cfg):
    """The make's kb (sourced torque specs, whole procedures) when it exists. Subaru alone keeps the older two
    DBs as a fallback; any other make without a kb gets an empty retriever (Intake says so)."""
    kb = os.path.expanduser(cfg.get("kb_db", ""))
    if not (kb and os.path.isfile(kb)) and cfg.get("make") != "Subaru":
        return NullRetriever()
    if kb and os.path.isfile(kb):
        sys.path.insert(0, os.path.dirname(kb))
        if f"{ROOT}/kb" not in sys.path:
            sys.path.insert(1, f"{ROOT}/kb")
        from kb_retriever import Retriever
        embed = None
        model_dir = find_embed_model(cfg)
        if model_dir:
            try:
                import embedder
                embed = embedder.shared(model_dir, threads=cfg.get("embed_threads", 4))
                embed.warm()
            except Exception as e:
                print(f"vector search off: {e}")
        r = Retriever(kb, model=cfg.get("kb_model"), year=cfg.get("kb_year"), embed=embed)
        r.kb_path = kb
        ctx, fsm = r.context_for, r.search_fsm
        r.context_for = lambda *a, **k: squeeze(ctx(*a, **k))
        r.search_fsm = lambda *a, **k: [squeeze(h) for h in fsm(*a, **k)]
        return r
    return LegacyRetriever(cfg["diag_db"], cfg["knowledge_db"], cfg["cache_dir"])


GENERIC = set("front rear left right upper lower inner outer side new old gasket gaskets seal bolt bolts nut nuts "
              "screw screws replace replacement repair removal install installation leak leaking cause causing "
              "most likely bad worn cylinder cylinders bank misfire".split())
STOPWORDS = set("the and for with from this that what which into onto off out".split())
SHOP_WORDS = {"exhaust manifold": "exhaust pipe", "cam belt": "timing belt", "valve cover": "rocker cover",
              "o2 sensor": "oxygen sensor", "lug nut": "wheel nut"}


def focus_specs(job, specs):
    """Keep spec rows about the part being repaired. The kb search ORs every word, so rows that only share
    'front' or 'gasket' with the job used to rank in. For a short job ('exhaust manifold gasket') its first
    part word must appear; for verdict text, rows that share more part words come first."""
    low = job.lower()
    low += " " + " ".join(v for k, v in SHOP_WORDS.items() if k in low)
    words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]{3,}", low)) if w not in GENERIC and w not in STOPWORDS]
    if not words:
        return specs
    has = lambda w, t: re.search(rf"\b{w}", t) is not None  # noqa: E731  (prefix match: gasket ~ gaskets)
    scored = [(sum(has(w, r.lower()) for w in words), r) for r in specs]
    if len(job.split()) <= 6:
        scored = [(n, r) for n, r in scored if has(words[0], r.lower())]
    return [r for n, r in sorted(scored, key=lambda x: -x[0]) if n > 0]


CASE_MAX_CHARS = 2500
CHART_READS_MAX = 6000
SCREENS = (("intake", "1. INTAKE && SCAN"), ("maint", "2. MAINTENANCE"), ("chat", "3. BRAINSTORM / CHAT"),
           ("verdict", "4. FINAL DIAGNOSIS"), ("sop", "5. REPAIR GUIDE (SOP)"), ("parts", "6. PARTS"),
           ("tests", "7. TESTS"))
PAGE = {name: i for i, (name, _) in enumerate(SCREENS)}
DRIVE_KEY = {"key": lambda: "drive_log_short"}


OPINION_FIELDS = (("suspect", "Suspect / known defects (the owner's or another shop's idea)"),
                  ("earlier_visits", "Earlier visits and their verdicts (deck's records; verdicts not confirmed by a test)"),
                  ("saved_debate", "Saved debate (competing explanations weighed, unresolved)"),
                  ("saved_wildcard", "Saved wildcard ideas (long shots the mechanic wanted kept)"))


MODELS = ["Forester", "Impreza", "Legacy", "Outback", "WRX", "WRX STI", "Baja", "Crosstrek", "Tribeca", "SVX",
          "Ascent", "BRZ", "XV Crosstrek"]
TRANS = ["", "Manual", "Automatic"]
REPAIRS = sorted(["exhaust manifold gasket", "spark plugs", "ignition coil", "spark plug wires", "timing belt",
                  "head gasket", "valve cover gasket", "front O2 sensor", "rear O2 sensor", "PCV valve", "thermostat",
                  "water pump", "intake manifold gasket", "fuel injector", "knock sensor", "crankshaft position sensor",
                  "camshaft position sensor"], key=str.lower)


VERDICT_KEYWORDS = [("exhaust manifold", "exhaust manifold gasket"), ("coil", "ignition coil"), ("plug wire", "spark plug wires"),
                    ("spark plug", "spark plugs"), ("timing belt", "timing belt"), ("head gasket", "head gasket"),
                    ("injector", "fuel injector"), ("thermostat", "thermostat"), ("o2", "front O2 sensor"),
                    ("oxygen sensor", "front O2 sensor"), ("knock", "knock sensor"), ("pcv", "PCV valve")]


def repair_from_verdict(verdict):
    """The repair named in the verdict's root cause (section 1 only, so ruled-out parts don't count)."""
    root = re.split(r"\n[\s#*_>-]*2\s*[.):]", verdict, maxsplit=1)[0].lower()

    def named(word):
        """Where the root cause names word as the problem: not 'not the O2 sensor' / 'O2 sensor reads normal'."""
        for m in re.finditer(re.escape(word), root):
            before = root[max(0, m.start() - 25):m.start()]
            after = root[m.end():m.end() + 30]
            if re.search(r"\b(not|no|isn't|rule[sd]? out|ruled out)\b[^.]*$", before) or \
                    re.search(r"^[^.]*\b(normal|good|fine|ok|okay|within spec|ruled out)\b", after):
                continue
            return m.start()
        return -1

    hits = [(i, r) for r in REPAIRS if (i := named(r.lower())) >= 0]
    hits = hits or [(i, r) for k, r in VERDICT_KEYWORDS if (i := named(k)) >= 0]
    return min(hits)[1] if hits else None
