"""The work order: one printable HTML page per ticket, with every fact's source next to it."""
import html
import os
import re
import time

from .case import INTERVIEW
from .repair import quote, torque_lines, must_be_new


def _e(x):
    return html.escape(str(x if x is not None else ""))


def tidy(body):
    """Manual text for print: no markdown marks, no figure placeholders, no runs of blank lines."""
    out = []
    for line in body.splitlines():
        s = line.rstrip()
        if s.strip() in ("[fig]",) or s.startswith("(continued"):
            continue
        if s.lstrip().startswith("|"):
            if re.fullmatch(r"[|\-:\s]+", s):
                continue
            s = "  |  ".join(c.strip() for c in s.strip().strip("|").split("|") if c.strip())
        s = re.sub(r"^#+\s*", "", s)
        s = re.sub(r"^- (?=\d+\)|\()", "", s)
        s = re.sub(r"\s*\(S\d{6}[A-Z]\d{2}\)$", "", s)
        if not s.strip() and (not out or not out[-1].strip()):
            continue
        out.append(s)
    return "\n".join(out).strip()


def work_order(case, kb, charts, out_dir):
    q = quote(case.jobs, case.labor_rate, case.tax_rate, case.supplies_rate)
    h = []
    add = h.append
    add("<!doctype html><meta charset='utf-8'><title>Work order " + _e(case.id) + "</title>")
    add("<style>body{font:14px/1.45 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#111}"
        "h1{font-size:22px;margin:0}h2{font-size:17px;border-bottom:2px solid #333;margin-top:28px}"
        "h3{font-size:15px;margin:18px 0 4px}table{border-collapse:collapse;width:100%}"
        "td,th{border-bottom:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}"
        ".src{color:#555;font-size:12px}.num{text-align:right;white-space:nowrap}.warn{color:#a40}"
        "pre{white-space:pre-wrap;font:13px/1.4 ui-monospace,monospace;background:#f6f6f6;padding:8px}"
        "@media print{h2{break-after:avoid}pre{background:none}}</style>")
    add(f"<h1>Work order {_e(case.id)}</h1><p>{_e(case.customer)}<br><b>{_e(case.car)}</b>"
        + (f" &middot; VIN {_e(case.vin)}" if case.vin else "") + (f" &middot; {_e(case.mileage)} mi" if case.mileage else "")
        + f"<br><span class=src>Printed {time.strftime('%Y-%m-%d %H:%M')}</span></p>")

    asked = [(label, case.interview.get(k)) for k, label in INTERVIEW if case.interview.get(k)]
    if asked:
        add("<h2>Customer report</h2><table>" + "".join(f"<tr><th>{_e(l)}</th><td>{_e(v)}</td></tr>" for l, v in asked)
            + "</table>")

    add("<h2>Diagnosis</h2>")
    add(f"<p>Trouble codes: <b>{_e(', '.join(case.codes) or 'none')}</b>"
        + (f" &middot; pending {_e(', '.join(case.pending))}" if case.pending else "")
        + (f" <span class=src>({_e(case.code_source)})</span>" if case.code_source else "") + "</p>")
    for code, walk in case.walks.items():
        ch = charts.get(code)
        title = ch.title if ch else code
        add(f"<h3>{_e(title)}</h3><p class=src>{_e(ch.source if ch else '')}</p><table>")
        for a in walk.get("trail", []):
            st = ch.steps.get(a["step"]) if ch else None
            add(f"<tr><td>Step {a['step']}</td><td>{_e(st.title if st else '')}<br><span class=src>"
                f"{_e(st.question if st else '')}</span></td><td><b>{_e(a['answer'])}</b><br><span class=src>"
                f"{_e(a['how'])}{': ' + _e(a['detail']) if a.get('detail') else ''}</span></td></tr>")
        add("</table>")
        if walk.get("result"):
            add(f"<p><b>Manual's conclusion:</b> {_e(walk['result'])}</p>")
    mech = [n for n in case.notes if n.kind in ("hypothesis", "history", "reused")]
    if mech:
        add("<h3>Mechanic's notes</h3><ul>" + "".join(f"<li>{_e(n.kind)}: {_e(n.text)}</li>" for n in mech) + "</ul>")

    add("<h2>Quote</h2><table><tr><th>Item</th><th class=num>Amount</th></tr>")
    for kind, text, amt in q.lines:
        if kind == "job":
            add(f"<tr><th colspan=2>{_e(text)}</th></tr>")
            continue
        add(f"<tr><td>{_e(text)}</td><td class=num>{'$%.2f' % amt if amt is not None else '<span class=warn>price needed</span>'}</td></tr>")
    add(f"<tr><td>Labor</td><td class=num>${q.labor:,.2f}</td></tr><tr><td>Parts</td><td class=num>${q.parts:,.2f}</td></tr>")
    if q.tax:
        add(f"<tr><td>Tax on parts ({case.tax_rate * 100:g}%)</td><td class=num>${q.tax:,.2f}</td></tr>")
    if q.supplies:
        add(f"<tr><td>Shop supplies ({case.supplies_rate * 100:g}% of labor)</td><td class=num>${q.supplies:,.2f}</td></tr>")
    add(f"<tr><th>Total</th><th class=num>${q.total:,.2f}</th></tr></table>")
    if q.missing:
        add("<p class=warn>Not included until priced: " + _e("; ".join(q.missing)) + "</p>")
    add("<p class=src>Labor hours are CHARM book times unless marked otherwise. CHARM part prices are old list "
        "prices; check current prices before quoting.</p>")

    add("<h2>Parts list</h2><table><tr><th>Part</th><th>Part number</th><th>Qty</th><th>Source</th></tr>")
    for j in case.jobs:
        for p in j.parts:
            add(f"<tr><td>{_e(p.what)}</td><td>{_e(p.part_number)}</td><td>{p.qty:g}</td><td class=src>{_e(p.source)}</td></tr>")
    add("</table>")

    add("<h2>Repair instructions (factory manual)</h2>")
    for j in case.jobs:
        add(f"<h3>{_e(j.title)}</h3><p class=src>{_e(j.reason)}</p>")
        for sid in j.procedures:
            sec = kb.section(sid)
            if not sec:
                continue
            for part in kb.topic_sections(sid):
                tq = torque_lines(part)
                nw = must_be_new(part)
                add(f"<p><b>{_e(part.title)}</b> <span class=src>{_e(part.source)}</span></p>")
                if nw:
                    add("<p class=warn>Must be new: " + _e(" / ".join(nw)) + "</p>")
                if tq:
                    add("<table>" + "".join(f"<tr><td>{_e(w)}</td><td class=num>{_e(v)}</td></tr>" for w, v in tq)
                        + "</table>")
                add(f"<pre>{_e(tidy(part.body))}</pre>")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"workorder-{case.id}.html")
    with open(path, "w") as f:
        f.write("\n".join(h))
    return path
