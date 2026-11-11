# SSM2 tables for the 2002 Forester (and other 1999+ Subarus)

Built 2026-10-16 from FreeSSM (Comer352L, GPL-3, https://github.com/Comer352L/FreeSSM) and checked against the RomRaider data already in `kb/subaru.db`.

## What exists online, and what doesn't

| Data | Exists for the 2002 Forester? | Where it lives now |
|---|---|---|
| Live-data addresses and scaling (RPM, trims, O2, roughness, etc.) | Yes. They're generic SSM2, not tied to one ROM. | Already in `kb/subaru.db` → `ssm_param` (RomRaider v370). FreeSSM's 223 entries all match addresses already there. |
| DTC bit addresses (which byte and bit = P0303) | Yes | Already in `kb/subaru.db` → `dtc_ssm`. FreeSSM agrees on all 537 overlapping codes, with no conflicts. Note that kb bits are 0-based and FreeSSM bits are 1-based. |
| **Which of those your ECU actually supports** | Yes. The ECU reports it in the init reply's capability bytes. | **New:** `ssm2_capabilities.py` + `freessm_ssm2_tables.json`. kb's import dropped this gating. |
| Switches (A/C, neutral, test-mode connector, O2 rich flags…) | Yes | **New:** in the JSON, 172 entries. |
| Actuator tests (fuel pump relay, fan, purge valve, EGR…) and adjustments (idle and timing trims) | Yes, gated by capability bits | In the JSON, **reference only**. Nothing in this folder sends them. |
| ROM tables (fuel and timing maps for tuning) | **No.** The public RomRaider/EcuFlash definitions (SubaruDefs) have no 2002 USDM 2.5 NA ROM. The earliest NA Forester is 2005 (32-bit SH7058); 2002 is an older 16-bit ECU. | Not needed for diagnosis anyway. |

**Your ECU ID is confirmed.** FreeSSM decodes SYS ID `A1 10 01` as "2.5L SOHC", which is your EJ251. So the October read was a real ECU answer: `A110012312` = SYS ID `A11001` + the first two ROM ID bytes `23 12`. The last three ROM ID bytes and the capability bytes have never been captured.

## The practical win

The app's OBD tab uses python-obd, which needs an OBDLink EX. With `ssm2_capabilities.py`, the **bare KKL cable can now read your trouble codes**: current and memorized P0303/P0304, read straight from addresses `0xA1`/`0xC1` with 0xA8 reads. It also reads the switches and lists exactly which live values your ECU supports.

## Files

- `ssm2_capabilities.py` has three parts:
  - `ECUCaps(init_reply)`: decodes SYS/ROM ID and flag bytes into the supported live data, switches, DTC blocks and test-mode flags.
  - `read_dtcs(poller, caps)` and `read_switches(poller, caps)`: use `TelemetryPoller` from `telemetry_logger.py` and send only 0xBF/0xA8.
  - `scale()`: FreeSSM's left-to-right formula rules.
- `freessm_ssm2_tables.json`: measuring_blocks 223, switches 172, dtc_obd 539, dtc_subaru 79, cruise_cancel_codes 32, actuator_tests_REFERENCE_ONLY 21, adjustments_REFERENCE_ONLY 21.

Tested against a simulated ECU (SYS ID A11001, P0303+P0304 set): it decoded 67 live values and both codes as current and memorized, and the sim asserted that no non-read command was sent. **Not yet run on the car.**

## First run on the car (KKL cable, key ON)

```bash
cd ~/open-diag-cyberdeck/app/ssm
python3 ssm2_capabilities.py --live     # saves ecu_init_reply.hex, prints supported data + DTCs + switches
```

Send back `ecu_init_reply.hex`. That one line pins down exactly what your ECU supports, including whether the roughness (misfire) monitors at 0xCE/0xCF/0xD8/0xD9 are real on a 2002. Later it can be decoded offline with `--init-hex`.

## Actuator tests and adjustments

They work by writing to ECU memory: test mode connector plus 0xB8 writes to 0x89/0x8A bits. Clear Memory is a write of 0x40 to 0x60. Adjustments permanently change idle and timing trims. A wrong bit can run the fuel pump or fan with nobody watching, so the deck is read-only by default.

Actuator tests (and clear memory) run only on the deck's TESTS screen, behind interlocks: TEST MODE off at every launch, only the tests this ECU advertises in its init reply, test connector in and engine off, confirm each test, a 30 s auto-stop and a STOP ALL that never asks (see `app/README.md`). They have been run on a 2002 Forester in test mode. Adjustments stay refused in code; if you ever need them, run FreeSSM itself on the desktop.

License note: the JSON is derived from GPL-3 code. That's fine for personal use; if the deck is ever distributed, it would need to be GPL-compatible.
