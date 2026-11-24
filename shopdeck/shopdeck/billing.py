"""The customer's paperwork for a ticket: a written estimate to approve, an invoice, and a paid receipt.
One printable HTML page each, plus a plain-text version for the body of an email or a text message.
Every amount comes from repair.quote; nothing here does its own sums except paid and balance."""
import html
import os
import time

KINDS = ("estimate", "invoice", "receipt")
TITLE = {"estimate": "Estimate", "invoice": "Invoice", "receipt": "Receipt"}
METHODS = ("Cash", "Card", "Venmo", "Zelle", "Cash App", "Check")

ESTIMATE_TERMS = ("This is a written estimate of the parts and labor for the work above. I won't charge more than "
                  "10% over this total without your OK first, and any change you approve is written down here with "
                  "the time and how you approved it. Your replaced parts are yours to keep if you ask before the "
                  "work starts.")
WARRANTY = ("Labor guaranteed 90 days / 3,000 miles. Parts carry the manufacturer's warranty. Customer-supplied "
            "parts are not covered.")


def _e(x):
    return html.escape(str(x if x is not None else ""))


def money(x):
    return f"${x:,.2f}"


def paid(case):
    return round(sum(p.get("amount", 0) for p in case.payments), 2)


def balance(case, q):
    return round(max(0.0, q.total - paid(case)), 2)


def number(cfg, now=None):
    """The next document number, year-sequence ('2026-007'). Advances cfg['next_no']; the caller saves cfg."""
    n = int(cfg.get("next_no", 1))
    cfg["next_no"] = n + 1
    return f"{time.strftime('%Y', time.localtime(now))}-{n:03d}"


def _header(biz):
    lines = [x for x in (biz.get("area"), biz.get("phone"), biz.get("email")) if x]
    return biz.get("name") or "Mobile mechanic", lines


def text(case, q, kind, biz):
    """Plain text: email body or a text message."""
    name, contact = _header(biz)
    out = [name] + contact + ["", f"{TITLE[kind].upper()} {case.doc_no}  ·  {time.strftime('%Y-%m-%d')}"]
    if case.customer:
        out.append(f"Customer: {case.customer}")
    if case.model:
        out.append(f"Vehicle: {case.car}" + (f" · {case.mileage} mi" if case.mileage else ""))
    if case.vin:
        out.append(f"VIN: {case.vin}")
    for k, line, amt in q.lines:
        if k == "job":
            out += ["", line]
        elif k == "fee":
            continue
        else:
            out.append(f"  {line}: " + (money(amt) if amt is not None else "price to come"))
    out.append("")
    if q.supplies:
        out.append(f"Shop supplies: {money(q.supplies)}")
    for k, line, amt in q.lines:
        if k == "fee":
            out.append(f"{line}: {money(amt)}")
    if q.tax:
        out.append(f"Sales tax ({q.taxed}): {money(q.tax)}")
    out.append(f"TOTAL: {money(q.total)}")
    if kind == "estimate":
        out += ["", ESTIMATE_TERMS, "", "Reply to approve, or tell me what you'd like changed."]
    else:
        for p in case.payments:
            out.append(f"Paid {money(p['amount'])} by {p['method']}" + (f" ({p['ref']})" if p.get("ref") else "")
                       + f" on {p['date']}")
        out.append(("Balance due: " if balance(case, q) else "Balance: ") + money(balance(case, q)))
        out += ["", biz.get("warranty") or WARRANTY]
    out += ["", "Thank you!"]
    return "\n".join(out)


CSS = ("body{font:14px/1.45 system-ui,sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#111}"
       ".head{display:flex;justify-content:space-between;gap:16px;border-bottom:3px solid #111;padding-bottom:10px}"
       "h1{font-size:24px;margin:0;text-transform:uppercase;letter-spacing:.02em}.kind{font-size:26px;font-weight:700;"
       "letter-spacing:.08em;color:#b4470d;text-transform:uppercase;text-align:right}h2{font-size:13px;letter-spacing:.1em;"
       "text-transform:uppercase;color:#555;margin:22px 0 6px}table{border-collapse:collapse;width:100%}"
       "td,th{border-bottom:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}"
       ".num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.muted{color:#555}"
       ".warn{color:#a40}.tot th,.tot td{border:0}.grand th{border-top:3px solid #111;font-size:17px}"
       ".paid{display:inline-block;border:4px solid #1f7a4a;color:#1f7a4a;font:700 34px sans-serif;"
       "letter-spacing:.12em;padding:2px 14px;transform:rotate(-8deg);margin:8px 0}"
       ".box{border:1px solid #999;padding:10px 14px;margin-top:14px}.sign{display:flex;gap:28px;margin-top:34px}"
       ".sign div{flex:1;border-top:1px solid #111;padding-top:4px;font-size:12px;color:#555}"
       "@media print{.noprint{display:none}}")


def document(case, q, kind, biz, out_dir):
    """Write the estimate, invoice or receipt for this ticket and return its path."""
    name, contact = _header(biz)
    h = [f"<!doctype html><meta charset='utf-8'><title>{TITLE[kind]} {_e(case.doc_no)}</title><style>{CSS}</style>"]
    add = h.append
    add(f"<div class=head><div><h1>{_e(name)}</h1><div class=muted>{'<br>'.join(_e(x) for x in contact)}</div></div>"
        f"<div><div class=kind>{TITLE[kind]}</div><div class=num>No. {_e(case.doc_no)}<br>"
        f"{time.strftime('%Y-%m-%d')}</div></div></div>")
    add("<h2>Customer &amp; vehicle</h2><p>" + "<br>".join(_e(x) for x in (
        case.customer, case.customer_email, case.car if case.model else "",
        (f"VIN {case.vin}" if case.vin else "") + (f" · {case.mileage} mi" if case.mileage else "")) if x) + "</p>")
    complaint = case.interview.get("complaint")
    if complaint:
        add(f"<h2>Concern</h2><p>{_e(complaint)}</p>")
    add("<h2>Work</h2><table><tr><th>Item</th><th class=num>Amount</th></tr>")
    for k, line, amt in q.lines:
        if k == "job":
            add(f"<tr><th colspan=2>{_e(line)}</th></tr>")
        elif k != "fee":
            add(f"<tr><td>{_e(line)}</td><td class=num>"
                + (money(amt) if amt is not None else "<span class=warn>price to come</span>") + "</td></tr>")
    add("</table><table class=tot>")
    rows = [("Labor", q.labor), ("Parts", q.parts)]
    if q.supplies:
        rows.append(("Shop supplies", q.supplies))
    rows += [(line, amt) for k, line, amt in q.lines if k == "fee"]
    if q.tax:
        rows.append((f"Sales tax ({q.taxed})", q.tax))
    add("".join(f"<tr><td>{_e(a)}</td><td class=num>{money(b)}</td></tr>" for a, b in rows))
    add(f"<tr class=grand><th>Total</th><th class=num>{money(q.total)}</th></tr>")
    if kind != "estimate":
        for p in case.payments:
            add(f"<tr><td>Paid by {_e(p['method'])}" + (f" ({_e(p['ref'])})" if p.get("ref") else "")
                + f" on {_e(p['date'])}</td><td class=num>−{money(p['amount'])}</td></tr>")
        due = balance(case, q)
        add(f"<tr class=grand><th>{'Balance due' if due else 'Balance'}</th><th class=num>{money(due)}</th></tr>")
    add("</table>")
    if q.missing:
        add("<p class=warn>Not in the total until priced: " + _e("; ".join(q.missing)) + "</p>")
    if kind == "receipt" and not balance(case, q):
        add("<div class=paid>PAID</div>")
    if kind == "estimate":
        a = case.approval
        add(f"<div class=box><b>Customer authorization</b><p>{_e(ESTIMATE_TERMS)}</p>")
        add(f"<p>☐ I authorize the work above, up to <b>{money(a.get('cap') or q.total)}</b>.<br>"
            "☐ Return my replaced parts to me.<br>☐ I don't want a written estimate (waiver).</p>")
        if a.get("by"):
            add(f"<p><b>Approved</b> by {_e(a['by'])} ({_e(a.get('how', ''))}) on {_e(a.get('at', ''))}"
                + (", replaced parts to be returned" if a.get("return_parts") else "") + ".</p>")
        if case.changes:
            add("<p><b>Changes approved</b></p><ul>" + "".join(
                f"<li>{_e(c['at'])}: {_e(c['text'])} ({money(c['amount'])}, {_e(c['how'])})</li>" for c in case.changes)
                + "</ul>")
        add("</div><div class=sign><div>Customer signature &amp; date</div><div>Mechanic</div></div>")
    else:
        add(f"<h2>Warranty</h2><p>{_e(biz.get('warranty') or WARRANTY)}</p>")
        add("<div class=sign><div>Customer signature (accepts the work and the vehicle)</div><div>Mechanic</div></div>")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{kind}-{case.doc_no or case.id}.html")
    with open(path, "w") as f:
        f.write("\n".join(h))
    return path
