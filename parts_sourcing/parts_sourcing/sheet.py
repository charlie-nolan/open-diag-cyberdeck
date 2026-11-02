"""Printable parts-sourcing sheet: one block per part with QR codes for each tier and the yard counts.
Self-contained HTML (inline SVG QRs, no network) that prints on one page per few parts."""
import datetime as dt
import html

from . import qr

CSS = """
body{font-family:system-ui,sans-serif;margin:16px;color:#111;background:#fff}
h1{font-size:20px;margin:0 0 4px} .sub{color:#555;font-size:12px;margin-bottom:12px}
.part{border:1px solid #bbb;border-radius:6px;padding:10px 12px;margin-bottom:14px;page-break-inside:avoid}
.part h2{font-size:17px;margin:0} .fit{font-size:12px;color:#333;margin:4px 0 8px}
.tiers{display:flex;flex-wrap:wrap;gap:10px}
.q{width:150px;text-align:center;font-size:11px} .q svg{width:130px;height:130px}
.q .u{color:#a60;font-size:10px}
table{border-collapse:collapse;font-size:12px;margin-top:8px;width:100%}
td,th{border-bottom:1px solid #ddd;padding:3px 6px;text-align:left;vertical-align:top}
.lv-likely{font-weight:600} .note{font-size:11px;color:#555;margin-top:6px}
@media print{body{margin:8mm}}
"""


def _qr_cell(title, url, unverified=False):
    svg = qr.qr_svg(url) or ""
    return (f'<div class="q">{svg}<div><b>{html.escape(title)}</b></div>'
            + ('<div class="u">link pattern unverified</div>' if unverified else "") + "</div>")


def part_block(s):
    """s = parts_sourcing.sourcing(...) result."""
    ic, links, rep = s["interchange"], s["links"], s["yards"]
    title = html.escape(s["part_number"]) + (f" &middot; {html.escape(s['name'])}" if s.get("name") else "")
    out = [f'<div class="part"><h2>{title}</h2>',
           f'<div class="fit">Fits per CHARM: {html.escape(ic.summary())}'
           + (f" &middot; related numbers: {html.escape(', '.join(n for n in ic.numbers if n != ic.part_number))}"
              if len(ic.numbers) > 1 else "")
           + (f" &middot; CHARM list price {html.escape(s['price'])}" if s.get("price") else "") + "</div>",
           '<div class="tiers">']
    for ln in links:                       # every link in sites.json order (a short list since 2026-10-19)
        out.append(_qr_cell(f"{ln.tier_label}: {ln.label}", ln.url, not ln.verified))
    if rep:
        for y in rep.yards[:3]:
            if y.cars:
                c = y.cars[0]
                out.append(_qr_cell(f"{y.yard}: {c.year} {c.model}, row {c.row or '?'}", c.url))
    out.append("</div>")
    if rep:
        out.append("<table><tr><th>Yard</th><th>Likely</th><th>Same family</th><th>Possible</th><th>Best cars</th></tr>")
        for y in rep.yards:
            best = "; ".join(f'<span class="lv-{c.level}">{c.year} {html.escape(c.model)} {c.engine.label()} '
                             f'row {html.escape(c.row or "?")}</span>' for c in y.cars[:3])
            out.append(f"<tr><td>{html.escape(y.yard)}</td><td>{y.count('likely')}</td><td>{y.count('family')}</td>"
                       f"<td>{y.count('possible')}</td><td>{best}</td></tr>")
        out.append("</table>")
        age = rep.age_hours()
        out.append(f'<div class="note">Yard stock from Row52 as of {html.escape((rep.synced_at or "never")[:16])}'
                   + (f" ({age:.0f} h old)" if age is not None else "")
                   + ". Likely = same model, year and engine as a car CHARM lists this number for. "
                     "Pull-your-own, no warranty: check the part number on the part before you pay.</div>")
    out.append("</div>")
    return "\n".join(out)


def render(results, car_label="", case_label=""):
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    body = "\n".join(part_block(s) for s in results)
    head = html.escape(" · ".join(x for x in (car_label, case_label) if x))
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>Parts sourcing</title>'
            f"<style>{CSS}</style></head><body><h1>Parts sourcing</h1>"
            f'<div class="sub">{head} &middot; printed {now} &middot; scan a code for the live price</div>'
            f"{body}</body></html>")
