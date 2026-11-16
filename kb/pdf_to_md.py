#!/usr/bin/env python3
"""
Re-convert factory service manual PDFs to the markdown build_subaru_kb.py reads, using the PDF's own text
and table lines (PyMuPDF) instead of marker's OCR/layout model.

Why: marker cut many diagnostic-chart cells off mid-word in the 2004+ manuals ("Measure voltage between
ECM and chas"), so their step charts can't be walked. These PDFs have a real text layer and ruled tables,
so reading them directly keeps every word and every cell in its column. No GPU, no OCR, no guessing.

Output, next to marker's output so load_manuals.sh picks it up (it prefers <name>.fixed.md):
  test_batch_out/<name>/<name>.fixed.md
An existing .fixed.md is kept as <name>.fixed.md.bak-<date> (never deleted).
Figures (raster images and vector drawings such as wiring diagrams) are saved as
  _page_<N>_Figure_<M>.jpeg    (distinct from marker's _Picture_ files; ship_images.py finds figures by the names in the text)

Run on the desktop (needs: pip install pymupdf):
  python3 kb/pdf_to_md.py --list                # which manuals, done or not
  python3 kb/pdf_to_md.py                       # all the damaged ones
  python3 kb/pdf_to_md.py --only 2009           # names containing "2009"
  python3 kb/pdf_to_md.py --only 2004 --pages 500-520 --out /tmp/try   # quick look
  python3 kb/pdf_to_md.py --no-images           # text only, faster
"""
import argparse
import bisect
import os
import re
import shutil
import sys
import time

import pymupdf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
PDF_DIR = os.path.join(ROOT, "manual_pdfs")
OUT_DIR = os.path.join(ROOT, "test_batch_out")

# The manuals whose marker conversion damaged the diagnostic charts (found 2026-10-19).
MANUALS = [
    "Forester_SG/USDM Forester FSM 2004 G8080BE_V14 (SG).pdf",
    "Impreza_GD/Subaru Impreza 2002.pdf",
    "Outback_BP/2004 Service Manual USDM.pdf",
    "Outback_BP/2005 Service Manual USDM.pdf",
    "Outback_BP/2008 Service Manual.pdf",
    "Outback_BP/2009 Service Manual USDM.pdf",
    # Two-column manuals whose step charts print CHECK / YES / NO as pictures (2026-10-21).
    "Forester_SF/USDM Forester FSM 1999 MSA5TCD99F (SF).pdf",
    "Forester_SF/USDM Forester FSM 2000 G8030BE_V1 (SF).pdf",
    "Forester_SF/USDM Forester FSM 2001 G8040BE_V1 (SF).pdf",
]

HEADER_BAND = 52        # running heads (chapter name, topic name) sit above this, in points
FOOTER_BAND = 48        # page id ("EN(H4SO)-98") sits within this of the bottom
FIGURE_FONT = 8.0       # lines whose largest font is smaller are figure labels (wiring diagrams, callouts)
PAGE_ID_RE = re.compile(r"^([A-Z]{1,4}(?:\([A-Za-z0-9 ]+\)){0,2})\s*-\s*\d+$")   # EN(H4SO)-98, EN(H4SO)(diag)-158, ME-87
# The 1999-2001 Subaru FSM step-chart icons: the CHECK / YES / NO marks in front of a step's question and its two
# answers are small pictures, so the text layer has only ': ...'. Matched by image digest (first 8 hex digits),
# never by xref: 1999 puts YES and NO inline, with no xref.
ICON_LABELS = {"e9b070c3": "CHECK", "bab6e7e6": "YES", "2e7a1dbc": "NO"}
STEP_LABEL_RE = re.compile(r"^(?:#+\s*)?(\d{1,2}[A-Z]{1,3})(\d{1,2})\s*:\s*(.*)$")     # '10AC1 :', '1B2 : CHECK ...'
CHART_LABEL_RE = re.compile(r"^(CHECK|YES|NO): (.*)$")
NEW_ITEM_RE = re.compile(r"^(\d{1,2}\)|\(\d{1,2}\)|[•·▪●]|NOTE|CAUTION|WARNING|Connector|Terminal|"
                         r"[A-Z]\s*:|\*\d|#\d|\(\w{1,4}\)\s*No\.|[A-Z][A-Z ]{3,}:$)")


# ---------------------------------------------------------------------------------------------- text helpers
def join_lines(lines):
    """Wrapped lines -> logical lines. A line continues the previous one unless it starts a new item
    (numbered step, bullet, NOTE, connector line), the previous one ended a sentence, or the font changed
    between bold and regular (a step's bold title vs its instructions). Lines are str or (text, bold)."""
    out = []
    last_bold = None
    for item in lines:
        ln, bold = item if isinstance(item, tuple) else (item, None)
        ln = ln.strip()
        if not ln:
            continue
        prev = out[-1] if out else ""
        open_ref = prev.count("<") > prev.count(">")          # '<Ref.' wrapped onto the next line
        same_font = bold is None or last_bold is None or bold == last_bold
        if out and (open_ref or (same_font and not NEW_ITEM_RE.match(ln) and not re.search(r"[.:?!]$", prev))):
            if prev.endswith("-") and re.match(r"[a-zA-Z]", ln) and not re.search(r"\s-$", prev):
                out[-1] = prev[:-1] + ln                    # "chas-" + "sis ground" -> "chassis ground"
            else:
                out[-1] = prev + " " + ln
        else:
            out.append(ln)
        last_bold = bold
    return out


def cell_text(page, rect):
    """A table cell's text in reading order, wrapped lines joined, items separated by '; '."""
    if rect is None:
        return ""
    lines = []
    for b in page.get_text("dict", clip=pymupdf.Rect(rect), sort=True)["blocks"]:
        for line in b.get("lines", []):
            spans = [sp for sp in line["spans"] if sp["text"].strip()]
            if not spans:
                continue
            text = re.sub(r"\s+", " ", "".join(span_text(sp) for sp in line["spans"])).strip()
            bold = all("Bold" in sp["font"] or sp["flags"] & 16 for sp in spans)
            lines.append((text, bold))
    return "; ".join(join_lines(lines)).replace("|", "/")


def span_text(span):
    """A span's text. Some manuals set Ω in a Symbol font whose encoding comes out as ':' ('less than 1 :?')."""
    t = span["text"]
    if "Symbol" in span["font"]:
        t = t.replace(":", "Ω")
    return t


def md_cell(s):
    return s.replace("\n", "; ").replace("|", "/").strip()


# ---------------------------------------------------------------------------------------------- tables
STEP_HEAD = ("step", "check", "yes", "no")


def table_md(page, tab, state):
    """Markdown rows for one ruled table. Diagnostic step charts come out as the
    '| No. | Step | Check | Yes | No |' table the app walks; other tables keep their columns."""
    rows = tab.rows
    if not rows:
        return []
    texts = [[cell_text(page, c) for c in r.cells] for r in rows]
    head = [t.lower().strip(" .") for t in texts[0]]
    is_step = (len(head) >= 4 and [h.split()[-1] if h else "" for h in head[-3:]] == ["check", "yes", "no"]) \
        or (state.get("in_steps") and len(texts[0]) == state.get("step_cols"))
    out = []
    if is_step:
        body = texts[1:] if head[-3:] and head[-1] == "no" and head[-2] == "yes" else texts
        if not state.get("in_steps") or head[-1] == "no":
            out += ["| No. | Step | Check | Yes | No |", "|---|---|---|---|---|"]
        state["in_steps"], state["step_cols"] = True, len(texts[0])
        for cells in body:
            cells = cells + [""] * (5 - len(cells)) if len(cells) < 4 else cells
            if len(cells) == 4:                     # number and step share the first column
                m = re.match(r"^(\d{1,2})(?:;\s*|\s+)(.*)$", cells[0], re.S)
                num, step = (m.group(1), m.group(2)) if m else ("", cells[0])
                cells = [num, step] + cells[1:]
            num, step, check, yes, no = (md_cell(c) for c in cells[:5])
            out.append(f"| {num} | {step} | {check} | {yes} | {no} |")
        return out
    state["in_steps"] = False
    ncol = max(len(r) for r in texts)
    for i, cells in enumerate(texts):
        cells = [md_cell(c) for c in cells] + [""] * (ncol - len(cells))
        out.append("| " + " | ".join(cells) + " |")
        if i == 0:
            out.append("|" + "---|" * ncol)
    return out


# ---------------------------------------------------------------------------------------------- headings
def classify_line(spans, text):
    """Markdown heading level for a line from its font, or 0 for body text."""
    size = max(s["size"] for s in spans)
    bold = all(("Bold" in s["font"] or s["flags"] & 16) for s in spans if s["text"].strip())
    if not bold:
        return 0
    if size >= 15.5 and re.match(r"^\d{1,2}\.", text):
        return 2                                   # "12.Inspection Mode"  (topic)
    if size >= 13.5 and re.match(r"^[A-Z]{1,2}\s*:", text):
        return 3                                   # "A: OPERATION", "G: DTC P0066 — ..."  (procedure)
    if size >= 13.5:
        return 2
    if size >= 10.5 and text.endswith(":") and len(text) < 60:
        return 4                                   # "CAUTION:", "DTC DETECTING CONDITION:"
    return 0


# ---------------------------------------------------------------------------------------------- figures
def figure_rects(page, tables, icons=()):
    """Areas that are pictures: raster images and clusters of vector drawing (wiring diagrams), minus tables
    and the step-chart icons."""
    rects = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"])
        if any(abs(r & ic) >= 0.5 * abs(r) for _, ic in icons):
            continue
        if r.width > 30 and r.height > 30:
            rects.append(r)
    try:
        clusters = page.cluster_drawings()
    except Exception:
        clusters = []
    for r in clusters:
        r = pymupdf.Rect(r)
        if r.width < 60 or r.height < 40 or r.y1 < HEADER_BAND or r.y0 > page.rect.height - FOOTER_BAND:
            continue
        if any(pymupdf.Rect(t.bbox).intersects(r) and pymupdf.Rect(t.bbox).contains(r.tl) for t in tables):
            continue                               # a table's own ruling lines
        if any(abs(pymupdf.Rect(t.bbox) & r) > 0.6 * abs(r) for t in tables):
            continue
        if rules_only(page, r):                    # ruled rows find_tables didn't take: text, not a picture
            continue
        rects.append(r)
    merged = []
    for r in sorted(rects, key=lambda r: (r.y0, r.x0)):
        for m in merged:
            if m.intersects(r):
                m |= r
                break
        else:
            merged.append(pymupdf.Rect(r))
    return merged


RULED_TEXT_WORDS = 30


def rules_only(page, r):
    """A drawing cluster made only of straight lines / hairline boxes that holds a lot of text is the ruling of a
    table (rows drawn, columns not: find_tables finds no table). Saving it as a picture dropped its text: the step
    rows of whole diagnostic charts (2002 Forester FSM, P0442 steps 2-6). Diagrams have curves, fills or little text."""
    for dr in page.get_drawings():
        dr_r = pymupdf.Rect(dr["rect"])
        if not (dr_r & r) or abs(dr_r & r) < 0.5 * abs(dr_r) and not dr_r.is_empty:
            continue
        if dr.get("fill") is not None and not (dr_r.width < 2.5 or dr_r.height < 2.5):
            return False
        for it in dr["items"]:
            if it[0] == "l":
                continue
            if it[0] == "re" and (it[1].width < 2.5 or it[1].height < 2.5):
                continue
            return False
    words = [w for w in page.get_text("words", clip=r)]
    return len(words) >= RULED_TEXT_WORDS


class _Row:
    def __init__(self, cells):
        self.cells = cells


class RuledTable:
    """A table whose rows are ruled but whose columns are not (find_tables needs both). Rows come from the
    full-width rules, columns from the vertical white gaps no word crosses. Same .bbox/.rows shape as a PyMuPDF
    table, so table_md (and its step-chart rule) handles it like any other table."""

    def __init__(self, bbox, rows):
        self.bbox, self.rows = bbox, rows


def ruled_table(page, r, min_gap=4):
    H = page.rect.height
    ys = set()
    for dr in page.get_drawings():
        for it in dr["items"]:
            if it[0] == "l" and abs(it[1].y - it[2].y) < 1:
                x0, x1, y = min(it[1].x, it[2].x), max(it[1].x, it[2].x), it[1].y
            elif it[0] == "re" and it[1].height < 2.5:
                x0, x1, y = it[1].x0, it[1].x1, it[1].y0
            else:
                continue
            if HEADER_BAND < y < H - FOOTER_BAND and min(x1, r.x1) - max(x0, r.x0) > 0.5 * r.width:
                ys.add(round(y, 1))
    ys = sorted(ys)
    if len(ys) < 3:
        return None
    area = pymupdf.Rect(r.x0, ys[0], r.x1, ys[-1])
    words = page.get_text("words", clip=area)
    if not words:
        return None
    x0, x1 = int(area.x0), int(area.x1) + 1
    cov = [0] * (x1 - x0 + 1)
    for w in words:
        for x in range(max(int(w[0]), x0), min(int(w[2]), x1) + 1):
            cov[x - x0] += 1
    bounds, run = [], 0
    for i, c in enumerate(cov):
        if c == 0:
            run += 1
            continue
        if run >= min_gap and i - run > 0:
            bounds.append(x0 + i - run / 2)
        run = 0
    cols = [area.x0] + bounds + [area.x1]
    # a gap inside one cell (an indented line) makes a column no word starts in: merge it into its left neighbour
    body = [w for w in words if (w[1] + w[3]) / 2 >= ys[1]] or words    # the header row may sit off-grid
    starts = [w[0] for w in body]
    cols = [c for i, c in enumerate(cols) if i in (0, len(cols) - 1)
            or any(c <= x < cols[i + 1] for x in starts)]
    if len(cols) < 3:
        return None
    rows = []
    for ya, yb in zip(ys, ys[1:]):
        if any(ya <= (w[1] + w[3]) / 2 < yb for w in words):
            rows.append(_Row([pymupdf.Rect(ca, ya, cb, yb) for ca, cb in zip(cols, cols[1:])]))
    return RuledTable(tuple(area), rows) if len(rows) >= 2 else None


def chart_icons(page):
    """[(label, rect)] of the CHECK / YES / NO pictures on a page (ICON_LABELS)."""
    out = []
    for info in page.get_image_info(hashes=True):
        label = ICON_LABELS.get(info.get("digest", b"").hex()[:8])
        if label:
            out.append((label, pymupdf.Rect(info["bbox"])))
    return out


def icon_label(icons, bbox, text):
    """CHECK / YES / NO when a ': ...' line has its icon just left of it: tops within 3 pt, or the icon's middle
    inside the line (1999 sets taller line boxes, so its icons sit ~4 pt below the line top)."""
    if not text.startswith(":"):
        return None
    x0, y0, y1 = bbox[0], bbox[1], bbox[3]
    for label, r in icons:
        level = abs(r.y0 - y0) <= 3 or y0 - 1 <= (r.y0 + r.y1) / 2 <= y1 + 1
        if level and r.x1 <= x0 + 2 and x0 - r.x1 < 20:
            return label
    return None


def two_column(boxes, width):
    """True when enough lines sit wholly right of the middle and few cross it (older two-column manuals)."""
    mid = width / 2
    right = sum(1 for b in boxes if b[0] >= mid - 5)
    cross = sum(1 for b in boxes if b[0] < mid - 20 and b[2] > mid + 20)
    return right >= 5 and cross * 3 <= right


def column_order(items, width):
    """Two-column reading order: items that cross the middle split the page into bands; inside a band the
    left column is read before the right one."""
    mid = width / 2
    spans = sorted(it[0] for it in items if it[1] < mid - 20 and it[4] > mid + 20)

    def key(it):
        crossing = it[1] < mid - 20 and it[4] > mid + 20
        band = bisect.bisect_right(spans, it[0])
        col = -1 if crossing else (0 if it[1] < mid - 5 else 1)
        return band, col, round(it[0]), it[1]
    return sorted(items, key=key)


def inside(rect, box, frac=0.6):
    rect = pymupdf.Rect(rect)
    inter = rect & box
    return abs(rect) > 0 and abs(inter) >= frac * abs(rect)


# ---------------------------------------------------------------------------------------------- one page
def page_md(doc, pno, out_dir, images, state):
    page = doc[pno]
    H = page.rect.height
    items = []                                     # (y, x, kind, payload, x1)
    try:
        tables = [t for t in page.find_tables().tables if t.row_count >= 1 and t.col_count >= 2]
    except Exception:
        tables = []
    if not tables:                                 # rows ruled, columns not: read it as a table, not a picture
        try:
            for r in page.cluster_drawings():
                r = pymupdf.Rect(r)
                if r.width >= 60 and r.height >= 40 and rules_only(page, r):
                    rt = ruled_table(page, r)
                    if rt and not any(pymupdf.Rect(t.bbox).intersects(pymupdf.Rect(rt.bbox)) for t in tables):
                        tables.append(rt)
        except Exception:
            pass
    icons = chart_icons(page)
    if icons:
        state["icon_charts"] = True
    figs = figure_rects(page, tables, icons)
    for t in tables:
        items.append((t.bbox[1], t.bbox[0], "table", t, t.bbox[2]))
    for k, r in enumerate(figs, 1):
        name = f"_page_{pno}_Figure_{k}.jpeg"
        if images:
            try:
                page.get_pixmap(clip=r, dpi=110).save(os.path.join(out_dir, name), jpg_quality=70)
                items.append((r.y0, r.x0, "fig", name, r.x1))
            except Exception:
                items.append((r.y0, r.x0, "fig", None, r.x1))
        else:
            items.append((r.y0, r.x0, "fig", None, r.x1))

    page_id = None
    for b in page.get_text("dict", sort=True)["blocks"]:
        for line in b.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = re.sub(r"\s+", " ", "".join(span_text(s) for s in line["spans"])).strip()
            text = re.sub(r"^([A-Z]{1,2}):(?=\S)", r"\1: ", text)      # 'AT:DTC P0301' -> 'AT: DTC P0301'
            x0, y0, x1, y1 = line["bbox"]
            if y1 <= HEADER_BAND:
                continue                           # running heads: repeated on every page
            if y0 >= H - FOOTER_BAND:
                if PAGE_ID_RE.match(text):
                    page_id = text
                continue
            if any(inside(line["bbox"], pymupdf.Rect(t.bbox)) for t in tables):
                continue
            if any(inside(line["bbox"], f) for f in figs) or max(s["size"] for s in spans) < FIGURE_FONT:
                continue                           # labels inside a picture
            label = icon_label(icons, line["bbox"], text) if icons else None
            if label:
                items.append((y0, x0, "label", f"{label}: {text[1:].strip()}", x1))
            elif icons and STEP_LABEL_RE.match(text):
                items.append((y0, x0, "label", text, x1))     # '10AC1 :' starts a step, not a heading
            else:
                items.append((y0, x0, "text", (classify_line(spans, text), text), x1))

    items.sort(key=lambda it: (round(it[0]), it[1]))
    W = page.rect.width
    if two_column([(it[1], it[0], it[4]) for it in items if it[2] in ("text", "label")], W):
        items = column_order(items, W)
    lines = [f'<span id="page-{pno}-0"></span>']
    chap = PAGE_ID_RE.match(page_id).group(1) if page_id else None
    if chap and chap != state.get("chapter"):
        state["chapter"] = chap
        name = state.get("chapter_names", {}).get(pno, "")
        lines.append(f"# {chap} {name}".rstrip())
    para, last_head = [], {}

    def flush():
        nonlocal para
        if para:
            lines.extend(join_lines(para))
            lines.append("")
            para = []

    for y, x, kind, payload, _ in items:
        if kind == "label":                        # a step label or an icon-marked line: always its own line
            flush()
            last_head.clear()
            para.append(payload)
            continue
        if kind in ("table", "fig"):
            last_head.clear()
        if kind == "table":
            flush()
            lines += table_md(page, payload, state) + [""]
            continue
        state["in_steps"] = False if kind == "text" and payload[0] in (2, 3) else state.get("in_steps")
        if kind == "fig":
            flush()
            lines += [f"![]({payload})" if payload else "[fig]", ""]
            continue
        level, text = payload
        if level:
            flush()
            prev_head = last_head.get("i")
            if (prev_head is not None and level in (2, 3) and last_head["level"] == 3 and y - last_head["y"] < 24
                    and not re.match(r"^\d{1,2}\.|^[A-Z]{1,2}\s*:", text)):
                h = lines[prev_head]               # a procedure title wrapped onto a second line
                lines[prev_head] = (h[:-1] + text) if h.endswith("-") and not h.endswith(" -") else h + " " + text
                last_head["y"] = y
                continue
            lines += ["#" * level + " " + text, ""]
            last_head.update(i=len(lines) - 2, level=level, y=y)
        else:
            para.append(text)
            last_head.clear()
    flush()
    return lines


DTC_HEAD_RE = re.compile(r"^###\s+[A-Z]{1,2}:\s*DTC\s+([PBCU][0-9A-F]\d{3})\b")


def link_stacked_dtcs(lines):
    """Several DTC headings printed one under the other share the chart that follows the last one
    (P0301..P0304 above one misfire chart). Give each of the others the manual's own wording for that:
    'For the diagnostic procedure, refer to DTC P0304.' so the app and the builder link them."""
    heads = [(i, DTC_HEAD_RE.match(l).group(1)) for i, l in enumerate(lines) if DTC_HEAD_RE.match(l)]
    out, k = list(lines), 0
    for n, (i, code) in enumerate(heads[:-1]):
        j, nxt = heads[n + 1]
        between = [l for l in lines[i + 1:j] if l.strip() and not l.startswith("<span id=\"page-")]
        if between:
            continue
        last = nxt                                  # follow the stack to its last heading
        for m in range(n + 1, len(heads) - 1):
            a, b = heads[m], heads[m + 1]
            if [l for l in lines[a[0] + 1:b[0]] if l.strip() and not l.startswith("<span id=\"page-")]:
                break
            last = b[1]
        out.insert(i + 1 + k, f"For the diagnostic procedure, refer to DTC {last}.")
        k += 1
    return out


def chart_tables(lines):
    """Steps of the older layout ('10AC1 :' then CHECK: / YES: / NO: lines from the icons) -> the same
    '| No. | Step | Check | Yes | No |' table the 2002+ manuals give, one header per chart. Charts run across
    pages, so this works on the whole document. A step without exactly one CHECK, one YES and one NO stays as
    its plain lines (never guessed). Page markers and figures met inside a chart follow its table."""
    out, chart = [], None

    def end():
        nonlocal chart
        if not chart:
            return
        prefix, rows, broken = chart["prefix"], [], []

        def local(text):
            def sub(m):
                return f"step {int(m.group(2))}" if m.group(1) == prefix else f"chart step {m.group(1)}{m.group(2)}"
            return re.sub(r"step\s*(\d{1,2}[A-Z]{1,3})(\d{1,2})", sub, text)

        for st in chart["steps"]:
            f = st["f"]
            if [len(f["CHECK"]), len(f["YES"]), len(f["NO"])] != [1, 1, 1]:
                broken += st["raw"]
                continue
            cell = lambda parts: md_cell(local("; ".join(join_lines(parts))))
            rows.append(f"| {st['n']} | {cell(f['STEP'])} | {cell(f['CHECK'][0])} | {cell(f['YES'][0])} | "
                        f"{cell(f['NO'][0])} |")
        if rows:
            out.extend(["| No. | Step | Check | Yes | No |", "|---|---|---|---|---|"] + rows + [""])
        if broken:
            out.extend(broken + [""])
        out.extend(chart["after"])
        chart = None

    for line in lines:
        s = line.strip()
        m = STEP_LABEL_RE.match(s)
        if m:
            if chart and m.group(1) != chart["prefix"]:
                end()
            if not chart:
                chart = {"prefix": m.group(1), "steps": [], "after": []}
            st = {"n": int(m.group(2)), "f": {"STEP": [], "CHECK": [], "YES": [], "NO": []}, "cur": None,
                  "raw": [line]}
            if m.group(3):
                st["f"]["STEP"].append(m.group(3))
            st["cur"] = st["f"]["STEP"]
            chart["steps"].append(st)
            continue
        if not chart:
            out.append(line)
            continue
        if s.startswith('<span id="page-') or s.startswith("![") or s == "[fig]":
            chart["after"].append(line)
            continue
        if not s:
            continue
        if re.match(r"^#{1,3} ", s) or s.startswith("|"):
            end()
            out.append(line)
            continue
        st = chart["steps"][-1]
        st["raw"].append(line)
        lab = CHART_LABEL_RE.match(s)
        if lab:
            st["f"][lab.group(1)].append([lab.group(2)])
            st["cur"] = st["f"][lab.group(1)][-1]
        else:
            st["cur"].append(s.lstrip("# "))     # continuation lines and NOTEs belong to the item above
    end()
    return out


def chapter_names(doc):
    """{page: 'ENGINE (DIAGNOSTICS)'} from the running head at the top left, used to name chapters."""
    out = {}
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                t = "".join(s["text"] for s in line["spans"]).strip()
                if line["bbox"][3] <= HEADER_BAND and line["bbox"][0] < 60 and t.isupper():
                    out[pno] = t
                    break
            if pno in out:
                break
    return out


# ---------------------------------------------------------------------------------------------- driver
def convert(pdf, out_md, pages=None, images=True):
    doc = pymupdf.open(pdf)
    out_dir = os.path.dirname(out_md)
    os.makedirs(out_dir, exist_ok=True)
    rng = range(doc.page_count) if not pages else range(max(0, pages[0]), min(doc.page_count, pages[1] + 1))
    state = {"chapter_names": chapter_names(doc) if not pages else
             {p: v for p, v in chapter_names_range(doc, rng).items()}}
    tmp = out_md + ".part"
    t0 = time.time()
    lines = []
    for i, pno in enumerate(rng):
        lines += page_md(doc, pno, out_dir, images, state)
        if i % 100 == 99:
            print(f"  page {pno + 1}/{doc.page_count}  ({time.time() - t0:.0f} s)", flush=True)
    with open(tmp, "w", encoding="utf-8") as f:
        if state.get("icon_charts"):            # only manuals that print CHECK / YES / NO as icons
            lines = chart_tables(lines)
        f.write("\n".join(link_stacked_dtcs(lines)) + "\n")
    os.replace(tmp, out_md)
    return len(rng)


def chapter_names_range(doc, rng):
    sub = {}
    for pno in rng:
        for b in doc[pno].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                t = "".join(s["text"] for s in line["spans"]).strip()
                if line["bbox"][3] <= HEADER_BAND and line["bbox"][0] < 60 and t.isupper():
                    sub[pno] = t
    return sub


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="convert only manuals whose name contains this")
    ap.add_argument("--pdf", help="convert this PDF instead of the MANUALS list (path, or relative to manual_pdfs/)")
    ap.add_argument("--list", action="store_true", help="show the manuals and whether they're done")
    ap.add_argument("--pages", help="page range for a quick test, e.g. 500-520 (0-based)")
    ap.add_argument("--out", help="output folder for a test run (default: test_batch_out/<name>/)")
    ap.add_argument("--no-images", action="store_true", help="don't save figures (faster, text only)")
    a = ap.parse_args()
    pages = tuple(int(x) for x in a.pages.split("-")) if a.pages else None
    for rel in ([a.pdf] if a.pdf else MANUALS):
        name = os.path.splitext(os.path.basename(rel))[0]
        if a.only and a.only not in name:
            continue
        pdf = os.path.join(PDF_DIR, rel)
        folder = a.out or os.path.join(OUT_DIR, name)
        out_md = os.path.join(folder, f"{name}.fixed.md")
        done = os.path.isfile(out_md) and os.path.isfile(os.path.join(folder, ".pymupdf"))
        if a.list:
            print(f"{'done' if done else 'todo':5} {name}{'' if os.path.isfile(pdf) else '   (PDF missing!)'}")
            continue
        if not os.path.isfile(pdf):
            print(f"[MISSING] {pdf}")
            continue
        if os.path.isfile(out_md) and not a.out:
            bak = out_md + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
            shutil.move(out_md, bak)               # keep marker's version
            print(f"  kept the old one as {os.path.basename(bak)}")
        print(f"[CONVERT] {name}", flush=True)
        n = convert(pdf, out_md, pages, images=not a.no_images)
        if not a.out and not pages:
            open(os.path.join(folder, ".pymupdf"), "w").write(time.strftime("%Y-%m-%d %H:%M\n"))
        print(f"[OK] {n} pages -> {out_md}", flush=True)
    if not a.list:
        print("\nNext: reload them into subaru.db (load_manuals.sh reloads every manual; each reload replaces "
              "that manual's rows), then embed --missing and finish.")


if __name__ == "__main__":
    sys.exit(main())
