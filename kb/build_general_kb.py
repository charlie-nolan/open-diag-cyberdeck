#!/usr/bin/env python3
"""
Build kb/general.db: make-independent car repair reference (how systems work, troubleshooting
charts as the books print them, what skipped maintenance does). Same doc/section/section_fts
schema as the make DBs (imported from build_subaru_kb.py), so kb_retriever and `embed` work
unchanged. Book text only: no hand- or LLM-written symptom->cause rows.

  python3 kb/build_general_kb.py --raw "<raw folder with sources.json>" [--db kb/general.db]
  then (GPU free):  venv/bin/python kb/build_subaru_kb.py --db kb/general.db embed
                    python3 kb/build_subaru_kb.py --db kb/general.db finish

Extra tables (citation + filtering):
  source(doc_id, url, publisher, license, era, file, sha256, fetched)
  section_tag(section_id, tag)   tags from words in the passage itself: carburetor, breaker_points,
                                 diesel, efi, obd2. Lets the app skip carburetor-era text on an EFI car.
Re-running rebuilds the DB from scratch (old file kept as general.db.bak-<date>).
"""
import argparse, hashlib, html, json, os, re, shutil, sqlite3, sys, time
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_subaru_kb import SCHEMA  # noqa: E402

TARGET = 3000      # chars per stored passage
MAX = 5000
MIN_PAGE_TEXT = 80  # pages with less text are counted as scans (need OCR later)

EXTRA = """
CREATE TABLE IF NOT EXISTS source (
  doc_id INTEGER PRIMARY KEY REFERENCES doc(id),
  url TEXT, publisher TEXT, license TEXT, era TEXT, file TEXT, sha256 TEXT, fetched TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS section_tag (
  section_id INTEGER NOT NULL REFERENCES section(id), tag TEXT NOT NULL,
  PRIMARY KEY (section_id, tag)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS section_tag_tag ON section_tag(tag);
"""
TAGS = {
    "carburetor": r"\bcarburet(?:or|ion|tor)s?\b|\bchoke valve\b|\bfloat bowl\b",
    "breaker_points": r"\bbreaker points?\b|\bcontact points\b|\bpoint gap\b|\bdwell\b",
    "diesel": r"\bdiesel\b|\bglow plugs?\b|\binjection pump\b",
    "efi": r"\belectronic fuel injection\b|\bfuel injectors?\b|\bthrottle body\b|\becm\b|\bpcm\b",
    "obd2": r"\bobd[- ]?ii\b|\bobd2\b|\bdiagnostic trouble codes?\b|\bmalfunction indicator\b|\bp0\d{3}\b",
}
TAG_RE = {k: re.compile(v, re.I) for k, v in TAGS.items()}


def chunk(text, target=TARGET, maxlen=MAX):
    """Split at blank lines, then lines, never mid-word."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        while len(p) > maxlen:
            cut = p.rfind("\n", 0, maxlen)
            cut = cut if cut > maxlen // 2 else p.rfind(" ", 0, maxlen)
            cut = cut if cut > 0 else maxlen
            if cur:
                out.append(cur); cur = ""
            out.append(p[:cut].strip()); p = p[cut:].strip()
        if cur and len(cur) + len(p) + 2 > target:
            out.append(cur); cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


def clean(t):
    t = t.replace("­", "").replace("\r", "")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)           # hyphenated line breaks
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def add_section(conn, doc_id, chapter, topic, title, page, body, stats):
    body = clean(body)
    if len(body) < 40:
        return
    for k, piece in enumerate(chunk(body), 1):
        h = hashlib.blake2b(piece.encode(), digest_size=8).digest()
        t = title + (f" (part {k})" if k > 1 else "")
        path = " > ".join(x for x in (chapter, topic if topic != title else None, t) if x)
        cur = conn.execute(
            "INSERT OR IGNORE INTO section(doc_id,chapter,topic,title,path,part,page,body,body_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?)", (doc_id, chapter, topic, t, path, k, page, piece, h))
        if not cur.rowcount:
            stats["dup"] += 1
            continue
        stats["kept"] += 1
        sid = cur.lastrowid
        conn.executemany("INSERT OR IGNORE INTO section_tag VALUES (?,?)",
                         [(sid, tag) for tag, rx in TAG_RE.items() if rx.search(piece)])


# ------------------------------------------------------------------ PDF
def load_pdf(conn, doc_id, path, stats):
    import pymupdf
    d = pymupdf.open(path)
    toc = [(lvl, title.strip(), pg) for lvl, title, pg in d.get_toc() if pg > 0]
    # page -> (chapter, topic) from the PDF's own bookmarks, if it has any
    heads = {}
    chap = topic = None
    marks = sorted(toc, key=lambda x: x[2])
    mi = 0
    for p in range(1, d.page_count + 1):
        while mi < len(marks) and marks[mi][2] <= p:
            lvl, t, _ = marks[mi]
            if lvl == 1:
                chap, topic = t, None
            else:
                topic = t
            mi += 1
        heads[p] = (chap, topic)
    buf, buf_head, buf_page = [], None, None
    chap_re = re.compile(r"^\s*(CHAPTER\s+\d+[A-Z]?\b.*|SECTION\s+[IVX\d]+\b.*)$", re.M)
    text_chap = None

    def flush():
        if buf:
            c, tp = buf_head
            add_section(conn, doc_id, c, tp, tp or c or os.path.basename(path), buf_page, "\n\n".join(buf), stats)

    for i in range(d.page_count):
        txt = d[i].get_text("text", sort=True)
        if len(txt.strip()) < MIN_PAGE_TEXT:
            stats["scan_pages"] += 1
            continue
        stats["pages"] += 1
        p = i + 1
        c, tp = heads.get(p, (None, None))
        if not toc:                                  # no bookmarks: use printed CHAPTER lines
            m = chap_re.search(txt[:400])
            if m:
                text_chap = re.sub(r"\s+", " ", m.group(1)).strip()[:120]
            c, tp = text_chap, None
        head = (c, tp)
        if head != buf_head or sum(len(b) for b in buf) > TARGET:
            flush()
            buf, buf_head, buf_page = [], head, p
        buf.append(txt)
    flush()


# ------------------------------------------------------------------ HTML
class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form", "aside"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.head = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("h1", "h2", "h3"):
            self.parts.append(("\x00H", ""))
        elif tag in ("p", "li", "br", "tr", "div"):
            self.parts.append(("t", "\n"))

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in ("h1", "h2", "h3"):
            self.parts.append(("\x00/H", ""))

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(("t", data))


def load_html(conn, doc_id, path, page_title, stats):
    p = _Text()
    p.feed(open(path, encoding="utf-8", errors="replace").read())
    secs, head, cur, in_h = [], page_title, [], False
    for kind, val in p.parts:
        if kind == "\x00H":
            if cur:
                secs.append((head, "".join(cur))); cur = []
            in_h, head = True, ""
        elif kind == "\x00/H":
            in_h = False; head = re.sub(r"\s+", " ", head).strip() or page_title
        elif in_h:
            head += val
        else:
            cur.append(val)
    if cur:
        secs.append((head, "".join(cur)))
    for h, body in secs:
        body = "\n".join(l.strip() for l in html.unescape(body).split("\n") if l.strip())
        if len(body) > 120:
            add_section(conn, doc_id, page_title, h, h, None, body, stats)


# ------------------------------------------------------------------ Wikipedia
SKIP_WIKI = {"See also", "References", "External links", "Further reading", "Notes", "Bibliography",
             "Sources", "Citations", "Footnotes", "In popular culture", "Gallery"}


def load_wiki(conn, doc_id, path, stats):
    j = json.load(open(path))
    title = j["title"]
    parts = re.split(r"^(={2,4})\s*(.+?)\s*\1\s*$", j["extract"], flags=re.M)
    secs = [("Overview", parts[0])]
    for i in range(1, len(parts) - 2, 3):
        secs.append((parts[i + 1], parts[i + 2]))
    for h, body in secs:
        if h in SKIP_WIKI:
            continue
        add_section(conn, doc_id, title, h, h, None, body, stats)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--db", default=os.path.join(HERE, "general.db"))
    a = ap.parse_args()
    manifest = json.load(open(os.path.join(a.raw, "sources.json")))
    if os.path.exists(a.db):
        bak = a.db + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.move(a.db, bak)
        print("old DB kept as", bak)
    conn = sqlite3.connect(a.db)
    conn.executescript(SCHEMA)
    conn.executescript(EXTRA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '1')")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('kind', 'general')")
    report = []
    for rel, r in sorted(manifest.items()):
        path = os.path.join(a.raw, rel)
        if not os.path.exists(path):
            report.append(f"[MISSING FILE] {rel}"); continue
        title = (r.get("title") or rel)[:200]
        if conn.execute("SELECT 1 FROM doc WHERE title=?", (title,)).fetchone():
            title = f"{title} [{os.path.basename(rel)}]"
        cur = conn.execute("INSERT INTO doc(kind,title,file_name,license) VALUES ('general',?,?,?)",
                           (title, os.path.basename(rel), r.get("license")))
        doc_id = cur.lastrowid
        conn.execute("INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?)",
                     (doc_id, r.get("url"), r.get("publisher"), r.get("license"), r.get("era"), rel,
                      r.get("sha256"), r.get("fetched"), r.get("item") or r.get("found_on")))
        stats = dict(kept=0, dup=0, pages=0, scan_pages=0)
        try:
            {"pdf": lambda: load_pdf(conn, doc_id, path, stats),
             "html": lambda: load_html(conn, doc_id, path, title, stats),
             "wiki": lambda: load_wiki(conn, doc_id, path, stats)}[r.get("type", "pdf")]()
        except Exception as e:
            report.append(f"[ERROR] {rel}: {e!r}")
        conn.commit()
        line = f"{stats['kept']:5} passages  {title}"
        if r.get("type", "pdf") == "pdf":
            line += f"  ({stats['pages']} text pages, {stats['scan_pages']} scan/empty pages)"
        report.append(line)
        print(line, flush=True)
    conn.execute("INSERT INTO section_fts(section_fts) VALUES ('rebuild')")
    conn.commit()
    n = conn.execute("SELECT count(*) FROM section").fetchone()[0]
    tags = dict(conn.execute("SELECT tag, count(*) FROM section_tag GROUP BY tag").fetchall())
    conn.close()
    print("\n".join(["", "== general.db", *report, f"TOTAL {n} passages, tags {tags}",
                     f"{os.path.getsize(a.db)/1e6:.1f} MB  {a.db}"]))


if __name__ == "__main__":
    main()
