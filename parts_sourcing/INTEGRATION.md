# Hooking parts_sourcing into the deck app

For the thread that owns `app/`. The module lives at `~/open-diag-cyberdeck/parts_sourcing/` and only reads `kb/subaru.db` and `kb/yards.db`. It never talks to the car, and only `yard_sync.py` goes online.

## 1. Import

```python
import sys, os
sys.path.insert(0, os.path.expanduser(CFG.get("parts_sourcing_dir", "~/open-diag-cyberdeck/parts_sourcing")))
try:
    import parts_sourcing as PS
    from parts_sourcing import qr as PSQR, sheet as PSSHEET
except ImportError:
    PS = None          # feature off; top bar can say "parts sourcing missing"
```

Pass the case's car so job lookups use it:

```python
ps_cfg = PS.settings()
ps_cfg["kb_db"] = CFG["kb_db"]
ps_cfg["car"] = {"make": "Subaru", "model": case.model, "year": case.year}
```

## 2. `/part` slash command (Chat tab, never saved, like the other lookups)

```python
PN_RE = re.compile(r"^\d{5}-?[A-Z]{2}[0-9A-Z]{3}$", re.I)

def cmd_part(arg):
    if PN_RE.match(arg.strip()):
        return PS.sourcing(arg, cfg=ps_cfg)["text"]
    rows = PS.parts_for_job(ps_cfg["kb_db"], arg, ps_cfg["car"]["model"], ps_cfg["car"]["year"])
    if not rows:
        return f"No CHARM part number for '{arg}' on this car."
    head = "\n".join(f"{pn}  {op}  {price or ''}" for pn, op, price in rows[:6])
    return head + "\n\n" + PS.sourcing(rows[0][0], cfg=ps_cfg)["text"]
```

`sourcing()` returns a dict: `text` (ready to show), `interchange` (fits), `links` (list of `Link` with `tier`, `label`, `url`, `verified`), `yards` (a `YardReport` or None). It takes a few ms.

## 3. QR dialog

`PSQR.qr_png(url, path)` writes a PNG (segno, offline). Show one per tier in a `QDialog` with `QLabel.setPixmap`. Cache the PNGs in `app/cache/qr/` keyed by URL hash. If `PSQR.available()` is False, show the URLs as text and say `pip install segno`.

## 4. Printout (Repair SOP / finalize)

```python
results = [PS.sourcing(pn, cfg=ps_cfg) for pn in case_part_numbers]
html = PSSHEET.render(results, f"{case.year} Subaru {case.model}", case.title)
open(path, "w").write(html)        # inline SVG QR codes, no network; print from Chromium
```

This covers the parts block of the "printout is the product" sheet (Gemini review C1). Its CHARM list prices are the ones printed in CHARM, which are old.

## 5. Optional SYNC YARDS button

Only when online. Run it in a subprocess so the GUI doesn't block:

```python
subprocess.Popen([sys.executable, "-m", "parts_sourcing.yard_sync"], cwd=PS_DIR)
```

Usually the PC refreshes `yards.db` (daily is plenty) and it gets copied to the deck with `kb/`.

## Config keys to add to `app/config.json`

- `parts_sourcing_dir`: default `~/open-diag-cyberdeck/parts_sourcing`
