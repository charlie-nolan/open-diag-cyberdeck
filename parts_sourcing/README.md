# Parts sourcing: OEM part number to QR links + junkyard interchange

Two things for the deck, both offline once set up:

1. **QR sourcing links.** Give it an OEM part number and it builds one link per source (Subaru dealer, RockAuto, O'Reilly, NAPA, AutoZone, eBay, car-part.com) and draws QR codes for them. Your phone shows the live price, so the deck never scrapes prices. This is the three-tier idea from your Gemini chats (new OEM, aftermarket, used).
2. **Pick-and-pull interchange.** For the same part number it works out which Subarus carry it (from the Operation CHARM parts pages in `kb/subaru.db`), then counts the matching cars currently set at each Pick-n-Pull around Seattle.

## Seattle-area yards

| Yard | Where | Online stock |
|---|---|---|
| Pick-n-Pull Lynnwood | 18306 Hwy 99, Lynnwood | Yes, via Row52 (Row52 LocationId 54), with VIN, row and date set |
| Pick-n-Pull Arlington | Arlington | Yes, via Row52 |
| Pick-n-Pull Tacoma | 2416 112th St S, Lakewood | Yes, via Row52 |
| Pick-n-Pull Tumwater | Tumwater | Yes, via Row52 |
| Braven Auto & Metals | 2315 N Machias Rd, Lake Stevens | Has a "Parts Search" page (JavaScript), not read yet |
| Yank-A-Part | 23719 Stottlemeyer Rd NE, Poulsbo | Its inventory goes through car-part.com (pulled parts, not cars) |
| Dick's U-Pull-It | 19708 Mountain Hwy E, Spanaway | Its site lists test rows only, so call them |

**Your own list (how yard counts work now):** browse Row52 or Pick-n-Pull yourself (filter Make = Subaru per yard), select the results, copy, and paste them in:

```
xclip -o | python3 -m parts_sourcing.yard_paste - --yard "PICK-n-PULL Lynnwood" --full     # or a saved .txt file
python3 -m parts_sourcing.yard_paste lynnwood.txt --dry-run                              # just show what it read
```

`--full` means "this is everything at that yard right now", so cars missing from it drop out. Without `--full`, cars are only added. Any car that hasn't been confirmed by a full list stops counting 45 days after it was set. Hand-typed lines like `2004 Subaru Forester row 12` work too. VINs are decoded through NHTSA's public vPIC API to get the engine. Row52 alert emails feed the same list:

```
python3 -m parts_sourcing.alert_mail --file alert.txt --dry-run    # a pasted or saved alert, no mailbox
python3 -m parts_sourcing.alert_mail                               # your mailbox, Row52 alerts from the last 60 days
```

For the mailbox, turn on IMAP in Gmail, make an app password, save it in `~/.config/subaru_cyberdeck/mail_app_password` (`chmod 600`), and put your address in `sourcing.json` under `alert_mail.user`. Emails are opened read-only and each is used once. Alert cars have no VIN, so the model and year decide the engine.

**Automatic crawl is off:** Row52's Terms ([row52.com/About/Terms](https://row52.com/About/Terms)) forbid robots and scrapers, so `yard_sync.py` refuses to run until `row52_crawl_allowed` is set in `sourcing.json`. Only set it if Row52 gives written permission. Until then the sheet carries a Row52 QR code you open yourself, plus the fit list of models, years and engines to look for.

When allowed, `yard_sync.py` searches Row52 by ZIP and radius (default 98101, 100 miles), so it finds whichever Pick-n-Pull yards are in range by itself. The 100-mile radius returned about 2,650 cars on Oct 16.

## Junkyard price quote

Select Pick-n-Pull's price table on their website, copy it into a text file and load it once (the Oct 16 list is already in `data/`):

```
python3 -m parts_sourcing.junk_quote load data/pick_n_pull_prices_2026-10-16.tsv --dry-run   # check it read every line
python3 -m parts_sourcing.junk_quote load data/pick_n_pull_prices_2026-10-16.tsv
python3 -m parts_sourcing.junk_quote quote 23700AA62A             # yard price + CHARM book hours x labor_rate
python3 -m parts_sourcing.junk_quote quote --job "alternator"     # same, finding the part number for your car
```

The table's columns (Item #, Description, Type, Price, Core Deposit) are read as columns. The part is matched by its CHARM name, with Pick-n-Pull's abbreviations spelled out (CYL, TRANS, MCPHER, C.V. and so on) and "Car" rows preferred over "Pick-Up/Van". A gasket doesn't match "MANIFOLD EXHAUST". If a part lands on the wrong line, pin it in `sourcing.json`, e.g. `"price_aliases": {"axle shaft": "AXLE C.V."}` (words in the CHARM name, then the price list line).

Labor is CHARM's book time to replace that part on the car in `sourcing.json`, times `labor_rate` (default $100/h). The core deposit is shown separately, `sales_tax` defaults to 0, and since Pick-n-Pull's list has no admission line, set `"admission": 3.00` (or whatever your yard charges) to include it. `python3 -m parts_sourcing <part>` includes the quote once a price list is loaded.

## How a car gets counted

For each Subaru in a yard (plus Saab 9-2X, which is a rebadged Impreza):

| Level | Meaning |
|---|---|
| **likely** | Same model and year as a vehicle that CHARM lists this exact part number for, and the engine matches. The engine comes from decoding the car's VIN with NHTSA vPIC, or the model only had one engine that year |
| **same family** | As above, but CHARM lists a different revision of the number. Subaru revisions share the first 9 characters (13028AA21A/B/C). Supersession isn't in letter order: 13028AA21C is replaced by ...21B. Check before buying |
| **possible** | Same model and year, but the year had several engines and the VIN didn't decode. Also used for the Saab 9-2X, and for a 1995-99 "Legacy" that may really be an Outback |

Cars whose decoded engine differs (for example a WRX 2.0 turbo against a 2.5 SOHC part) aren't counted. CHARM covers 1990-2012, so newer cars aren't rated. CHARM's parts pages don't list every part for every car, so a missing match doesn't mean the part won't fit. The hand-checked extras live in `parts_sourcing/interchange_overrides.json`.

## Use

```
cd ~/subaru_cyberdeck/parts_sourcing
pip install segno                                    # QR codes (pure Python, also on the Pi)
python3 -m parts_sourcing.yard_sync --probe          # online: fetch one Row52 page, show what was parsed
python3 -m parts_sourcing.yard_sync                  # online: refresh kb/yards.db (about 90 pages, 3 s apart, plus VIN decodes)
python3 -m parts_sourcing 14038AA000                 # offline: fitment, links, yard counts
python3 -m parts_sourcing --job "exhaust manifold gasket"    # part numbers for the car in sourcing.json
python3 -m parts_sourcing 14038AA000 13028AA21C --sheet sourcing.html   # printable sheet with QR codes
python3 -m parts_sourcing 14038AA000 --qr            # QR codes in the terminal
```

Settings are in `parts_sourcing/sourcing.json` (home ZIP, radius, DB paths, the car). Sources are in `parts_sourcing/sites.json`. Links marked `"verified": false` (RockAuto, O'Reilly, NAPA, AutoZone) follow each site's usual search URL but weren't opened from here, because the cloud sandbox can't reach them. Scan each one once and flip the flag, or fix the template.

Yard cars turn over in 30-45 days. The report gives the age of the last sync and tells you to re-sync before driving out once it's over 3 days old.

## Files

- `parts_sourcing/interchange.py`: CHARM fitment lookup, part families, `parts_for_job()`
- `parts_sourcing/yards.py`: `yards.db` schema, per-yard matching and report
- `parts_sourcing/yard_sync.py`: Row52 crawler + NHTSA vPIC VIN decode (the only part that goes online)
- `parts_sourcing/qr.py`, `sheet.py`: links, QR codes, printable HTML sheet
- `tests/`: offline tests on a made-up mini database (`python3 -m unittest discover -s tests`)
- `INTEGRATION.md`: how the deck app calls it
