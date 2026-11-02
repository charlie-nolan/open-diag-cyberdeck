#!/usr/bin/env python3
"""
Download the public sources for kb/general.db (make-independent car repair knowledge).
Stdlib only. Safe to re-run: files already downloaded are kept. Everything lands in RAW
with a manifest (sources.json) recording URL, publisher, license, era and sha256.

  python3 kb/general/fetch_general.py --raw "<folder>"

PDFs are checked: must start with %PDF and their first pages must mention what we asked
for (otherwise moved to _rejected/, never loaded). Wikipedia comes via its API with the
revision id so every passage can be cited to an exact article version.
"""
import argparse, hashlib, html, json, os, re, shutil, sys, time, urllib.parse, urllib.request

UA = "Mozilla/5.0 (X11; Linux x86_64) cyberdeck-general-kb/1.0 (personal research)"
LOG = []

def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)

def get(url, binary=True, timeout=120, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    log(f"  [FAIL] {url}: {err}")
    return None

def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120]

# ------------------------------------------------------------------ what to fetch
# PDF sources. 'want' = words that must appear in the PDF's first pages (case-insensitive, any of).
PD_US = "US Government work, public domain (17 U.S.C. 105)"
ARCHIVE_QUERIES = [
    dict(key="tm9-8000", q='title:("TM 9-8000") OR title:("principles of automotive vehicles")',
         want=["principles of automotive vehicles", "tm 9-8000"], publisher="US Army",
         title="TM 9-8000 Principles of Automotive Vehicles", license=PD_US, era="carb+early-efi", max=2),
    dict(key="navedtra-cm-basic", q='"NAVEDTRA 14264" OR title:("construction mechanic basic")',
         want=["construction mechanic"], publisher="US Navy", title="Construction Mechanic Basic (NAVEDTRA 14264)",
         license=PD_US, era="mixed", max=2),
    dict(key="navedtra-cm-adv", q='title:("construction mechanic advanced") OR title:("construction mechanic intermediate")',
         want=["construction mechanic"], publisher="US Navy", title="Construction Mechanic Advanced/Intermediate",
         license=PD_US, era="mixed", max=2),
    dict(key="army-wheeled-mech", q='title:("wheeled vehicle mechanic") AND (mediatype:texts)',
         want=["vehicle", "engine"], publisher="US Army", title="Army wheeled vehicle mechanic course",
         license=PD_US, era="mixed", max=2),
]
# Navy training-manual PDFs hosted by the Navy (exact titles unknown until opened: kept only if they match).
DIRECT_PDFS = [
    dict(url=u, want=["construction mechanic", "automotive", "engine"], publisher="US Navy",
         title=None, license=PD_US, era="mixed")
    for u in [
        "https://media.defense.gov/2017/Oct/31/2002658522/-1/-1/1/171031-N-ZY182-15258.pdf",
        "https://media.defense.gov/2017/Oct/31/2002658521/-1/-1/1/171031-N-ZY182-15257.pdf",
        "https://media.defense.gov/2017/Oct/31/2002658519/-1/-1/1/171031-N-ZY182-15255.pdf",
        "https://media.defense.gov/2014/Jun/20/2002655894/-1/-1/1/140620-N-ZZ182-6544.pdf",
        "https://media.defense.gov/2014/Jun/20/2002655885/-1/-1/1/140620-N-ZZ182-6530.pdf",
    ]
]
# Web pages: (url, publisher, license, also harvest PDF links whose URL matches this regex, max PDFs)
PAGES = [
    ("https://everyspec.com/ARMY/TM-Tech-Manual/TM_9-8000_CHG-1_48512/", "US Army (via EverySpec)", PD_US,
     r"\.pdf", 2, ["principles of automotive vehicles", "tm 9-8000"], False),
    ("https://www.fueleconomy.gov/feg/maintain.jsp", "US DOE / EPA fueleconomy.gov", PD_US, None, 0, None, True),
    ("https://www.fueleconomy.gov/feg/drive.shtml", "US DOE / EPA fueleconomy.gov", PD_US, None, 0, None, True),
    ("https://consumer.ftc.gov/articles/auto-repair-basics", "US FTC", PD_US, None, 0, None, True),
    ("https://www.nhtsa.gov/equipment/tires", "US NHTSA", PD_US, r"tire.*\.pdf", 2, ["tire"], True),
    ("https://www.epa.gov/state-and-local-transportation/vehicle-emissions-board-diagnostics-obd",
     "US EPA", PD_US, r"\.pdf", 4, ["diagnostic", "obd"], True),
    ("https://ww2.arb.ca.gov/our-work/programs/obd-board-diagnostic-program", "California Air Resources Board",
     "California state document, personal reference use", r"1968\.?2.*\.pdf|obd.*\.pdf", 3,
     ["diagnostic", "malfunction"], True),
]

WIKI = [  # Wikipedia articles (CC BY-SA 4.0). Missing titles are skipped, not guessed.
    "Internal combustion engine", "Four-stroke engine", "Compression ratio", "Compression test",
    "Leak-down tester", "Engine knocking", "Pre-ignition", "Misfire", "Spark plug", "Ignition coil",
    "Ignition system", "Distributor", "Fuel injection", "Fuel pump", "Fuel filter", "Mass flow sensor",
    "MAP sensor", "Oxygen sensor", "Air–fuel ratio", "Stoichiometry", "Throttle", "Idle air control actuator",
    "Engine control unit", "On-board diagnostics", "OBD-II PIDs", "Catalytic converter",
    "Exhaust gas recirculation", "Positive crankcase ventilation", "Evaporative emission control system",
    "Vacuum leak", "Head gasket", "Cylinder head", "Piston ring", "Valve float", "Timing belt (camshaft)",
    "Timing chain", "Camshaft", "Crankshaft position sensor", "Hydraulic tappet", "Motor oil", "Oil sludge",
    "Oil filter", "Oil pressure", "Engine wear", "Blow-by", "Coolant", "Antifreeze", "Radiator (engine cooling)",
    "Thermostat", "Water pump", "Overheating (combustion engines)", "Engine cooling", "Serpentine belt",
    "Alternator (automotive)", "Automotive battery", "Starter (engine)", "Ground (electricity)",
    "Electrical wiring", "Fuse (electrical)", "Relay", "Multimeter", "Voltage drop", "Parasitic load",
    "Brake fluid", "Disc brake", "Drum brake", "Anti-lock braking system", "Brake fade", "Hydraulic brake",
    "Clutch", "Manual transmission", "Automatic transmission", "Automatic transmission fluid",
    "Continuously variable transmission", "Torque converter", "Differential (mechanical device)",
    "Constant-velocity joint", "Universal joint", "Wheel bearing", "Shock absorber", "Wheel alignment",
    "Power steering", "Tire", "Tire maintenance", "Tread", "Hydroplaning (tires)", "Car air conditioning",
    "Diesel engine", "Glow plug", "Turbocharger", "Supercharger", "Intercooler", "Carburetor", "Contact breaker",
    "Corrosion", "Galvanic corrosion", "Rust", "Engine break-in", "Vehicle inspection", "Preventive maintenance",
]

# ------------------------------------------------------------------ helpers
def pdf_first_text(path, pages=6):
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        d = pymupdf.open(path)
        t = " ".join(d[i].get_text() for i in range(min(pages, d.page_count)))
        meta = " ".join(str(v) for v in (d.metadata or {}).values() if v)
        return (meta + " " + t).lower()
    except Exception:
        return ""

def save_pdf(data, folder, name, want, manifest, rec):
    if not data or data[:4] != b"%PDF":
        log(f"  [SKIP] not a PDF: {rec['url']}")
        return
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(data)
    txt = pdf_first_text(path)
    if want and txt is not None and not any(w in txt for w in want):
        rej = os.path.join(os.path.dirname(folder), "_rejected")
        os.makedirs(rej, exist_ok=True)
        shutil.move(path, os.path.join(rej, name))
        log(f"  [REJECT] {name}: first pages don't mention {want}")
        return
    rec.update(file=os.path.relpath(path, RAW), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
               fetched=time.strftime("%Y-%m-%d"), type="pdf")
    if not rec.get("title"):
        m = re.search(r"(construction mechanic[a-z ,]*|principles of automotive vehicles|[a-z ]*mechanic[a-z ]*)",
                      txt or "")
        rec["title"] = (m.group(1).strip().title() if m else os.path.splitext(name)[0])
    manifest[rec["file"]] = rec
    log(f"  [OK] {name} ({len(data)/1e6:.1f} MB)")

def already(manifest, url):
    return any(r.get("url") == url and os.path.exists(os.path.join(RAW, r["file"])) for r in manifest.values())

def archive(manifest):
    for s in ARCHIVE_QUERIES:
        log(f"[archive.org] {s['key']}")
        q = urllib.parse.urlencode({"q": f"({s['q']}) AND mediatype:texts", "fl[]": "identifier",
                                    "sort[]": "downloads desc", "rows": 8, "output": "json"})
        js = get("https://archive.org/advancedsearch.php?" + q, binary=False)
        if not js:
            continue
        ids = [d["identifier"] for d in json.loads(js).get("response", {}).get("docs", [])]
        got = 0
        for ident in ids:
            if got >= s["max"]:
                break
            meta = get(f"https://archive.org/metadata/{ident}", binary=False)
            if not meta:
                continue
            files = [f for f in json.loads(meta).get("files", []) if f.get("name", "").lower().endswith(".pdf")]
            if not files:
                continue
            f = max(files, key=lambda f: int(f.get("size", 0) or 0))
            url = f"https://archive.org/download/{ident}/{urllib.parse.quote(f['name'])}"
            if already(manifest, url):
                log(f"  [HAVE] {ident}"); got += 1; continue
            if int(f.get("size", 0) or 0) > 400e6:
                log(f"  [SKIP] {ident}: {int(f['size'])/1e6:.0f} MB is too big"); continue
            rec = dict(url=url, item=f"https://archive.org/details/{ident}", publisher=s["publisher"],
                       title=s["title"], license=s["license"], era=s["era"], source_key=s["key"])
            n0 = len(manifest)
            save_pdf(get(url, timeout=600), os.path.join(RAW, "pdf", s["key"]), safe(ident) + ".pdf",
                     s["want"], manifest, rec)
            got += len(manifest) > n0
            time.sleep(1)

def direct(manifest):
    log("[direct PDFs]")
    for s in DIRECT_PDFS:
        if already(manifest, s["url"]):
            log(f"  [HAVE] {s['url']}"); continue
        rec = dict(s); rec.pop("want")
        save_pdf(get(s["url"], timeout=600), os.path.join(RAW, "pdf", "navy"), safe(s["url"].rsplit("/", 1)[1]),
                 s["want"], manifest, rec)

def pages(manifest):
    log("[web pages]")
    for url, pub, lic, pdf_re, pdf_max, want, keep_html in PAGES:
        page = get(url, binary=False)
        if not page:
            continue
        if keep_html:
            os.makedirs(os.path.join(RAW, "html"), exist_ok=True)
            name = safe(urllib.parse.urlparse(url).netloc + urllib.parse.urlparse(url).path) + ".html"
            path = os.path.join(RAW, "html", name)
            open(path, "w", encoding="utf-8").write(page)
            m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
            rec = dict(url=url, publisher=pub, license=lic, era="modern", type="html",
                       title=html.unescape(m.group(1)).strip() if m else name, file=os.path.relpath(path, RAW),
                       sha256=hashlib.sha256(page.encode()).hexdigest(), fetched=time.strftime("%Y-%m-%d"))
            manifest[rec["file"]] = rec
            log(f"  [OK] {name}")
        if pdf_re:
            links = []
            for h in re.findall(r'href="([^"]+)"', page):
                full = urllib.parse.urljoin(url, html.unescape(h))
                if re.search(pdf_re, full, re.I) and full not in links:
                    links.append(full)
            for link in links[:pdf_max]:
                if already(manifest, link):
                    log(f"  [HAVE] {link}"); continue
                rec = dict(url=link, found_on=url, publisher=pub, license=lic, title=None,
                           era="modern" if "everyspec" not in url else "carb+early-efi")
                save_pdf(get(link, timeout=600), os.path.join(RAW, "pdf", safe(urllib.parse.urlparse(url).netloc)),
                         safe(link.rsplit("/", 1)[1].split("?")[0]) or "doc.pdf", want, manifest, rec)

def wiki(manifest):
    log("[wikipedia]")
    os.makedirs(os.path.join(RAW, "wiki"), exist_ok=True)
    for t in WIKI:
        name = safe(t) + ".json"
        path = os.path.join(RAW, "wiki", name)
        if os.path.exists(path) and os.path.relpath(path, RAW) in manifest:
            continue
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts|revisions", "explaintext": 1,
                                    "rvprop": "ids|timestamp", "redirects": 1, "titles": t,
                                    "format": "json", "formatversion": 2})
        js = get("https://en.wikipedia.org/w/api.php?" + q, binary=False)
        if not js:
            continue
        pg = (json.loads(js).get("query", {}).get("pages") or [{}])[0]
        if pg.get("missing") or not pg.get("extract"):
            log(f"  [MISSING] {t}"); continue
        rev = (pg.get("revisions") or [{}])[0]
        title = pg["title"]
        if any(r.get("title") == f"Wikipedia: {title}" for r in manifest.values()):
            continue   # two names redirected to the same article
        json.dump(dict(title=title, extract=pg["extract"], revid=rev.get("revid")), open(path, "w"))
        rec = dict(url=f"https://en.wikipedia.org/w/index.php?title={urllib.parse.quote(title)}&oldid={rev.get('revid')}",
                   publisher="Wikipedia", license="CC BY-SA 4.0 (Wikipedia contributors)", era="modern",
                   type="wiki", title=f"Wikipedia: {title}", file=os.path.relpath(path, RAW),
                   sha256=hashlib.sha256(pg["extract"].encode()).hexdigest(), fetched=time.strftime("%Y-%m-%d"),
                   revision_time=rev.get("timestamp"))
        manifest[rec["file"]] = rec
        time.sleep(0.5)
    log(f"  {sum(1 for r in manifest.values() if r.get('type') == 'wiki')} articles")

def main():
    global RAW
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--only", help="archive,direct,pages,wiki (comma list)")
    a = ap.parse_args()
    RAW = os.path.abspath(a.raw)
    os.makedirs(RAW, exist_ok=True)
    mpath = os.path.join(RAW, "sources.json")
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {}
    steps = dict(archive=archive, direct=direct, pages=pages, wiki=wiki)
    for k in (a.only.split(",") if a.only else steps):
        try:
            steps[k](manifest)
        except Exception as e:
            log(f"[ERROR] step {k}: {e!r}")
        json.dump(manifest, open(mpath, "w"), indent=1)
    kinds = {}
    for r in manifest.values():
        kinds[r.get("type")] = kinds.get(r.get("type"), 0) + 1
    log(f"[DONE] manifest {mpath}: {kinds}")

if __name__ == "__main__":
    main()
