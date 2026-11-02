"""CLI:
    python3 -m parts_sourcing 14038AA000 13028AA21C        # text: fitment, links, yard counts
    python3 -m parts_sourcing --job "exhaust manifold gasket"   # find part numbers for the configured car
    python3 -m parts_sourcing 14038AA000 --sheet sheet.html     # printable sheet with QR codes
    python3 -m parts_sourcing 14038AA000 --qr                   # QR codes in the terminal
"""
import argparse
import sys

from . import parts_for_job, qr, settings, sourcing
from .sheet import render


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("parts", nargs="*", help="OEM part numbers")
    ap.add_argument("--job", help="look up part numbers for a job on the configured car")
    ap.add_argument("--sheet", help="write a printable HTML sheet here")
    ap.add_argument("--qr", action="store_true", help="print each tier's first QR in the terminal")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = settings(a.config)
    car = cfg.get("car", {})
    parts = list(a.parts)
    if a.job:
        rows = parts_for_job(cfg["kb_db"], a.job, car.get("model"), car.get("year"))
        print(f"Part numbers for '{a.job}' on {car.get('year')} {car.get('model')} (CHARM):")
        for pn, op, price in rows:
            print(f"  {pn}  {op}{'  ' + price if price else ''}")
        if not parts:
            parts = [r[0] for r in rows[:1]]
    if not parts:
        ap.error("give a part number or --job")
    results = [sourcing(p, cfg=cfg) for p in parts]
    for s in results:
        print("\n" + s["text"])
        if a.qr:
            seen = set()
            for ln in s["links"]:
                if ln.tier not in seen:
                    seen.add(ln.tier)
                    print(f"\n{ln.tier_label}: {ln.label}\n{qr.qr_terminal(ln.url) or '(pip install segno for QR)'}")
    if a.sheet:
        with open(a.sheet, "w") as f:
            f.write(render(results, f"{car.get('year', '')} {car.get('make', '')} {car.get('model', '')}".strip()))
        print(f"\nsheet: {a.sheet}" + ("" if qr.available() else " (no QR codes: pip install segno)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
