# Open Diagnostic Cyberdeck

A self-contained car diagnostic deck built on an Orange Pi 5. Plug it into a car and it works out what the car
is, reads codes and live data, walks you through the factory diagnostic chart one step at a time, suggests
likely causes, quotes parts and labor, and **cites its source for every step, spec and price**. Everything runs
offline on the deck: local Qwen3 models on the RK3588 NPU, and service-manual knowledge bases stored as
SQLite. When a bigger PC is reachable (over Tailscale, say from a phone hotspot), the deck can hand the
thinking to much larger models running there.

It started on a 2002 Subaru Forester and a 2003 Chevy Tahoe, but nothing is hard-coded for either. There is one
knowledge database per make (`kb/subaru.db`, `kb/chevrolet.db`, ...), and builders exist for 50+ makes.

> **This repository is code only.** It contains no service manuals, no CHARM content, no built databases, no
> scraped forum text and no model weights. The builders in `kb/` make the databases from sources you have the
> right to use. Want to build one yourself? **[docs/BUILD_GUIDE.md](docs/BUILD_GUIDE.md)** walks through it
> from parts list to first scan.

## Why it's more than a code reader

A basic OBD-II scanner tells you a code and maybe a definition. This deck gives the models **the car's live engine
data and all of the databases at the same time**: the factory diagnostic tree, specs, owner-confirmed fixes, TSBs and
complaints, plus what you saw, heard and smelled at intake. That combination is where the deep, useful diagnosis comes
from, especially for faults that are hidden or a bit obscure.

For example, it can read the fuel trims at idle, at cruise and under load and compare them with the O2 sensor
readings, timing advance, MAF/MAP and coolant temperature. That separates a vacuum leak (trims high at idle, normal
under load) from a weak fuel pump or a dirty MAF (the opposite), or catches a sensor that's reading plausibly but wrong.
All of that sits alongside the codes and the manual's own test steps. Python works out what each reading means before
the model sees it, and every conclusion still has to cite its source.

The Chat screen is also useful as a sounding board, like talking the problem over with a mechanic. Throw out your own
theory ("could it be the coil pack?") and it checks the idea against the live data, the factory manual and the
community fixes from Reddit and the forums. It tells you what fits, what doesn't and the quickest test to settle it,
instead of just agreeing with you.

In the author's use this goes well past what a simple OBD-II scanner shows. It's still a diagnostic aid, though:
confirm with the manual's tests before you replace parts.

## Built to stay grounded, not to guess

The usual fear with AI is that it confidently makes things up. This project was designed from top to bottom around
that problem:

- **The models answer from the databases, not from memory.** Every question first goes through retrieval over the
  make's knowledge base (manual pages, diagnostic trees, spec tables, cards), and the prompts require the answer to
  come from what was retrieved.
- **The manual comes first and wins.** The factory diagnostic tree is checked first, then the baked card, then other
  manual pages. Community fixes are used only when owners confirmed them, and forum ideas are labelled "ideas only".
- **Every step, spec and price cites its source**, so you can open the page and check it yourself.
- **Python does the exact work.** Diagnostic-tree questions about the codes you scanned, and the meaning of each
  live reading, are worked out in code before the model sees them, so the model doesn't do the arithmetic.
- **Embeddings find the right page** even when your wording doesn't match the manual's. That means the model isn't
  left to fill gaps on its own.
- **Known-bad data is kept out.** Fabricated command tables and unverified torque values were found and removed.
  The deck refuses any ECU write except the documented, reversible tests.
- **You can always skip the AI and read the manual yourself.** Typing `/fsm <words>`, `/torque <part>` or
  `/fluids <system>` in Chat returns the manual's own text and spec rows straight from the database, with no model
  involved. `shopdeck` walks the factory diagnostic charts with no LLM at all.

No language model is perfect. But here an answer has to point at a real source, so an unsupported claim is easy to spot
and doesn't quietly turn into a repair step.

---

## What it does

### 1. Identify the car
- Reads the VIN over OBD-II (mode 09), or takes a typed one.
- If there's no VIN (common before 2005), it identifies Subarus from their SSM2 ECU ID through a RomRaider-derived
  ECU table (`kb/romid.db`), and remembers the VIN you type for that ECU next time.
- Decodes the VIN offline with NHTSA's vPIC database (optional local PostgreSQL copy), with a cached online
  fallback. Then it loads that make's knowledge DB, scoped to the right model, year and engine.
- See `app/vehicle_id.py` and `app/VIN_DECODING.md`.

### 2. Read the car
| Cable | What it gets |
|---|---|
| **OBDLink EX** | Generic OBD-II on any 1996+ car: stored and pending codes, **every mode 01 PID the car says it supports** (O2 sensors included), mode 06 test results, VIN. It also talks Subaru SSM2 through the same cable, and GM mode 22 extended PIDs (`chevrolet/gm_live.py`). |
| **Plain KKL cable** | Subaru SSM2 on the K-line (4800 baud): the ECU's own DTC table, the switches, and every live value the ECU advertises in its init-reply capability bits. |

- **TEST DRIVE** repeats the read every 30 s and writes each sample to a crash-safe JSONL drive file.
- Live readings are put into plain words in Python before any model sees them (`app/live_data.py`,
  `app/telemetry_ref.py`), for example "engine still cold" or "trims adding fuel". They're also judged against the
  manual's own specified ranges where the manual has them, and each range quotes the row it came from.
- Closed manufacturer tools (FORScan and others) get an **OPEN <tool>** launcher button that hands over the
  cable. The deck never extracts or copies their internals.

### 3. Diagnose
Two apps share the same databases and can be used independently:

**Home app** (`app/cyberdeck_gui.py`, PyQt6, keyboard-first, landscape or portrait)

| Screen | What it does |
|---|---|
| Intake & Scan | Car, codes, symptoms and notes (typed or dictated), READ CAR, TEST DRIVE, LOAD DRIVE FILE |
| Maintenance | Service-schedule items from the manual for this car and mileage; approved items go on the case |
| Chat | A brief, conversational back-and-forth that already knows the case and is grounded in the factory manual. If you propose a cause, it weighs it on the evidence (what fits, what doesn't, the quickest test) instead of just agreeing. Slash commands: `/torque`, `/fluids`, `/fsm`, `/part`, `/common`, `/chart`, `/debate`, `/wildcard`, `/save` and more |
| Final Diagnosis | Most likely cause, what rules out the rest, and the next test. Multi-tab, with owner-reported ideas listed last and labelled "ideas only". **THINK HARDER** reruns it on a reasoning model that also reviews the first plan |
| Repair Guide | Numbered procedure assembled from manual excerpts, with the manual's pictures shown beside the steps so you can look at the diagram while you read: figures from the factory PDFs are stored on the deck, and CHARM pictures are streamed from the PC's picture server when it's reachable. Specs are always looked up, never generated |
| Parts | OEM part numbers from the make DB, which other models share the part, QR codes for parts-store and junkyard searches, and a junkyard quote (yard price list plus book labor hours) |
| Tests | Two-way tests, locked down (see Safety) |

**diagent** (`python -m diagent gui`), the strict one. It walks the manual's own diagnostic chart step by step. Python
answers any step it can from the scan, checks every number you type against the cited spec, and watches live data
for "until X reaches N" steps. The model can only cite sources it was handed; uncited values and invented steps are
rejected by the harness. Every case ends with an HTML work order. There's a scripted test harness that generates
scenarios across makes and code families (`python -m diagent gen`, `python -m diagent test`).

**From diagnosis straight to parts.** Once there's a verdict or repair, the Parts screen pulls the part names out of
the repair line by itself and looks up the OEM part numbers for that exact car in the make's database. It shows which
other models share each part, then puts up a **QR code per part for each store**: O'Reilly, AutoZone, RockAuto and
Row52 junkyard inventory near your ZIP. Scan one with your phone and you're on that part's search page, ready to
check price and stock. There's also a junkyard quote (yard price list plus book labor hours) and a printable parts sheet
with all the codes. `/part <name or number>` does the same from Chat.

**shopdeck** (`python -m shopdeck`) is a no-LLM chart walker with a terminal UI for use over ssh. It's experimental.

### 4. Where answers come from, in priority order
1. **The factory diagnostic tree** for the code (DTC procedure), with steps that ask "which codes are set?"
   answered from the scan.
2. **Baked DTC cards** (`card_baking/`): one card per trouble code, combining the manual's procedure with real-world
   fixes **scraped from Reddit and owner forums and checked for credibility** before they count:
   - Threads are filtered to likely repair threads. A model then pulls out the car, the code, what fixed it, and
     whether the fix was confirmed: the original poster says it worked, or another owner with the same problem does.
     Suggestions nobody confirmed and unresolved threads don't count as fixes.
   - Every extract must quote the sentence it came from, and a quote that isn't actually in the thread throws the extract out.
   - Causes are ranked by how many owners confirmed them. The card is written with a source id on every claim, then a
     second pass drops any claim its sources don't support (dropped claims are kept for review).
   - Torque values, capacities and part numbers on a card are copied from the manual tables, never written by a model.
3. Other manual pages: specs, connector views, component tests, cross-references.
4. **Community reports**: what owners tested, ruled out and fixed, extracted from forum and Reddit threads
   and linked back to manual sections (`kb/build_community.py`). They're shown as supporting evidence, never as the answer.
5. NHTSA complaints, TSBs, recalls and investigations; StackExchange Q&A; a general automotive knowledge base.

Manual sources: Subaru factory service manuals (PDF), Operation CHARM (most makes, up to the early 2010s) and, optionally,
LEMON Manuals for newer cars. See [the build guide](docs/BUILD_GUIDE.md#where-the-manuals-come-from).

Rules the code follows: no hard-coded symptom-to-cause maps, no canned answers. When there's no source, it shows
nothing rather than guessing. Broken chart steps are shown as the page text with a jump to the page, never filled in.

### 5. Models
| Where | Fast | Deep | Think harder | Search |
|---|---|---|---|---|
| Deck (RKLLM on NPU) | Qwen3-1.7B | Qwen3-4B-Instruct-2507 | Qwen3-4B-Thinking-2507 | Qwen3-Embedding-0.6B |
| Home PC (Ollama, `--home`) | Qwen3.6 35B-A3B | Qwen3-Next 80B-A3B Instruct | Qwen3-Next 80B-A3B Thinking | same |

Home mode falls back to the deck's own models whenever the PC can't be reached. (Speeds quoted anywhere in these docs were measured on an RTX 3070 PC and a 16 GB Orange Pi 5; yours will vary with your hardware.) There's no agent framework:
each model has a fixed job, and Python does the bookkeeping.

### 6. Voice (offline)
"Hey deck" wake word (Vosk, closed phrase list, so shop noise is ignored), dictation into any text box
(whisper.cpp), and spoken answers to quick lookups like "torque for exhaust manifold" (pyttsx3 / espeak-ng /
Piper). Diagnoses and prices stay on screen. See `app/README.md`.

---

## Safety

- **Reading is the default.** READ CAR only sends identify and read requests.
- The only writes are on the **TESTS** screen, behind a TEST MODE switch that starts off on every launch:
  - FreeSSM's Subaru actuator tests. Only the ones this ECU advertises are offered, the test connector must be in,
    the engine must be off, each test asks for confirmation, and each one stops after 30 s.
  - Clear Subaru ECU memory, and OBD-II mode 04 clear codes.
  - STOP ALL, which never asks.
  Anything else is refused in code (`app/ssm/ssm_tests.py`).
- **Never:** flashing, security-access unlocks (UDS $27, GM $AE), adjustments, or commands that aren't documented
  by an open-source project you can check. Please keep it that way in forks.
- This is a hobby project with no warranty (see LICENSE). Check every reading and spec against the factory manual
  before you put a wrench on a car.

---

## Repository map

| Path | What |
|---|---|
| `app/` | Home app, LLM backends (RKLLM ctypes + Ollama), retrieval, live data, voice, theme |
| `app/ssm/` | Subaru SSM2: telemetry logger, capability decoding, tests, cable detection, SSM-through-EX probe |
| `diagent/` | Chart-walking diagnosis app with harness, tools, policy checks and scenario generator |
| `shopdeck/` | No-LLM chart walker (GUI + TUI) |
| `kb/` | Knowledge-base builders, retriever, embedder, telemetry and community builders, tests |
| `card_baking/` | Per-DTC card baker (PC GPU): forum/Reddit fix extraction with confirmation and quote checks, then sourced cards |
| `chevrolet/` | The generic per-make pipeline, make profiles, and GM VPW / mode 22 live reader |
| `parts_sourcing/` | Part-number interchange, QR links, junkyard sync and quotes |
| `tools/` | Full per-make builds, deck sync, CHARM picture server, RKLLM model export, status |
| `cyberdeck_forge/charm_ingest.py` | Reader for a CHARM LMDB archive |
| `build_subaru_diag_db.py` | NHTSA flat files + RomRaider definitions -> sourced DTC/TSB DB |
| `docs/BUILD_GUIDE.md` | Step-by-step build: hardware, OS, models, databases, first scan |

## Quick start (if you already have the hardware and databases)

```bash
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git ~/subaru_cyberdeck
cd ~/subaru_cyberdeck && python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp app/config.example.json app/config.json
cd app && ../venv/bin/python cyberdeck_gui.py            # add --home to use the PC's models
```

The scripts expect the checkout at `~/subaru_cyberdeck`. For everything else, see the
[build guide](docs/BUILD_GUIDE.md).

## Status

Working: car ID, OBD-II reads through the EX on real cars (a 2003 Tahoe on J1850 VPW), SSM2 live data over KKL on a
2002 Forester, both apps on the PC and the deck, Home mode, and builders for 50+ makes.
Not yet proven on a car: the two-way tests, SSM2 through the EX, and the 4B models on the NPU under load.
Planned: a vision pass that reads chart pictures into walkable steps, real-time parts prices, customer work orders,
and more makes' extended live data.

## Tests

```bash
cd kb && ../venv/bin/python -m pytest tests            # telemetry, OBD PID decoding, community refs
cd shopdeck && ../venv/bin/python -m pytest tests
cd parts_sourcing && ../venv/bin/python -m pytest tests
venv/bin/python -m diagent test                          # needs kb/subaru.db + generated scenarios
```

Tests that need data you haven't built are skipped.

## License and credits

GPL-3.0-or-later (see `LICENSE`). `app/ssm/freessm_ssm2_tables.json` and the test commands are derived from
[FreeSSM](https://github.com/Comer352L/FreeSSM) (GPL-3), which is why the whole project is GPL.

Also used, not bundled: RomRaider logger definitions, PCMHammer's PID list (GPL-3), NHTSA vPIC / complaints /
TSBs / recalls (public domain), python-OBD, Rockchip rknn-llm, Qwen3 models (Apache-2.0), Vosk, whisper.cpp,
sqlite-vec.

Not affiliated with Subaru, GM, Rockchip, OBDLink or any parts seller. Trademarks belong to their owners.
