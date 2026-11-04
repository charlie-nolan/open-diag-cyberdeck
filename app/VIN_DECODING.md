# Which car is it, and which database loads

Code: `app/vehicle_id.py` (decode) and Intake in `app/cyberdeck_gui.py`. Written 2026-10-18. Tested offscreen with fake cars; not yet tested on a real car.

## One database per make
- Each make has its own file in `kb/`: `subaru.db` now, and later `chevrolet.db`, `toyota.db` and so on. A few alternate names are also accepted, for example `chevy.db` or `vw.db`.
- Whenever the make changes, Intake loads that make's database. That happens at startup (the last car's make), when you pick a Make, when you type a VIN, or when READ CAR identifies the car.
- If a make has no database, Intake says so in orange: "No database for Toyota yet (toyota.db is not in ~/open-diag-cyberdeck/kb). Codes, chat and verdict still work, without manual pages, specs or parts." The Parts tab and `/part` give the same message.
- The Model list comes from that make's database. A make without a database gets a box where you type the model.
- The prompts no longer say "Subaru". The car named in the case is the one the models work on.

## How the car is identified (read-only)
1. **VIN over OBD-II (OBDLink EX).** The deck asks every car for Mode 09 PID 02, whatever the model year. It's required from 2005 on, and many older cars answer it too.
2. **SSM reply (through the same OBDLink EX; the bare KKL cable is only a fallback).** Only Subaru ECUs answer the 0xBF init, so any reply means the car is a Subaru, and `subaru.db` loads. The SYS ID gives the engine (for example A11001 = 2.5L SOHC). SSM does not carry a VIN on these older ECUs.
3. **Hint only.** If no VIN comes back, the deck shows the protocol and OBD standard, e.g. "J1850 PWM: usually Ford or Mazda; OBD-II as defined by the CARB", and asks for the VIN.
4. **Manual entry.** Type the VIN, or pick Year / Make / Model.

When you type a VIN for a car that doesn't report one, it's saved in `cache/ecu_vins.json` against the ECU's ROM ID or calibration ID. The next READ CAR on that car fills it in by itself.

Caveat: the ROM ID identifies the ECU's software, not the individual car. Two cars with the same ECU software would get the same saved VIN. The deck says when it uses a saved VIN, so you can correct it.

## VIN decoding today (built in)
- **Make:** from the first 3 characters, using a built-in table that covers the common US-market makers.
- **Year:** from character 10. This works for every make.
- **Model:** filled in only for Subaru, from the chassis code in characters 4-5 (SF/SG = Forester, GD/GG = Impreza, and so on). Legacy and Outback share codes, so you pick those two yourself.
- **Check digit:** a mismatch is shown as a warning, not an error.

## Every make (planned): NHTSA vPIC on the PC
NHTSA's vPIC database decodes any 17-character VIN from 1981 on: make, model, year and engine. It's a free download. The cloud sandbox can't reach NHTSA, so the build has to run on the user's PC.

1. Download the vPIC "standalone" database from NHTSA (vpic.nhtsa.dot.gov → Downloads). It ships as a Microsoft SQL Server backup (`.bak`).
2. Restore it in SQL Server for Linux (the free Docker image `mcr.microsoft.com/mssql/server`). Then export to SQLite:
   - **Step 1, small:** the `Wmi` and `Make` tables (joined through `Wmi_Make`) become `kb/vin.db`, with a table `wmi(wmi TEXT PRIMARY KEY, make TEXT)`. The app already reads this. Once it's there, every make is recognised.
   - **Step 2, full decode:** model and engine also need the `Pattern`, `Element` and `VinSchema` tables, plus a port of NHTSA's `spVinDecode` matching logic. That's a few days of work, and it's the reason step 1 comes first.
3. Don't use `cyberdeck_forge/build_out/vin_decoder.db`. Its `vehicle_catalog` is fabricated.

NHTSA data is public domain, so `vin.db` can ship with the open-source code.
