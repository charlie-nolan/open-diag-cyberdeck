# diagent: manual-led diagnostic agent (test app)

Brief: `../DIAG_AGENT_APP.md`. Separate from `app/` and `shopdeck/`. It reads the kb DBs read-only and
imports `app/procedures.py`, `kb/kb_retriever.py`, `app/live_data.py`, `app/common_issues.py`,
`app/vehicle_id.py` and `app/ssm_reader.py` without changing them.

```sh
cd ~/open-diag-cyberdeck
venv/bin/python -m diagent test                      # scripted fake-the user scenarios, log in logs/diagent/
venv/bin/python -m diagent test --agent pc           # same scenarios with Qwen3.6 35B (only when the GPU is free)
venv/bin/python -m diagent gen [--seed N]            # rewrite the script-chosen scenarios (diagent/scenarios/gen_*.json)
venv/bin/python -m diagent run [--vin V | --make M --model X --year Y] --codes C [--agent auto|pc|off] [--car none|real|x.json]
venv/bin/python -m diagent run --resume 3            # pick a saved case up again
venv/bin/python -m diagent show 3                    # case + audit log
```

**Window (GUI, 2026-10-31):** `venv/bin/python -m diagent gui [same options as run] [--play SCENARIO]`.
`venv/bin/python -m diagent gui --install-icon` adds "Diagent" to the app list (KDE / the deck's menu); it never
autostarts. `diagent/gui.py` runs the same `open_case` + `harness.run` as `run` on a worker thread, with
`GuiHuman` (same calls as `TerminalHuman`) waiting on the window. Start screen (car, codes, saved cases, play a
test scenario), step card with source chips, safety/detour cards, number pad with the unit, live strip with
sparklines, figures (tap to zoom), side panel (path, hypotheses, filterable log, source text), finish screen
with an HTML work order in `logs/diagent/`. Keys: Y/N, D done, C can't, S skip, B back, Esc stop, 1-9 picks,
F3 side panel, F4 speech. Colours come from `app/theme.py` (shared with the Home app); narrow or portrait
windows stack the side panel under the work area and wrap button rows (`app/ui_fit.py`). Not yet: dictation
(the mic) in the window; speech reuses `app/voice.py` speak() when SPEAK is on.

## How it works

- **Car identification first** (`identify.py`): the main app's order, through `app/vehicle_id.py`. That is the
  mode 09 VIN or a typed one; then the SSM2 ECU id -> `kb/romid.db`, with the VIN bound to that ECU; then the
  decode (vin.db / NHTSA cache -> local vPIC -> NHTSA online); then `kb/<make>.db` scoped to the kb's own
  model/engine names (asked when the decode doesn't map), plus the telemetry tables, the ECU capability list
  and earlier visits. Anything unknown is asked, and with no answer no case starts. The main app's
  `cache/ecu_vins.json` and visit table are read only; diagent writes its bindings to `diagent/data/`.
- **Scenarios are chosen by script** (`gen.py`, seeded): DTC families across Subaru FSM models/years (random
  branches, with the readings written from each step's own check), CHARM page-mode cases from other makes,
  a symptom taken from a symptom-chart title, ECU-id identification from romid.db, an unidentifiable car,
  and injected bad calls. `test` also fails if the system prompt or tool descriptions name a DTC, a
  value, a make or a kb model name.

- **Review fixes 2026-10-25** (`DIAGENT_REVIEW_2026-10-25.md`):
  - Charts are split at the manual's own DTC headings, not at the converter's section titles. Only charts
    headed with the case's code are used, and variants whose engine tag (H4SO, H4DOTC, H6DO, TURBO ...)
    contradicts the car's engine are dropped before the user is asked.
  - Engine names are merged: FSM code names join the scope, and CHARM descriptions are the choices.
  - DTC-list steps are answered only from a scan of this case.
  - Every prompt shows the manual's question, and steps are split into one action per prompt with
    scan-tool boilerplate dropped.
  - Read-data steps are read live through `app/telemetry_ref.py` when this car and cable can report the value
    in the check's unit.
  - "done" on a measured step is asked again, and an unconfirmed finish needs another manual path looked at
    first.
  - Page-mode cases step through the page's own instructions (`page_steps`).
  - Symptoms are searched in symptom/trouble charts only.
- **Live telemetry** (`watch.py`, tools `watch_live` / `pathways`, all through `app/telemetry_ref.py`):
  - Every sample goes through `normalize` then `judge` (this car's zones, the condition the readings show).
  - Triggers ("until <value> is above <the manual's number>") are evaluated in Python, with a live strip on
    screen. The trigger value must be cited.
  - Out-of-zone values list the manual's `pathways`. Each session is stored in the case DB's `watch`
    table, and the tests replay it offline through the same path.
- **Review 2 fixes:**
  - An ECU id picks the engine from the ECU list's trim (cylinders taken from the same model's
    other-year descriptions). If the kb has no such engine, it says so and loads all engine sections.
    Market (EDM/JDM vs. the USDM manuals) is flagged.
  - No codes: Python asks the user for the symptom, and `symptom_chart` walks the manual's symptom chart
    (suspects in the manual's order, rank A/B/C, each item's inspection).
  - Card/forum causes the code's own chart never names are flagged and can't become detours.
    `offer_detour` finds the inspection itself or offers nothing.
  - Page mode skips background blocks and starts at the test steps.
  - Identical chart variants are merged, and charts from manuals for other years or other spec codes
    are dropped.
- **Field knowledge** (`field.py`, tools `field_reports` / `offer_detour` / `end_detour`): TSBs/recalls, baked
  cards, kb forum fixes (grouped by component), Reddit and forum threads tagged with the code, and owner
  complaints, all counted and car-matched in Python.
  - A detour needs a manual inspection page that names the component, and the user's yes.
  - The tree position is kept, and a detour's "can't" doesn't touch the tree.
- **Case object** (`case.py`), saved to `diagent/data/cases.db` after every tool call: car, codes, symptoms,
  readings (R ids), live snapshots, procedure position and path, hypothesis board, the source registry
  (M/SP/L/N/SC/F ids), and an audit row for every tool call, rejection and reply.
- **Procedures** (`kb.py`): each linked section is parsed on its own with `procedures.parse`. A restart at step 1
  starts a new variant (for example NA and turbo charts), and the user picks the variant when there is more
  than one. Rows the converter mangled are *broken*, and rows on a page titled for another DTC, or that never
  name the code's component, are *suspect*. Neither kind is walked: the walk stops and the page text is shown.
  "Refer to DTC X" is followed only on pages that say it for the diagnostic procedure.
- **Python owns the walk** (`tools.py`): the user's reply is parsed and unit-checked (`units.py`). If the step's
  check is one plain comparison, Python answers it and follows the manual's Yes/No cell. Steps that ask which
  DTCs are set are answered from the scan. The required ignition/engine state comes from the step text, and
  the user confirms it. A step that needs an actuator test or a clear goes to the main app's TESTS screen.
- **Harness** (`harness.py`): a plain loop over Ollama `/api/chat` with tools. Each turn is the case summary
  plus up to 8 tool calls, which must end in `ask_human` or `finish`. There is also an agent-time cap.
  Instructions, board entries and summaries are checked against this run's tool results: an unknown
  number+unit, step, DTC or id is rejected. A `diagnosis` finish needs a walked path that reached the tree's
  end on real answers; anything else finishes `unconfirmed` with the manual check still needed.
- **Agent off** (`policy.py`): when the PC can't be reached or the GPU is busy, the same harness runs a
  deterministic manual walk with no model, and the screen says so. The deck's NPU models are never used here.

## Known gaps (milestone 1)

- `RealCar.sample` re-reads the whole live line each sample (slow on SSM2); a continuous TelemetryPoller would be
  faster. Not run on a car yet.
- Card check: 98 of 176 Subaru cards have a cause the code's own chart never names (340 of 555 causes),
  list in `logs/diagent/card_cause_check_20261025.txt` (for the card-baking side).
- 2008+ Subaru manuals: many step tables were cut off in the PDF conversion (truncated cells, empty Yes/No),
  so they show as page text. Re-converting those pages (vision OCR) would make them walkable.
- Not yet run with Qwen3.6 35B (GPU busy with the CHARM build on 2026-10-25): `test --agent pc` when free.
- The VIN decode path isn't covered by a scenario (no real VINs in kb/vin.db to pick from); ECU-id and typed paths are.
- Only Subaru FSMs have parsed step tables; every other make (and Subaru EGR codes) runs in page mode.
- `fresh_audit/tests/fake_ecu.py` from the brief doesn't exist on this PC; scenarios use `FakeCar`.
- The 2003 Tahoe is model `K`/`C` (engine `V8-5.3L VIN T`) in chevrolet.db; GM CHARM pages have no step tables,
  so GM runs are page mode.
- Figures resolve to file names; the image folder for FSM figures isn't wired to a path yet.
