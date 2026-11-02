#!/usr/bin/env python3
"""
Mechanics StackExchange (mechanics.stackexchange.com) -> 'stackexchange' rows in each make's knowledge table.
Each row is one question with its accepted answer (confirmed=1: the asker marked it as the answer) or, when
none was accepted, its best answer scoring 3 or more (confirmed=0). A question goes to a make when its tags
or title name the make; the model is filled when a tag or the title names one of that make's NHTSA model
names, and the year when the title has one. Questions naming no make are left out. No GPU.

  python3 add_stackexchange.py kb/chevrolet.db kb/subaru.db    # these DBs (make from the file name)
  python3 add_stackexchange.py --done                          # every finished make not done yet, and subaru
The first run reads Posts.xml once into kb/stackexchange_qa.db (a few minutes). Posts.xml comes from
--posts, raw/stackexchange/, ~/cyberdeck_forge/hoarder_input/, or is downloaded (archive.org, ~100 MB .7z;
reading it needs 7z: `sudo pacman -S p7zip`, or `pip install py7zr`).
Run `build_subaru_kb.py --db X embed --missing` afterwards for vectors.
"""
import argparse
import html
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from knowledge_load import HERE, make_names, nhtsa_models, target_dbs, write_kind   # noqa: E402

CK = os.path.dirname(HERE)
RAW = os.path.join(CK, "raw", "stackexchange")
URL = "https://archive.org/download/stackexchange/mechanics.stackexchange.com.7z"
CACHE = os.path.join(HERE, "stackexchange_qa.db")
SITE = "https://mechanics.stackexchange.com/q/"
MIN_SCORE = 3                 # best answer kept without acceptance only at this score
Q_CHARS, A_CHARS = 1500, 3000
YEAR = re.compile(r"(?<![\d.,$])(19[89]\d|20[0-2]\d)(?![\d,]|\s*(?:miles|mi\b|km|rpm|cc))", re.I)


def find_posts(arg):
    for p in [arg, os.path.join(RAW, "Posts.xml"), os.path.expanduser("~/cyberdeck_forge/hoarder_input/Posts.xml")]:
        if p and os.path.isfile(p):
            return p, None
    z = os.path.join(RAW, "mechanics.stackexchange.com.7z")
    if not os.path.isfile(z):
        os.makedirs(RAW, exist_ok=True)
        print(f"downloading {URL}", flush=True)
        req = urllib.request.Request(URL, headers={"User-Agent": "subaru-cyberdeck/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(z + ".part", "wb") as f:
            while b := r.read(1 << 20):
                f.write(b)
        os.replace(z + ".part", z)
    return None, z


def open_posts(path, z):
    if path:
        return open(path, "rb"), None
    exe = shutil.which("7z") or shutil.which("7za")
    if exe:      # streamed straight out of the archive, nothing extracted to disk
        p = subprocess.Popen([exe, "e", "-so", z, "Posts.xml"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return p.stdout, p
    try:
        import py7zr
    except ImportError:
        sys.exit("can't open the .7z: install 7z (sudo pacman -S p7zip) or `pip install py7zr`")
    with py7zr.SevenZipFile(z) as a:
        a.extract(path=RAW, targets=["Posts.xml"])
    return open(os.path.join(RAW, "Posts.xml"), "rb"), None


def text(h):
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", h or "")
    h = re.sub(r"(?i)<li[^>]*>", "- ", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()


def tags(s):
    return [t for t in re.split(r"[<>|]+", (s or "").lower()) if t]


def build_cache(path, z):
    tmp = CACHE + ".part"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript("""CREATE TABLE q (id INTEGER PRIMARY KEY, title TEXT, tags TEXT, body TEXT, accepted INTEGER);
                       CREATE TABLE a (id INTEGER PRIMARY KEY, parent INTEGER, score INTEGER, body TEXT);""")
    fh, proc = open_posts(path, z)
    nq = na = 0
    for _, el in ET.iterparse(fh, events=("end",)):
        if el.tag != "row":
            continue
        g = el.attrib.get
        if g("PostTypeId") == "1":
            c.execute("INSERT INTO q VALUES (?,?,?,?,?)", (int(g("Id")), html.unescape(g("Title") or ""),
                      " ".join(tags(g("Tags"))), text(g("Body")), int(g("AcceptedAnswerId") or 0) or None))
            nq += 1
        elif g("PostTypeId") == "2" and g("ParentId"):
            c.execute("INSERT INTO a VALUES (?,?,?,?)", (int(g("Id")), int(g("ParentId")),
                      int(g("Score") or 0), text(g("Body"))))
            na += 1
        el.clear()
    if proc:
        proc.wait()
    if nq == 0:
        sys.exit("Posts.xml had no questions (wrong file, or 7z failed)")
    c.execute("CREATE INDEX a_parent ON a(parent, score)")
    c.commit()
    c.close()
    os.replace(tmp, CACHE)
    print(f"stackexchange cache: {nq} questions, {na} answers", flush=True)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def words_re(name):
    return re.compile(r"(?<![a-z0-9])" + r"[\s-]*".join(map(re.escape, name.lower().split())) + r"(?![a-z0-9])")


def in_title(r, short, title):
    if not short:
        return bool(r.search(title.lower()))
    return any(m.group(0) != m.group(0).lower() for m in re.finditer(r.pattern, title, re.I))


def qa_rows(c):
    """(qid, title, tags, question, answer, accepted?, score) for questions with a usable answer."""
    return c.execute("""
        SELECT q.id, q.title, q.tags, q.body, a.body, a.id = q.accepted, a.score FROM q JOIN a ON a.id = coalesce(
          (SELECT x.id FROM a x WHERE x.id = q.accepted),
          (SELECT x.id FROM a x WHERE x.parent = q.id AND x.score >= ? ORDER BY x.score DESC LIMIT 1))""",
                     (MIN_SCORE,))


def match(db, qa):
    name = os.path.splitext(os.path.basename(db))[0]
    mk = [m.lower() for m in make_names(name)]
    # short names (RAM, VW) count in a title only when not written in lower case ('ram air intake' isn't a Ram)
    mk_re = [(words_re(m), len(m) <= 3) for m in mk]
    mk_norm = {norm(m) for m in mk}
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    models = sorted(nhtsa_models(c), key=len, reverse=True)
    c.close()
    mods = [(m, norm(m), words_re(m)) for m in models if norm(m)]
    out = []
    for qid, title, tg, q, a, acc, score in qa:
        tl = tg.split()
        parts = {p for t in tl for p in [t] + t.split("-")}
        tlow = title.lower()
        by_tag = bool(mk_norm & {norm(p) for p in parts})
        long_t = any(in_title(r, False, title) for r, short in mk_re if not short)
        short_t = any(in_title(r, True, title) for r, short in mk_re if short)
        if not (by_tag or long_t or short_t):
            continue
        # model: a tag like 'make-model' or 'model', else the title
        rest = {norm(t.split("-", 1)[1]) for t in tl if "-" in t and norm(t.split("-", 1)[0]) in mk_norm} | \
               {norm(t) for t in tl}
        model = None
        for m, n, r in mods:
            if n in rest:
                model = m
                break
        if model is None:   # the title, only right after the make or a year ("2005 Honda Accord", "Fit" alone isn't)
            lead = "(?:" + "|".join(re.escape(k) for k in mk) + r"|(?:19|20)\d\d)(?:[\s-]+[a-z0-9]+)?[\s-]+"
            for m, n, r in mods:
                if re.search(lead + r.pattern, tlow):
                    model = m
                    break
        if model is None and not (by_tag or long_t):
            continue        # a short make name alone (Ram, VW) needs one of its models named too
        y = YEAR.search(title)
        yr = int(y.group(1)) if y else None
        head = "Accepted answer" if acc else f"Top answer (score {score}, not accepted)"
        body = f"Question: {q[:Q_CHARS]}\n\n{head}: {a[:A_CHARS]}"
        out.append((str(qid), model, yr, yr, title, body, SITE + str(qid), 1 if acc else 0))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbs", nargs="*")
    ap.add_argument("--done", action="store_true")
    ap.add_argument("--posts", help="path to Posts.xml")
    ap.add_argument("--rebuild-cache", action="store_true")
    a = ap.parse_args()
    dbs = target_dbs(a.dbs, a.done, "se")
    if not dbs:
        print("stackexchange: nothing to do")
        return
    if a.rebuild_cache or not os.path.isfile(CACHE):
        build_cache(*find_posts(a.posts))
    c = sqlite3.connect(f"file:{CACHE}?mode=ro", uri=True)
    qa = qa_rows(c).fetchall()
    c.close()
    for db in dbs:
        rows = match(db, qa)
        n = write_kind(db, "stackexchange", rows, "se")
        withm = sum(1 for r in rows if r[1])
        print(f"{os.path.splitext(os.path.basename(db))[0]}: {n} StackExchange rows ({withm} with a model)", flush=True)


if __name__ == "__main__":
    main()
