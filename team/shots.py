"""Chat screenshots of Team runs: every member's message in order, as the team saw them, one PNG per run
(split into parts when very tall). Rendered offscreen with Qt in the deck's colours (app/theme.py).

    venv/bin/python team/shots.py logs/team/replay/<run> [...]      -> <run>/chat.png (chat_2.png ...)
    venv/bin/python team/shots.py --all logs/team/replay              every run folder under it
    venv/bin/python team/shots.py --card <run> [...]                  public-safe summary card -> <run>/card.png
"""
import glob
import html
import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

from PyQt6.QtCore import QRectF, QSizeF  # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter, QTextDocument  # noqa: E402

import theme as T  # noqa: E402

WIDTH, MAX_H, MAX_CHARS = 1200, 14000, 7000
WHO = {"gemini": ("GEMINI (agy)", T.PURPLE_TEXT), "gptoss": ("GPT-OSS 120B (agy)", T.AMBER),
       "local": ("LOCAL QWEN (Ollama, with tools)", T.OK), "baseline": ("HOME BASELINE (Ollama, no tools)", T.MUTED),
       "claude": ("CLAUDE (lead, verdict)", T.FOREST_TEXT)}


def _msgs(run):
    out, stage = [], {}
    case = {}
    for d in map(json.loads, open(os.path.join(run, "trace.jsonl"))):
        ev = d["event"]
        if ev == "case":
            case = d
        elif ev in ("agy", "local", "baseline", "claude") and (d.get("reply") or d.get("error") or d.get("status")):
            who = d.get("who") or ("baseline" if ev == "baseline" else ev)
            n = stage[who] = stage.get(who, 0) + 1
            kind = {"claude": "verdict", "baseline": "Home verdict"}.get(who, "diagnosis" if n == 1 else "cross-check")
            u = d.get("usage") or {}
            tok = sum(v for k, v in u.items() if isinstance(v, int) and k in (
                "input_tokens", "output_tokens", "prompt_eval_count", "eval_count"))
            meta = f"{kind} · {d.get('seconds', '?')} s" + (f" · {tok:,} tokens" if tok else "") + \
                (f" · {d['tool_calls']} tool calls" if d.get("tool_calls") is not None else "")
            text = d.get("reply") or f"(no answer: {d.get('status') or ''} {(d.get('error') or '')[:300]})"
            out.append((who, meta, text))
    v = os.path.join(run, "verdict.txt")
    if os.path.isfile(v) and out and out[-1][0] == "claude":    # the shown verdict: ids marked, lookups listed
        out[-1] = (out[-1][0], out[-1][1], open(v).read())
    return case, out


def _html(run, case, msgs):
    rep = {}
    if os.path.isfile(os.path.join(run, "report.json")):
        rep = json.load(open(os.path.join(run, "report.json")))
    head = (f"<h2 style='color:{T.TEXT}'>Team run · {html.escape(os.path.basename(run))}</h2>"
            f"<p style='color:{T.MUTED}'>web {'on' if rep.get('web') else 'off'} · DB text for members "
            f"{'on' if rep.get('cloud_manual') else 'off'} · {rep.get('seconds', '?')} s · source-check flags: team "
            f"{(rep.get('verdict') or {}).get('source_check', '-')}, home "
            f"{(rep.get('baseline') or {}).get('source_check', '-')}</p>"
            f"<table width='100%' cellpadding='10' style='background:{T.PANEL}'><tr><td>"
            f"<b style='color:{T.TEXT}'>CASE</b><pre style='color:{T.TEXT};white-space:pre-wrap'>"
            f"{html.escape((case.get('summary') or '')[:3000])}</pre></td></tr></table><br>")
    body = []
    for who, meta, text in msgs:
        name, col = WHO.get(who, (who.upper(), T.TEXT))
        t = text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n[... cut here; full text in trace.jsonl]"
        body.append(f"<table width='100%' cellpadding='10' style='background:{T.PANEL}'><tr><td>"
                    f"<b style='color:{col}'>{html.escape(name)}</b> <span style='color:{T.MUTED}'>"
                    f"{html.escape(meta)}</span>"
                    f"<pre style='color:{T.TEXT};white-space:pre-wrap;font-size:13px'>{html.escape(t)}</pre>"
                    "</td></tr></table><br>")
    return (f"<body style='background:{T.BG};font-family:{T.SANS}'>" + head + "".join(body) + "</body>")


def render(run):
    case, msgs = _msgs(run)
    doc = QTextDocument()
    doc.setDocumentMargin(18)
    doc.setHtml(_html(run, case, msgs))
    doc.setTextWidth(WIDTH)
    h = int(doc.size().height()) + 1
    paths = []
    for part, top in enumerate(range(0, h, MAX_H), 1):
        ph = min(MAX_H, h - top)
        img = QImage(WIDTH, ph, QImage.Format.Format_RGB32)
        img.fill(QColor(T.BG))
        p = QPainter(img)
        p.translate(0, -top)
        doc.drawContents(p, QRectF(0, top, WIDTH, ph))
        p.end()
        path = os.path.join(run, "chat.png" if part == 1 else f"chat_{part}.png")
        img.save(path)
        paths.append(path)
    return paths


def card(run):
    """A public-safe summary of one run (no manual text, no case notes, no VIN): who did what, how long, how many
    lookups, the source check, and the verdict's issue titles. -> <run>/card.png"""
    case, msgs = _msgs(run)
    rep = json.load(open(os.path.join(run, "report.json"))) if os.path.isfile(os.path.join(run, "report.json")) else {}
    summ = case.get("summary") or ""
    car = next((ln.split(":", 1)[1].split("(")[0].strip() for ln in summ.splitlines() if ln.startswith("Vehicle:")), "")
    codes = next((ln.split(":", 1)[1].strip() for ln in summ.splitlines() if ln.startswith("DTCs:")), "")
    looks, used = {}, {}
    if os.path.isfile(os.path.join(run, "lookups.jsonl")):
        for d in map(json.loads, open(os.path.join(run, "lookups.jsonl"))):
            looks[d["who"]] = looks.get(d["who"], 0) + 1
    if os.path.isfile(os.path.join(run, "evidence.jsonl")):
        for it in map(json.loads, open(os.path.join(run, "evidence.jsonl"))):
            if it["kind"] in "MSCO":
                used[it["by"]] = used.get(it["by"], 0) + len(it["text"])
    rows = []
    for who, meta, _ in msgs:
        name, col = WHO.get(who, (who.upper(), T.TEXT))
        rows.append(f"<tr><td style='color:{col}'><b>{html.escape(name)}</b></td>"
                    f"<td style='color:{T.TEXT}'>{html.escape(meta)}</td></tr>")
    verdict = open(os.path.join(run, "verdict.txt")).read() if os.path.isfile(os.path.join(run, "verdict.txt")) else ""
    import re as _re
    issues = _re.findall(r"(?m)^=== ISSUE (\d+): (.+?) ===", verdict)
    v = rep.get("verdict") or {}
    look = ", ".join(f"{w} {n}" for w, n in sorted(looks.items()))
    db = ", ".join(f"{w} {n // 1000}k" for w, n in sorted(used.items()) if w not in ("claude", "runner"))
    doc_html = (f"<body style='background:{T.BG};font-family:{T.SANS}'>"
                f"<h2 style='color:{T.TEXT}'>Team run · {html.escape(car)} · {html.escape(codes)}</h2>"
                f"<p style='color:{T.MUTED}'>{rep.get('seconds', 0) / 60:.1f} min · web "
                f"{'on' if rep.get('web') else 'off'} · source-check flags: team {v.get('source_check', '-')}, "
                f"Home {(rep.get('baseline') or {}).get('source_check', '-')} · cited ids that don't exist: "
                f"{len(v.get('bad_ids') or [])}</p>"
                f"<table cellpadding='6' width='100%' style='background:{T.PANEL}'>{''.join(rows)}</table>"
                f"<p style='color:{T.MUTED}'>Database lookups: {html.escape(look or 'none')}"
                + (f" · manual/DB text per member: {html.escape(db)} (cap 40k)" if db else "") + "</p>"
                f"<h3 style='color:{T.FOREST_TEXT}'>Verdict (Claude, lead)</h3><ol style='color:{T.TEXT}'>"
                + "".join(f"<li>{html.escape(t)}</li>" for _, t in issues) + "</ol>"
                f"<p style='color:{T.MUTED}'>Every claim in the verdict cites an evidence id (manual page, card, owner "
                "thread, web page or video). Manual text isn't shown here.</p></body>")
    doc = QTextDocument()
    doc.setDocumentMargin(18)
    doc.setHtml(doc_html)
    doc.setTextWidth(1100)
    h = int(doc.size().height()) + 1
    img = QImage(1100, h, QImage.Format.Format_RGB32)
    img.fill(QColor(T.BG))
    p = QPainter(img)
    doc.drawContents(p, QRectF(0, 0, 1100, h))
    p.end()
    path = os.path.join(run, "card.png")
    img.save(path)
    return path


def main():
    app = QGuiApplication(sys.argv[:1])  # noqa: F841
    args = sys.argv[1:]
    if args[:1] == ["--card"]:
        for r in args[1:]:
            print(r, card(r))
        return
    runs = (sorted(d for d in glob.glob(os.path.join(args[1], "*")) if os.path.isfile(os.path.join(d, "trace.jsonl")))
            if args[:1] == ["--all"] else args)
    for r in runs:
        print(r, render(r))


if __name__ == "__main__":
    main()
