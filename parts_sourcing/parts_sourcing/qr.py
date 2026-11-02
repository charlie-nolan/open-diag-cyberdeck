"""Offline QR codes and sourcing links for an OEM part number.

The deck never fetches prices: each QR opens the search on the phone, which shows the live price.
QR codes come from `segno` (pure Python, no network; `pip install segno`).
"""
import json
import os
import urllib.parse
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Link:
    tier: str
    tier_label: str
    site: str
    label: str
    url: str
    verified: bool


def load_sites(path=None):
    with open(path or os.path.join(HERE, "sites.json")) as f:
        return json.load(f)["tiers"]


def links_for(part_number, zip_code="98101", sites=None):
    pn = part_number.strip().upper()
    subs = {"pn": urllib.parse.quote(pn), "pn_lc": urllib.parse.quote(pn.lower()),
            "q": urllib.parse.quote_plus(pn), "zip": urllib.parse.quote(zip_code)}
    out = []
    for t in sites or load_sites():
        for s in t["sites"]:
            out.append(Link(t["tier"], t["label"], s["id"], s["label"].format(zip=zip_code), s["url"].format(**subs),
                            s.get("verified", False)))
    return out


def _segno():
    try:
        import segno
        return segno
    except ImportError:
        return None


def available():
    return _segno() is not None


def qr_svg(url, scale=3, border=2):
    """Inline <svg> markup (dark modules black on white so phones read it in any theme), or None."""
    segno = _segno()
    if not segno:
        return None
    q = segno.make(url, error="m", micro=False)
    import io
    buf = io.BytesIO()
    q.save(buf, kind="svg", scale=scale, border=border, dark="#000", light="#fff", xmldecl=False,
           svgns=True, nl=False)
    return buf.getvalue().decode()


def qr_png(url, path, scale=6, border=2):
    segno = _segno()
    if not segno:
        raise RuntimeError("segno not installed (pip install segno)")
    segno.make(url, error="m", micro=False).save(path, kind="png", scale=scale, border=border)
    return path


def qr_terminal(url):
    """Unicode block QR for a terminal; None without segno."""
    segno = _segno()
    if not segno:
        return None
    import io
    buf = io.StringIO()
    segno.make(url, error="m", micro=False).terminal(out=buf, compact=True)
    return buf.getvalue()
