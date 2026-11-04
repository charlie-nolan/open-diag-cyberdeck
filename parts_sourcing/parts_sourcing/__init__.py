"""Parts sourcing for the cyberdeck: OEM part number -> QR sourcing links (new OEM, aftermarket, used)
plus how many cars at each nearby self-service yard probably carry the part.

    from parts_sourcing import sourcing, parts_for_job
    s = sourcing("14038AA000")                 # interchange + links + yard report
    print(s["text"])

Everything here reads kb/subaru.db and the cached yards.db; nothing touches the car or the network.
"""
import json
import os

from . import interchange, qr, yards
from .interchange import parts_for_job, normalize_pn  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))


ROOT = os.path.dirname(os.path.dirname(HERE))      # the checkout


def settings(path=None):
    """sourcing.json; its relative DB paths are relative to the checkout."""
    with open(path or os.path.join(HERE, "sourcing.json")) as f:
        cfg = json.load(f)
    for k in ("kb_db", "yards_db"):
        if cfg.get(k):
            cfg[k] = os.path.join(ROOT, os.path.expanduser(cfg[k]))
    return cfg


def sourcing(part_number, name=None, price=None, cfg=None):
    cfg = cfg or settings()
    kb_db = os.path.expanduser(cfg["kb_db"])
    yards_db = os.path.expanduser(cfg["yards_db"])
    pn = normalize_pn(part_number)
    ic = interchange.lookup(kb_db, pn)
    links = qr.links_for(pn, cfg.get("home_zip", "98101"))
    rep = yards.report(yards_db, kb_db, ic) if os.path.exists(yards_db) else None
    if not name and ic.numbers.get(pn):
        name = ic.numbers[pn]
    if not price:
        price = next((f.price for f in ic.fits if f.part_number == pn and f.price), None)
    lines = [f"{pn}{' · ' + name if name else ''}", f"Fits (CHARM): {ic.summary()}"]
    others = [n for n in ic.numbers if n != pn]
    if others:
        lines.append(f"Same part family (other revisions, check supersession): {', '.join(others)}")
    lines += [f"{ln.tier_label}: {ln.label} {ln.url}" for ln in links]
    lines.append(yards.format_report(rep) if rep else ("Yard inventory: no yard list yet. Copy a yard's cars from Row52 in your browser and run "
                 "parts_sourcing.yard_paste, or use the Row52 link and look for the cars on the fit list above."))
    jq = None
    if os.path.exists(yards_db):
        from . import junk_quote          # late import: junk_quote imports settings() from here
        jq = junk_quote.quote(pn, cfg)
        if jq["price"] or jq["labor"]:
            lines.append(jq["text"])
    return {"part_number": pn, "name": name, "price": price, "interchange": ic, "links": links,
            "yards": rep, "junk_quote": jq, "text": "\n".join(lines)}
