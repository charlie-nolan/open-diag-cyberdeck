#!/usr/bin/env python3
"""
Polite YouTube repair-video ingester for the Open Diagnostic Cyberdeck Community DB.

Turns high-signal mechanic YouTube videos (and their top viewer comments) into standard
`work.db` thread rows compatible with `kb/build_community.py` and `colab/colab_worker.py`:
  - `discover-dtc`: finds the best video(s) per DTC for a make using objective quality gates:
      minimum views, like/view ratio, duration bounds, and positive/negative comment sentiment.
  - `channels`: ingests diagnostic/repair videos from curated real-shop mechanic channels.
  - `export-work`: converts cached json3 transcripts into ~75s timestamped segments (`t_sec`)
      plus top viewer comments as `reply` segments, writing standard `work.db` files ready
      for Colab GPU extraction or local `build_community.py`.

Rules & etiquette:
  - Uses local `yt-dlp` (no cookies, no proxies, no anti-bot bypasses) or optional free
    YouTube Data API v3 (`YOUTUBE_API_KEY`) for zero-scraping search/metadata listing.
  - Polite delays between requests (`--sleep`) and HARD STOP on any HTTP 429 / rate limit.
  - Raw captions stay private on `cache/youtube/` (never in the public repo).

Examples:
  # 1. Small 10-video trial for top Subaru or Chevy codes (polite yt-dlp):
  python3 kb/ingest_youtube.py discover-dtc \
      --make chevrolet --codes P0300,P0420,P0171,P0442,P0335 --per-code 2 --with-comments

  # 2. Pull top 20 diagnostic videos from starter master-tech channels:
  python3 kb/ingest_youtube.py channels --limit-per-channel 5

  # 3. Export cached videos to work DBs ready for Colab or build_community.py:
  python3 kb/ingest_youtube.py export-work --out-dir cache/youtube/work
"""
import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB = os.path.join(ROOT, "kb")
CACHE_DIR = os.environ.get("CYBERDECK_YT_CACHE", os.path.join(ROOT, "cache", "youtube"))
RAW_DB = os.path.join(CACHE_DIR, "youtube_raw.db")
CAPTIONS_DIR = os.path.join(CACHE_DIR, "captions")

CURATED_CHANNELS = [
    ("SouthMainAuto", "https://www.youtube.com/@SouthMainAuto/videos"),
    ("PineHollowAutoDiagnostics", "https://www.youtube.com/@PineHollowAutoDiagnostics/videos"),
    ("ScannerDanner", "https://www.youtube.com/@ScannerDanner/videos"),
    ("RainmanRaysRepairs", "https://www.youtube.com/@RainmanRaysRepairs/videos"),
    ("WatchWesWork", "https://www.youtube.com/@WatchWesWork/videos"),
    ("FordTechMakuloco", "https://www.youtube.com/@FordTechMakuloco/videos"),
    ("TheCarCareNut", "https://www.youtube.com/@TheCarCareNut/videos"),
    ("MrSubaru1387", "https://www.youtube.com/@MrSubaru1387/videos"),
    ("HumbleMechanic", "https://www.youtube.com/@HumbleMechanic/videos"),
    ("RoyaltyAutoService", "https://www.youtube.com/@RoyaltyAutoService/videos"),
    ("DiagnoseDan", "https://www.youtube.com/@DiagnoseDan/videos"),
    ("EricTheCarGuy", "https://www.youtube.com/@EricTheCarGuy/videos"),
]

MAKES_27 = [
    "subaru", "chevrolet", "toyota", "honda", "ford", "nissan", "jeep", "dodge",
    "hyundai", "kia", "gmc", "mazda", "volkswagen", "acura", "lexus", "bmw",
    "volvo", "audi", "mercedes_benz", "chrysler", "buick", "cadillac", "lincoln",
    "scion", "infiniti", "mitsubishi", "mini",
]

MAKE_ALIASES = {
    "chevy": "chevrolet", "silverado": "chevrolet", "tahoe": "chevrolet", "suburban": "chevrolet",
    "equinox": "chevrolet", "malibu": "chevrolet", "impala": "chevrolet", "cobalt": "chevrolet",
    "camry": "toyota", "corolla": "toyota", "tacoma": "toyota", "4runner": "toyota", "prius": "toyota",
    "rav4": "toyota", "tundra": "toyota", "sienna": "toyota", "highlander": "toyota",
    "civic": "honda", "accord": "honda", "cr-v": "honda", "crv": "honda", "odyssey": "honda", "pilot": "honda",
    "f150": "ford", "f-150": "ford", "f250": "ford", "f-250": "ford", "f350": "ford", "f-350": "ford",
    "escape": "ford", "explorer": "ford", "focus": "ford", "fusion": "ford", "mustang": "ford", "ranger": "ford",
    "forester": "subaru", "outback": "subaru", "impreza": "subaru", "wrx": "subaru", "legacy": "subaru", "crosstrek": "subaru",
    "altima": "nissan", "sentra": "nissan", "rogue": "nissan", "frontier": "nissan", "pathfinder": "nissan", "maxima": "nissan",
    "wrangler": "jeep", "cherokee": "jeep", "grand cherokee": "jeep",
    "ram": "dodge", "ram 1500": "dodge", "ram 2500": "dodge", "caravan": "dodge", "durango": "dodge", "charger": "dodge",
    "elantra": "hyundai", "sonata": "hyundai", "tucson": "hyundai", "santa fe": "hyundai",
    "sierra": "gmc", "yukon": "gmc", "acadia": "gmc",
    "vw": "volkswagen", "jetta": "volkswagen", "golf": "volkswagen", "gti": "volkswagen", "passat": "volkswagen", "tiguan": "volkswagen",
    "mercedes": "mercedes_benz", "mercedes-benz": "mercedes_benz", "sprinter": "mercedes_benz",
    "miata": "mazda", "cx-5": "mazda", "mazda3": "mazda", "mazda 3": "mazda",
}

DTC_RE = re.compile(r"\b([PCBU][0-3][0-9A-F]{3})\b", re.I)
SPOKEN_DTC_RE = re.compile(r"\b([PCBU])\s*([O0-3])\s*([0-9A-F])\s*([0-9A-F])\s*([0-9A-F])\b", re.I)

FIX_CONFIRM_RE = re.compile(
    r"\b(this fixed (?:it|my|the)|that fixed (?:it|my|the)|fixed my|fixed it|fixed the (?:issue|problem|code|cel)|"
    r"solved my|finally (?:fixed|solved|got it|found|cleared|running)|saved me \$?\d|saved me (?:hundreds|thousands|a ton|so much|a trip)|"
    r"exact same (?:problem|issue|code|thing)|that was (?:it|my problem|the issue|the culprit)|"
    r"(?:check engine )?light (?:is |went )?(?:off|gone|out)|code(?:s)? (?:is |are |went )?(?:gone|cleared)|"
    r"no more (?:cel|code|misfire|leak|stalling)|passed (?:smog|emissions|inspection)|"
    r"runs? (?:great|smooth|like new|perfect) now|back on the road|worked like a charm|it worked!?)\b", re.I)
POS_COMMENT_RE = re.compile(
    r"\b(saved me|worked|fixed my|fixed it|solved|spot on|nailed it|thank you|thanks|finally|"
    r"life ?saver|lifesaver|helped me|great video|best video|awesome video|you are a genius|god bless|"
    r"got it running|cleared the code|no more cel|check engine light (?:is )?(?:off|gone))\b", re.I)
NEG_COMMENT_RE = re.compile(
    r"\b(didn'?t work|did not work|wrong|clickbait|waste of time|misleading|fake|ai voice|ai slop|"
    r"scam|useless|terrible advice|don'?t do this|do not do this|broke my|made it worse|horrible)\b", re.I)

NON_DIAG_TITLE_RE = re.compile(
    r"\b(tool haul|toolbox tour|unboxing|giveaway|merch|q&a|livestream|live stream|podcast|"
    r"shop tour|channel update|vlog|reacts to|sponsored|top 10 cars|buying guide|should you buy)\b", re.I)
DIAG_TITLE_RE = re.compile(
    r"\b([PCBU][0-3][0-9A-F]{3}|misfire|no start|won'?t start|crank|stall|overheat|leak|code|cel|mil|"
    r"diag\w*|troubleshoot\w*|test\w*|fix\w*|repair|replace\w*|sensor|alternator|battery|parasitic|"
    r"fuel trim|lean|rich|evap|o2|maf|map|tps|abs|traction|transmission|clutch|brake|bearing|noise|knock|tick)\b", re.I)

RAW_SCHEMA = """
CREATE TABLE IF NOT EXISTS video (
  vid TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  title TEXT,
  channel TEXT,
  channel_id TEXT,
  upload_date TEXT,
  duration INTEGER,
  view_count INTEGER,
  like_count INTEGER,
  like_ratio REAL,
  comment_count INTEGER,
  pos_comments INTEGER DEFAULT 0,
  neg_comments INTEGER DEFAULT 0,
  comment_ratio REAL,
  quality_score REAL,
  accepted INTEGER DEFAULT 0,
  reject_reason TEXT,
  make TEXT,
  codes TEXT,
  query_tag TEXT,
  description TEXT,
  comments_json TEXT,
  caption_path TEXT,
  caption_lang TEXT,
  fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS video_make ON video(make, accepted);
CREATE TABLE IF NOT EXISTS search_done (
  query_key TEXT PRIMARY KEY,
  n_found INTEGER,
  n_accepted INTEGER,
  at TEXT
);
"""

WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS thread (
  src TEXT NOT NULL, tid TEXT NOT NULL, sub TEXT, url TEXT, title TEXT, created INTEGER, score INTEGER,
  n_replies INTEGER, segs TEXT NOT NULL, tags TEXT, rules TEXT, card_keep INTEGER, pilot INTEGER DEFAULT 0,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS prefilter_stat (src TEXT, sub TEXT, metric TEXT, n INTEGER, PRIMARY KEY (src, sub, metric));
CREATE TABLE IF NOT EXISTS sub_score (src TEXT, sub TEXT, p90 INTEGER, PRIMARY KEY (src, sub));
CREATE TABLE IF NOT EXISTS run (
  make TEXT, model TEXT, backend TEXT, parallel INTEGER, lite INTEGER, n INTEGER, seconds REAL, started TEXT);
CREATE TABLE IF NOT EXISTS extract (
  src TEXT NOT NULL, tid TEXT NOT NULL, status TEXT NOT NULL, raw TEXT, rec TEXT, checks TEXT,
  llm_model TEXT, ms INTEGER, prompt_tokens INTEGER, eval_tokens INTEGER, tries INTEGER DEFAULT 1, at TEXT,
  PRIMARY KEY (src, tid)) WITHOUT ROWID;
"""

class RateLimitError(RuntimeError):
    """Raised immediately on HTTP 429 / Too Many Requests — caller MUST stop cleanly."""

def open_raw_db(path=RAW_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(CAPTIONS_DIR, exist_ok=True)
    con = sqlite3.connect(path, timeout=60)
    con.executescript(RAW_SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con

def normalize_spoken_dtcs(text):
    """Fix auto-caption mangled DTCs like 'P 0 3 0 3' or 'PO303' -> 'P0303'."""
    def repl(m):
        d1 = "0" if m.group(2).upper() == "O" else m.group(2)
        return f"{m.group(1).upper()}{d1}{m.group(3).upper()}{m.group(4).upper()}{m.group(5).upper()}"
    return SPOKEN_DTC_RE.sub(repl, text or "")

def detect_make(title, description="", fallback=""):
    blob = f" {(title or '').lower()} {(description or '')[:400].lower()} "
    for m in MAKES_27:
        disp = m.replace("_", " ")
        if re.search(rf"(?<![a-z]){re.escape(disp)}(?![a-z])", blob):
            return m
    for alias, m in MAKE_ALIASES.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", blob):
            return m
    return fallback or "general"

def score_comments(comments):
    """Count positive, strong fix-confirmation, and negative feedback in viewer comments."""
    pos = neg = confirmed = 0
    best = []
    for c in comments or []:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        likes = int(c.get("like_count") or 0)
        is_neg = bool(NEG_COMMENT_RE.search(text))
        is_fix = bool(FIX_CONFIRM_RE.search(text)) and not is_neg
        is_pos = (is_fix or bool(POS_COMMENT_RE.search(text))) and not is_neg
        if is_fix:
            confirmed += 1
        if is_pos:
            pos += 1
        elif is_neg:
            neg += 1
        best.append({"text": text[:1200], "likes": likes, "pos": is_pos, "fix": is_fix, "neg": is_neg})
    best.sort(key=lambda x: (-int(x["fix"]), -x["likes"], -int(x["pos"])))
    ratio = (pos / (pos + neg)) if (pos + neg) > 0 else None
    return pos, neg, confirmed, ratio, best[:8]

def evaluate_video(meta, min_views=2000, min_like_ratio=0.008, min_dur=120, max_dur=4500,
                   min_comment_ratio=0.70, require_diag_title=False):
    """Objective, unbiased quality gate for mechanic YouTube videos.
    Returns (accepted: bool, reason: str, metrics: dict).
    """
    title = meta.get("title") or ""
    dur = int(meta.get("duration") or 0)
    views = int(meta.get("view_count") or 0)
    likes = int(meta.get("like_count") or 0)
    like_ratio = (likes / views) if views > 0 else 0.0
    pos, neg, confirmed, c_ratio, top_comments = score_comments(meta.get("comments"))

    eff_like_floor = min(min_like_ratio, 0.005) if (confirmed >= 2 and neg == 0) else min_like_ratio

    reasons = []
    if NON_DIAG_TITLE_RE.search(title):
        reasons.append("non_diag_title")
    if require_diag_title and not DIAG_TITLE_RE.search(title):
        reasons.append("no_diag_keyword")
    if dur < min_dur:
        reasons.append(f"too_short({dur}s)")
    elif dur > max_dur:
        reasons.append(f"too_long({dur}s)")
    if views < min_views:
        reasons.append(f"low_views({views})")
    if likes > 0 and like_ratio < eff_like_floor:
        reasons.append(f"low_like_ratio({like_ratio:.3%})")
    if c_ratio is not None and (pos + neg) >= 2 and c_ratio < min_comment_ratio:
        reasons.append(f"neg_comments({pos}+/{neg}-)")

    import math
    qscore = (like_ratio * 100.0) * math.log10(max(views, 10))
    if c_ratio is not None:
        qscore *= (0.5 + c_ratio)
    if confirmed > 0:
        qscore *= (1.0 + 0.25 * min(confirmed, 6))
    if DIAG_TITLE_RE.search(title):
        qscore *= 1.15

    metrics = {
        "like_ratio": round(like_ratio, 5),
        "pos_comments": pos,
        "neg_comments": neg,
        "fix_confirmations": confirmed,
        "comment_ratio": round(c_ratio, 3) if c_ratio is not None else None,
        "quality_score": round(qscore, 3),
        "top_comments": top_comments,
    }
    return len(reasons) == 0, ";".join(reasons), metrics

def _run_ytdlp(args, timeout=120):
    r = subprocess.run(["yt-dlp"] + args, capture_output=True, text=True, timeout=timeout)
    err = (r.stderr or "") + "\n" + (r.stdout or "")
    if re.search(r"HTTP Error 429|Too Many Requests|Sign in to confirm you.?re not a bot", err, re.I):
        raise RateLimitError(err.strip().splitlines()[-1] if err.strip() else "YouTube HTTP 429 / rate limited")
    return r

def yt_api_search(query, max_results=8):
    """Optional zero-scraping search via official YouTube Data API v3 when YOUTUBE_API_KEY is set."""
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        return None
    params = urllib.parse.urlencode({
        "part": "snippet", "q": query, "type": "video", "maxResults": max_results,
        "relevanceLanguage": "en", "videoCaption": "closedCaption", "key": key,
    })
    url = f"https://www.googleapis.com/youtube/v3/search?{params}"
    with urllib.request.urlopen(url, timeout=30) as r:
        items = json.loads(r.read()).get("items") or []
    vids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    if not vids:
        return []
    params2 = urllib.parse.urlencode({
        "part": "snippet,statistics,contentDetails", "id": ",".join(vids), "key": key,
    })
    with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/videos?{params2}", timeout=30) as r:
        vitems = json.loads(r.read()).get("items") or []
    out = []
    for it in vitems:
        sn = it.get("snippet") or {}
        st = it.get("statistics") or {}
        iso = (it.get("contentDetails") or {}).get("duration") or ""
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
        dur = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 0
        out.append({
            "id": it["id"],
            "webpage_url": f"https://www.youtube.com/watch?v={it['id']}",
            "title": sn.get("title") or "",
            "channel": sn.get("channelTitle") or "",
            "channel_id": sn.get("channelId") or "",
            "upload_date": (sn.get("publishedAt") or "")[:10].replace("-", ""),
            "duration": dur,
            "view_count": int(st.get("viewCount") or 0),
            "like_count": int(st.get("likeCount") or 0),
            "comment_count": int(st.get("commentCount") or 0),
            "description": sn.get("description") or "",
        })
    return out

def fetch_video_meta(vid_or_url, with_comments=False, max_comments=15):
    args = ["--dump-single-json", "--no-warnings", "--skip-download"]
    if with_comments:
        args += ["--write-comments", "--extractor-args", f"youtube:max_comments={max_comments},all,0,0;comment_sort=top"]
    args.append(vid_or_url)
    r = _run_ytdlp(args, timeout=120)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return json.loads(r.stdout)

def fetch_captions(vid, out_dir=CAPTIONS_DIR):
    """Download English captions (manual preferred, else auto-subs) in json3 format. Returns (path, lang)."""
    os.makedirs(out_dir, exist_ok=True)
    for lang in ("en", "en-US", "en-orig"):
        existing = os.path.join(out_dir, f"{vid}.{lang}.json3")
        if os.path.isfile(existing) and os.path.getsize(existing) > 50:
            return existing, lang
    with tempfile.TemporaryDirectory() as td:
        out_tmpl = os.path.join(td, "%(id)s.%(ext)s")
        r = _run_ytdlp([
            "--skip-download", "--no-warnings",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", "en,en-US,en-orig",
            "--sub-format", "json3",
            "-o", out_tmpl,
            f"https://www.youtube.com/watch?v={vid}",
        ], timeout=120)
        if r.returncode != 0:
            return None, None
        for fn in sorted(os.listdir(td)):
            if fn.endswith(".json3"):
                src = os.path.join(td, fn)
                dst = os.path.join(out_dir, fn)
                with open(src, "rb") as fi, open(dst, "wb") as fo:
                    fo.write(fi.read())
                lang = fn.split(".")[1] if fn.count(".") >= 2 else "en"
                return dst, lang
    return None, None

def parse_json3_segments(json3_path, chunk_sec=75, max_chars=9000, seg_chars=1400):
    """Parse YouTube json3 captions into ~75-second segments with exact `t_sec` start timestamps."""
    with open(json3_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    chunks = []
    cur_start = None
    cur_words = []
    for ev in data.get("events") or []:
        t_ms = ev.get("tStartMs")
        segs = ev.get("segs")
        if t_ms is None or not segs:
            continue
        t_s = int(t_ms // 1000)
        piece = "".join(s.get("utf8", "") for s in segs).replace("\n", " ").strip()
        piece = re.sub(r"\s+", " ", piece)
        if not piece or piece in ("[Music]", "[Applause]", "[Laughter]"):
            continue
        if cur_start is None:
            cur_start = t_s
        cur_text_len = sum(len(w) + 1 for w in cur_words)
        if (t_s - cur_start >= chunk_sec or cur_text_len + len(piece) > seg_chars) and cur_words:
            chunks.append((cur_start, normalize_spoken_dtcs(" ".join(cur_words))))
            cur_start = t_s
            cur_words = []
        cur_words.append(piece)
    if cur_words and cur_start is not None:
        chunks.append((cur_start, normalize_spoken_dtcs(" ".join(cur_words))))

    out = []
    total = 0
    for t_s, text in chunks:
        mm, ss = divmod(t_s, 60)
        labeled = f"[{mm:02d}:{ss:02d}] {text}"[:seg_chars]
        if total + len(labeled) > max_chars:
            break
        total += len(labeled)
        out.append({"k": "op", "s": None, "t": labeled, "t_sec": t_s})
    return out

def polite_sleep(base_sec):
    if base_sec > 0:
        time.sleep(base_sec + random.uniform(0.5, max(1.0, base_sec * 0.4)))

def save_video_row(con, meta, accepted, reason, metrics, make, query_tag, cap_path=None, cap_lang=None):
    vid = meta["id"]
    title = meta.get("title") or ""
    desc = (meta.get("description") or "")[:2000]
    codes = sorted(set(c.upper() for c in DTC_RE.findall(normalize_spoken_dtcs(title + " " + desc))))
    con.execute(
        "INSERT OR REPLACE INTO video VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
        (
            vid, meta.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
            title, meta.get("channel") or meta.get("uploader") or "",
            meta.get("channel_id") or "", meta.get("upload_date") or "",
            int(meta.get("duration") or 0), int(meta.get("view_count") or 0),
            int(meta.get("like_count") or 0), metrics["like_ratio"],
            int(meta.get("comment_count") or 0),
            metrics["pos_comments"], metrics["neg_comments"], metrics["comment_ratio"],
            metrics["quality_score"], int(accepted), reason,
            make, ",".join(codes), query_tag, desc,
            json.dumps(metrics["top_comments"], ensure_ascii=False),
            cap_path, cap_lang,
        ),
    )
    con.commit()

def top_codes_for_make(make, n=30):
    """Pick the most common real-world DTCs for a make (all code families: P0/P1/P2/B/C/U) from its DB."""
    db_path = os.path.join(KB, f"{make}.db")
    if not os.path.isfile(db_path):
        return ["P0300", "P0420", "P0171", "P0442", "P0335", "P0340", "P0128", "P0455", "P0101", "P0401"][:n]
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    codes = []
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='comm_code'").fetchone():
        codes = [r[0] for r in c.execute(
            "SELECT code FROM comm_code WHERE op_had=1 GROUP BY code ORDER BY count(*) DESC LIMIT ?",
            (n,))]
    if not codes:
        codes = [r[0] for r in c.execute(
            "SELECT code FROM dtc_procedure GROUP BY code ORDER BY count(*) DESC LIMIT ?",
            (n,))]
    c.close()
    return codes

def cmd_discover_dtc(a):
    con = open_raw_db(a.raw_db)
    codes = [c.strip().upper() for c in a.codes.split(",") if c.strip()] if a.codes else top_codes_for_make(a.make, a.top_codes)
    make_label = a.make.replace("_", " ").title()
    model_label = (a.model or "").strip()
    car_label = f"{make_label} {model_label}".strip()
    print(f"Discovering YouTube diagnostic videos for {car_label} ({len(codes)} codes, per_code={a.per_code}, sleep={a.sleep}s)")

    total_acc = 0
    try:
        for code in codes:
            if a.max_captions > 0 and total_acc >= a.max_captions:
                print(f"Reached polite --max-captions {a.max_captions} cap for this window; stopping cleanly.")
                break
            qkey = f"{a.make}:{model_label.lower()}:{code}" if model_label else f"{a.make}:{code}"
            if not a.force and con.execute("SELECT 1 FROM search_done WHERE query_key=?", (qkey,)).fetchone():
                print(f"  [skip] {qkey} already searched")
                continue
            query = f"{car_label} {code} diagnosis fix"
            cands = yt_api_search(query, max_results=a.search_n)
            if cands is None:
                r = _run_ytdlp([
                    f"ytsearch{a.search_n}:{query}",
                    "--dump-json", "--flat-playlist", "--no-warnings",
                ], timeout=90)
                cands = []
                for ln in r.stdout.splitlines():
                    ln = ln.strip()
                    if ln:
                        try:
                            cands.append(json.loads(ln))
                        except ValueError:
                            pass
                polite_sleep(a.sleep)

            scored = []
            for c in cands:
                vid = c.get("id")
                if not vid:
                    continue
                if con.execute("SELECT 1 FROM video WHERE vid=?", (vid,)).fetchone():
                    continue
                if c.get("like_count") is None or a.with_comments:
                    full = fetch_video_meta(f"https://www.youtube.com/watch?v={vid}",
                                            with_comments=a.with_comments, max_comments=a.max_comments)
                    polite_sleep(a.sleep)
                    if not full:
                        continue
                    c = full
                acc, reason, metrics = evaluate_video(
                    c, min_views=a.min_views, min_like_ratio=a.min_like_ratio,
                    min_dur=a.min_dur, max_dur=a.max_dur, min_comment_ratio=a.min_comment_ratio,
                )
                det_make = detect_make(c.get("title"), c.get("description"), fallback=a.make)
                scored.append((metrics["quality_score"], acc, reason, metrics, c, det_make))

            scored.sort(key=lambda x: (-int(x[1]), -x[0]))
            kept = 0
            for qscore, acc, reason, metrics, c, det_make in scored:
                vid = c["id"]
                cap_path = cap_lang = None
                if acc and kept < a.per_code:
                    cap_path, cap_lang = fetch_captions(vid, CAPTIONS_DIR)
                    polite_sleep(a.sleep)
                    if not cap_path:
                        acc = False
                        reason = (reason + ";no_captions").strip(";")
                    else:
                        kept += 1
                        total_acc += 1
                elif acc:
                    acc = False
                    reason = "extra_above_per_code"
                save_video_row(con, c, acc, reason, metrics, det_make, qkey, cap_path, cap_lang)
                tag = "KEEP" if acc else f"DROP({reason})"
                print(f"    [{code}] {tag:28s} score={qscore:6.1f} views={c.get('view_count',0):7,d} "
                      f"like={metrics['like_ratio']:.2%} fix={metrics['fix_confirmations']} pos/neg={metrics['pos_comments']}/{metrics['neg_comments']} "
                      f"| {c.get('title','')[:60]}")

            con.execute("INSERT OR REPLACE INTO search_done VALUES (?,?,?,datetime('now'))", (qkey, len(scored), kept))
            con.commit()
    except RateLimitError as e:
        print(f"\nSTOPPED CLEANLY ON YOUTUBE RATE LIMIT (HTTP 429): {e}", file=sys.stderr)
        print("All fetched metadata & captions so far are safely saved. Re-run later to resume.", file=sys.stderr)
        con.close()
        sys.exit(42)
    finally:
        try:
            con.close()
        except Exception:
            pass
    print(f"Done discover-dtc: {total_acc} videos accepted with captions -> {a.raw_db}")

def cmd_channels(a):
    con = open_raw_db(a.raw_db)
    if a.channels:
        ch_list = [(c.strip().lstrip("@"), f"https://www.youtube.com/@{c.strip().lstrip('@')}/videos")
                   for c in a.channels.split(",") if c.strip()]
    else:
        ch_list = CURATED_CHANNELS
    total_acc = 0
    try:
        for name, url in ch_list:
            if a.max_captions > 0 and total_acc >= a.max_captions:
                break
            print(f"\n=== Channel: {name} (limit {a.limit_per_channel}) ===", flush=True)
            r = _run_ytdlp([
                "--flat-playlist", "--dump-json", "--no-warnings",
                "--playlist-end", str(a.limit_per_channel), url,
            ], timeout=120)
            polite_sleep(a.sleep)
            entries = []
            for ln in r.stdout.splitlines():
                if ln.strip():
                    try:
                        entries.append(json.loads(ln))
                    except ValueError:
                        pass
            for e in entries:
                if a.max_captions > 0 and total_acc >= a.max_captions:
                    print(f"Reached polite --max-captions {a.max_captions} cap for this window; stopping cleanly.")
                    break
                vid = e.get("id")
                if not vid or con.execute("SELECT 1 FROM video WHERE vid=?", (vid,)).fetchone():
                    continue
                if NON_DIAG_TITLE_RE.search(e.get("title") or ""):
                    continue
                full = fetch_video_meta(f"https://www.youtube.com/watch?v={vid}",
                                        with_comments=a.with_comments, max_comments=a.max_comments)
                polite_sleep(a.sleep)
                if not full:
                    continue
                acc, reason, metrics = evaluate_video(
                    full, min_views=a.min_views, min_like_ratio=a.min_like_ratio,
                    min_dur=a.min_dur, max_dur=a.max_dur, min_comment_ratio=a.min_comment_ratio,
                    require_diag_title=False,
                )
                det_make = detect_make(full.get("title"), full.get("description"), fallback="general")
                cap_path = cap_lang = None
                if acc:
                    cap_path, cap_lang = fetch_captions(vid, CAPTIONS_DIR)
                    polite_sleep(a.sleep)
                    if not cap_path:
                        acc, reason = False, "no_captions"
                    else:
                        total_acc += 1
                save_video_row(con, full, acc, reason, metrics, det_make, f"ch:{name}", cap_path, cap_lang)
                tag = "KEEP" if acc else f"DROP({reason})"
                print(f"  [{det_make:10s}] {tag:26s} like={metrics['like_ratio']:.2%} | {full.get('title','')[:65]}")
    except RateLimitError as e:
        print(f"\nSTOPPED CLEANLY ON YOUTUBE RATE LIMIT (HTTP 429): {e}", file=sys.stderr)
        con.close()
        sys.exit(42)
    finally:
        try:
            con.close()
        except Exception:
            pass
    print(f"Done channels: {total_acc} videos accepted with captions -> {a.raw_db}")

def cmd_export_work(a):
    """Export accepted YouTube videos with captions into build_community.py `work.db` format."""
    con = open_raw_db(a.raw_db)
    os.makedirs(a.out_dir, exist_ok=True)
    rows = con.execute(
        "SELECT vid, url, title, channel, upload_date, like_count, view_count, like_ratio, "
        "make, codes, comments_json, caption_path FROM video WHERE accepted=1 AND caption_path IS NOT NULL"
    ).fetchall()
    con.close()
    if not rows:
        print("No accepted captioned videos to export yet.")
        return

    by_make = {}
    for r in rows:
        by_make.setdefault(r[8] or "general", []).append(r)

    total = 0
    for make, vrows in sorted(by_make.items()):
        wpath = os.path.join(a.out_dir, f"{make}.work.db")
        w = sqlite3.connect(wpath, timeout=60)
        w.executescript(WORK_SCHEMA)
        w.execute("PRAGMA journal_mode=WAL")
        n_make = 0
        for vid, url, title, channel, udate, likes, views, lratio, _, codes, cjson, cap_path in vrows:
            if not os.path.isfile(cap_path):
                continue
            segs = parse_json3_segments(cap_path)
            if not segs:
                continue
            segs[0]["t"] = f"YouTube repair video ({channel}): {title}\n\n" + segs[0]["t"]
            comments = json.loads(cjson or "[]")
            for cm in comments[:5]:
                if cm.get("neg"):
                    continue
                segs.append({"k": "reply", "s": int(cm.get("likes") or 0), "t": cm["text"]})

            created = 0
            if udate and len(udate) == 8 and udate.isdigit():
                try:
                    created = int(time.mktime(time.strptime(udate, "%Y%m%d")))
                except ValueError:
                    created = 0
            src = "yt_" + re.sub(r"[^a-z0-9]+", "_", (channel or "youtube").lower()).strip("_")[:24]
            tags = {"models": "", "years": "", "engines": "", "dtcs": codes or "", "like_ratio": lratio}
            rules = {"reply": True, "code": bool(codes), "symptom": True, "part": True, "test": True, "outcome": True, "keep": True}
            w.execute(
                "INSERT OR REPLACE INTO thread(src, tid, sub, url, title, created, score, n_replies, segs, tags, rules, card_keep, pilot) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (src, vid, channel or "YouTube", url, title, created, int(likes or 0),
                 max(3, len(comments)), json.dumps(segs, ensure_ascii=False),
                 json.dumps(tags), json.dumps(rules), 1),
            )
            n_make += 1
            total += 1
        w.commit()
        w.close()
        print(f"Exported {n_make:4d} YouTube videos -> {wpath}")
    print(f"Total exported: {total} videos across {len(by_make)} make DBs.")

def main():
    ap = argparse.ArgumentParser(description="Polite YouTube repair-video ingester for Cyberdeck Community DB")
    ap.add_argument("--raw-db", default=RAW_DB)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--sleep", type=float, default=5.0, help="Base polite delay between yt-dlp requests (seconds)")
        p.add_argument("--min-views", type=int, default=2000)
        p.add_argument("--min-like-ratio", type=float, default=0.008, help="Minimum likes/views ratio (default 0.8%%)")
        p.add_argument("--min-dur", type=int, default=120, help="Min video duration seconds (skips Shorts)")
        p.add_argument("--max-dur", type=int, default=4500, help="Max video duration seconds (skips long livestreams)")
        p.add_argument("--min-comment-ratio", type=float, default=0.70, help="Min positive/(pos+neg) comment ratio")
        p.add_argument("--with-comments", action="store_true", help="Also pull top viewer comments for sentiment + extra fixes")
        p.add_argument("--max-comments", type=int, default=15)
        p.add_argument("--max-captions", type=int, default=0, help="Polite cap on fresh caption downloads per run (0=unlimited)")

    p1 = sub.add_parser("discover-dtc", help="Find the best verified YouTube video(s) per DTC for a make/model")
    p1.add_argument("--make", required=True, help="Make name (e.g. chevrolet, toyota, ford, subaru)")
    p1.add_argument("--model", default="", help="Optional model name to target that model's niche creators (e.g. Tahoe, Camry, F-150)")
    p1.add_argument("--codes", default="", help="Comma-separated DTCs (else top codes from kb/<make>.db)")
    p1.add_argument("--top-codes", type=int, default=20)
    p1.add_argument("--search-n", type=int, default=6, help="Candidates to evaluate per DTC")
    p1.add_argument("--per-code", type=int, default=2, help="Best verified videos to keep per DTC")
    p1.add_argument("--force", action="store_true")
    add_common(p1)

    p2 = sub.add_parser("channels", help="Ingest diagnostic videos from curated mechanic channels")
    p2.add_argument("--channels", default="", help="Comma-separated @handles (default: curated master-tech list)")
    p2.add_argument("--limit-per-channel", type=int, default=10)
    add_common(p2)

    p3 = sub.add_parser("export-work", help="Export accepted captioned videos into work.db format for Colab/local extraction")
    p3.add_argument("--out-dir", default=os.path.join(CACHE_DIR, "work"))

    a = ap.parse_args()
    if a.cmd == "discover-dtc":
        cmd_discover_dtc(a)
    elif a.cmd == "channels":
        cmd_channels(a)
    elif a.cmd == "export-work":
        cmd_export_work(a)

if __name__ == "__main__":
    main()
