# Design history

*Covers the idea's roots from June 2026, then early October 2026 (first planning chats) to 11 November 2026.*

I started with an offline diagnostic deck for older Subarus, built around an Orange Pi 5, local language models and my own car's known fault as the test case. The deck itself began as a side quest: I wanted a useful reason to run a local-model experiment of mine on that board (see [Where it started](#where-it-started)). Today it is a multi-make tool: one cited SQLite database per make, a two-model app on the deck, bigger models on my PC when it's reachable, and strict rules about sources and car safety. What changed most is how I treat data: after finding that early AI-drafted tables were invented, everything now has to cite a real source, and an empty answer beats a wrong one.

I worked the design through with AI assistants as a sounding board and reference; the direction, constraints and key calls are mine. From Oct 6 to about Oct 15 that was Gemini; I moved the project to Claude when Gemini's usage limits kept dropping me to a much weaker model in the middle of building.

## How to read this

- **Skim first:** "My key calls" and the timeline give the whole story in two screens.
- **Ideas are grouped A to G.** Each one has the same five headings: Origin, Versions, Why it changed, Status, Options considered and rejected.
- **Dates are 2026.** "approx." means I know the rough time but not the day; "undated" means no date is recorded.
- **Data I don't own is described, never quoted.** Factory manual data and paid repair-database content are excluded for copyright; forum posts are summarised, not copied.

## Contents

1. [Where it started](#where-it-started)
1. [The first ten days](#the-first-ten-days)
1. [My key calls](#my-key-calls)
2. [Timeline](#timeline)
3. [Idea-by-idea breakdown](#idea-by-idea-breakdown)
   - [A. Hardware and enclosure](#a-hardware-and-enclosure)
   - [B. Power](#b-power)
   - [C. Vehicle interfaces and protocols](#c-vehicle-interfaces-and-protocols)
   - [D. Safety interlocks](#d-safety-interlocks)
   - [E. Software and UI](#e-software-and-ui)
   - [F. Data, accuracy and the audit](#f-data-accuracy-and-the-audit)
   - [G. Testing](#g-testing)
4. [Dead ends and lessons](#dead-ends-and-lessons)
5. [Key decisions](#key-decisions)
6. [Open questions](#open-questions)

---

## Where it started

*June to October 2026. Dates checked against my chat archive.*

| When | What happened |
|---|---|
| Jun 25–26 | The Orange Pi 5 (16 GB) had sat on a shelf for a long time; I turned it into a ROCKNIX emulator box, which it is very good at. |
| Summer | I'd followed open-weight models for a while but held off running them locally, because my RTX 3070 is a weak card for it. Over the summer, models got much smarter per unit of compute and started fitting on smaller devices. At the same time my engine was out of the car most of the summer, and I was rebuilding around it, buying parts as I went. |
| Sep 24 | Rumours of the next Qwen generation. I wondered whether a model like that could be shrunk to my GPU, or even to the 16 GB Orange Pi. That sent me deep into weights, tokens and how a signal moves through a model's layers. |
| Sep 24 | **"Scent marking" idea.** Human thought isn't pure words; it runs alongside sensory data. Could a model leave breadcrumbs along the paths its signal usually takes, and use them to pre-open the likely path, like opening valves ahead of the flow? A dog that marks the same tree twice a day knows exactly where it is relative to it. I learned this resembles work already being studied (compared to ant pheromone trails). Adding an ID token before every token and retraining seemed messy, and that led me to a hardware-side experiment idea of my own, which I'm keeping private for now. |
| Sep 24 | **Test bench choice.** The Orange Pi 5 suited that experiment: CPU, GPU and NPU on one chip, and slow enough to be easy to observe. But friends were using it as their emulator, and I wasn't going to take that away just for a silly local chat or a side experiment. It had to do something genuinely useful to earn the swap. |
| Oct 6 | **The deck.** Research showed the board is capable of a lot more than I thought. With the car fresh in my mind, I landed on an offline diagnostic tool that could also back mobile mechanic work and give people a free, detailed diagnosis. That same day I finished the engine project and had the car back together before Seattle's rain season set in; until then I'd been getting around by Onewheel and light rail. The deck idea was worth it, so I wiped the emulator. The planning chats below start here. |

**Status of the side experiment:** parked and private; the deck took over.

---

## The first ten days

*Oct 6 to about Oct 15, in Gemini. Dates come from my chat archive. A lot of ideas that later sections date to Oct 16 or after actually started here; this is where they first appear.*

| Date | Idea or turn | What it became |
|---|---|---|
| Oct 6 | A stripped-down model specialised in older Subarus, fed every service manual and the owner forums, running on the Orange Pi. "I don't want a giant model." | The deck's core idea. |
| Oct 7 | Bought a cheap FTDI K-line (KKL) cable that speaks Subaru. | The Subaru cable today (C). |
| Oct 8 | After a chatbot was "certain" my misfire was the coil, then the wires (it was the exhaust gasket), I wanted the tool to **ask me questions in diagnostic-tree fashion** before deciding. | Manual tree first, Python answers scan-only steps (D8, D10). |
| Oct 8 | A planned stack of agents: an intake "secretary" (agent 0), diagnosis agents, a parts agent, a quote agent, plus an alignment agent for camera data and a bolt-spec agent that does the maths. | Pared back two days later; alignment became its own project. |
| Oct 8 | Power: hot-swap Milwaukee packs vs a sled of 18650/21700 cells. | Milwaukee M12 with a cutoff (B). |
| Oct 9 | **"Make it more generic."** I asked for my car's history to be taken out of the scripts and the prompts. | The first version of the no-bias rule I enforced again Oct 18 and Oct 25. |
| Oct 9 | My engine came from a different car, of unknown origin. How do you tell the AI that? | The seed of the "this car probably isn't stock" checks and the factory-condition insight (F). |
| Oct 9–10 | Two mechanics, not "electrical vs mechanical": one replaces parts in order of likelihood, the other is a frugal sceptic who tests for free first. I kept having to correct the assistant on this. | Became `/debate`, only on request (E). |
| Oct 10 | "It can take 5 minutes for all I care. I want it to be accurate and not annoying." | Accuracy over speed became a standing rule. |
| Oct 10 | First full run on the board: the 7B was far too slow and the extra agents added nothing. **Back to one conversational model inside the diagnosis script.** I also suspected the database, not the agents, was making the models act strangely. | No agents (D2) and the data audit (D5), both made formal Oct 16. |
| Oct 10–11 | A hybrid: NPU (RKLLM) models and CPU models on different processors at the same time; a small model for tree logic and a bigger one for the final synthesis. | The fast + deep model pair (D3). |
| Oct 11 | Told the assistant to stop pivoting to each new idea and **synthesise every plan so far into one from the ground up.** | How I've run the project since: ideas go in a list, then get merged. |
| Oct 11 | A breakout box between the cable and the car for circuit testing. | Parked (B, vehicle 12 V side). |
| Oct 12 | **One database file per vehicle** instead of one giant one. | One database per make (D4). |
| Oct 12 | Converting manuals to Markdown: 88 of 1,972 pages in over an hour, and table OCR failing on exactly the diagnostic trees that matter. | Text-layer extraction instead of OCR (F). |
| Oct 12 | A Python GUI with one button each for intake, live chat, the job's instructions and parts, all sharing one case database. | The four-button app (E). |
| Oct 12 | Tested its logic on my own misfire while already knowing the answer. | Real known-fix cases as tests (G). |
| Oct 13 | Work instructions that pull up the manual's pictures for that step. | Pictures read on demand (F). |
| Oct 13 | A fast small model for brainstorming and a 7B for the final diagnosis, with the big model reading the case while I chat. | Chat model + verdict model (E). |
| Oct 13 | Case layout planned on a traveler's notebook insert; keyboard and trackball; a bigger screen mounted vertically; separate buck converters so the screen can't brown out the board. | The enclosure and power plan (A, B). |
| Oct 13 | Two voice modes with different wake words: quick lookups (torque, fluids) and long dictation for intake. | Voice (E). |
| Oct 13 | **Scope: older vehicles, up to about 2012–13.** Newer cars need too many calibrations and closed tools. | Stage 1 Subaru; stage 2 widened to every car's generic OBD-II, still read-only (C). |
| Oct 13 | Scrape forums and Reddit for diagnostic reasoning, not just fixes. | Community knowledge in two tiers (D14). |
| Oct 14 | Modified-car communities (lifted, slammed, swapped, tuned, rally, off-road) as accelerated life tests; and owners who bought a car from someone who lied about it. | The failure-engineering idea (F). |
| Oct 14 | "I want this to be for all cars." | Stage 2 (D1). |
| Oct 14 | Switched the board's OS to Armbian. | Broke NVMe boot; settled on Ubuntu (A). |
| ~Oct 15 | Moved the project to Claude. | The Oct 16 triage and reset. |

**Side ideas from later Gemini chats (parked):** reading make, model and year from the OBD port alone (Oct 21, now the VIN and ECU-ID detect order); a Flipper Zero or Pi Zero 2 W as a helper board (Oct 24); old Pixel phones and their AI chips as the processor, or the app on Android (Oct 30); a list of 20 alternative boards because Orange Pis got expensive (Nov 5); squeezing the RKLLM models down the way someone fit a storytelling model onto an ESP32-S3 (Nov 6).

---

## My key calls

1. **Subaru first, but nothing hard-coded for my car (Oct 16, restated Oct 18).** My 2002 Forester is the beta car; stage 1 is all Subarus, stage 2 all cars.
2. **No agent stack on the deck (first Oct 10, made formal Oct 16).** I turned down a seven-agent design for two models in a plain GUI, with Python doing the exact work.
3. **I spotted the placeholder data (Oct 14).** "None of my databases have any information in them" and a block of "reality override" text in the output started the audit that threw out the invented tables.
4. **I called out bias (first Oct 9, again Oct 18 and Oct 25).** My car's misfire and gasket fault had leaked into prompts, hint text and example lists; I made "no examples from my car, anywhere" a standing rule.
5. **Bake the heavy work ahead of time (Oct 16).** A question about how Game Boy games fit in tiny memory became the per-code cards built on the PC overnight.
6. **Read-only by default; two-way tests only behind interlocks (Oct 14, approved Oct 19), and they work on my car.** I ported the Subaru actuator-test logic from the open-source FreeSSM (GPL-3, C++) into Python, then added what FreeSSM doesn't have: every write is checked against an allowlist built from the ECU's own init reply, plus confirm-each-test, a 30 s auto-stop and a STOP ALL. From the deck I've switched my Forester's fans, relays and pumps on and off in test mode. I rejected a free-form "actuate" command and any model-issued ECU write.
7. **Intake starts empty and has no preset bubbles (Oct 19).** Preset code and symptom chips were my car's history in disguise.
8. **The manual comes first and wins (Oct 19).** Then cards, then other manual pages; chat must test my own theories rather than agree with me (Nov 1).
9. **One database per make, picked from the VIN (idea Oct 12, built Oct 16).** Each make can be rebuilt alone and the deck carries only what it needs.
10. **The "factory-condition" insight (Oct 14–15).** Service manuals assume a stock car; extreme-use communities act like accelerated life tests. That shaped how community data sits next to the manual.
11. **Local over rented compute (Oct 31, Nov 3).** I declined paid APIs and rented GPUs for forum extraction and picked the smaller local model after a blind A/B.
12. **Wheel alignment is its own project (Nov 4).** I split it out so the deck stays focused.
13. **Deck models as strict quoters (approx. Oct 21).** Once the big PC models took over brainstorming, I tuned the on-deck models for low temperature and manual-only answers: boring and repetitive, but exactly what the manual says, which is right for an offline backup.

---

## Timeline

| Date | Stage | What changed | Why |
|---|---|---|---|
| Jun–Sep 24 | Roots | Local-model deep dive; "scent marking" idea and a private experiment idea; Orange Pi 5 picked as the test bench | Wanted a useful job for the board alongside the experiment |
| Oct 6–15 | First ten days (Gemini) | See [The first ten days](#the-first-ten-days) | Moved to Claude ~Oct 15 |
| Oct 6–11 | Concept | Offline deck for older Subarus; my car's fault kept as test data only | Support mobile diagnostic work and show how I build tools |
| Oct 11–14 | Hardware plan | Orange Pi 5, M12 pack, cutoff, two bucks, Apache case, keyboard and trackball; no more purchases until software works | Field use on a tool battery, no touchscreen |
| Oct 13 | First stack | Two Qwen2.5 models, manual converted to Markdown, separate forum DB idea | Small model chats, bigger one decides |
| Oct 14 | Scope creep, then a catch | "All cars", universal-cable idea, failure-engineering DB; I found the DBs empty or placeholder | Ambition outran verified data |
| Oct 14–16 | Hardware scare | Something touched the powered board; NVMe vanished; board wiped | Lost the only copy of the telemetry code (later rebuilt) |
| Oct 16 | Reset | Triage flagged invented data; Subaru-first scope; no agents; Qwen3 1.7B + 4B; one DB per make; first real Subaru knowledge DB | Build on sourced data only |
| Oct 17 | First audit | Independent fresh-eyes audit with break tests | Check the new base before stacking on it |
| Oct 18 | Neutral and searchable | Bias purge; hybrid keyword + vector search; 176 cards baked | My fault had leaked into code; search drives every answer |
| Oct 19 | First run in the car | Both cables tested on the car; intake rebuilt; TESTS screen approved; step-table truncation found | Real use exposed what scripted tests missed |
| Oct 21 | Two-tier compute | Home mode uses big PC models over a private network; all-makes archive build starts | I actually prefer the big models; the deck is the fallback |
| Oct 25 | New workflow | Daily work moves to a coding agent on my PC; roadmap in four phases; diagent test app; manuals reconverted | Faster loop on the machine that holds the data |
| Oct 26 | Look and feel | Dark-only palette with fixed colour meanings | Readable in the field, not flashy |
| Oct 31 | Community data | Two-tier community DB; screen fits portrait and landscape | The card filter threw away ~89% of threads |
| Nov 1–2 | Live data and second car | Read every supported PID + Mode 06; Tahoe live read worked | Generic OBD-II for every car, not just Subaru |
| Nov 2 | Open source | Code released under GPL-3; manual-derived data stays private | Share the method, not data I don't own |
| Nov 3 | Choices | Case sizing; community model A/B; reranker tried and turned off | Measure before adopting |
| Nov 4 | Focus | Alignment split into its own repo; code hardening and CI | Keep the deck repo about diagnosis |
| Nov 7 | Coverage | All 53 make databases built | Stage 2 groundwork done |
| Nov 8 | Trust | Full audit; grounding check on every number; wildcard rules rewritten; battery reader after the deck died on a flat pack | Catch invented values before they reach a repair |
| Nov 10 | Community extraction | Subaru extraction finished; merge stopped mid-run, needs a rerun | I needed the GPU for the deck |

---

## Idea-by-idea breakdown

### A. Hardware and enclosure

#### Compute board and OS
- **Origin:** I already owned an Orange Pi 5 (16 GB); it ran a gaming OS until approx. September.
- **Versions:**
  - v1 (Oct 13): Ubuntu image with an NPU driver too old for the LLM runtime; models on CPU via Ollama, far too slow.
  - v2 (Oct 14–15): attempt at Armbian with a 6.1 kernel; NVMe boot and detection broke.
  - current (approx. Oct 18–19): Ubuntu 24.04 community image, kernel 6.1, NPU driver 0.9.7; RKLLM 1.3.1 runs both models on the NPU.
- **Why it changed:** the LLM runtime needs NPU driver 0.9.7 or newer, and I hate reflashing, so I stopped once one image worked.
- **Status:** Kept.
- **Options considered and rejected:** Raspberry Pi 5 (no NPU, models would fight the app for CPU; fine only in Home mode), Jetson Nano (4 GB shared memory, old software), moving to a newer Orange Pi model (claimed "nothing to rewrite"; false, models are chip-specific).

#### Enclosure and input
- **Origin:** Oct 11–14 plan: Apache 3800 case, Ducky One 2 Mini keyboard, 38 mm trackball, 10.5" 3:2 screen, board cooler, no e-ink.
- **Versions:**
  - v1 (Oct 11–14): Apache 3800, everything inside.
  - v2 (Nov 3): I asked whether the smaller Apache 2800 could work. First answer was "no"; I pushed back on the keyboard size and trackball placement.
  - current (Nov 3): 2800 is workable if the keyboard comes out of its shell (or ~1 mm per side is trimmed), the trackball sits above it, and the M12 pack docks outside. 3800 stays as the no-modification fallback.
- **Why it changed:** I wanted a smaller deck and questioned the first fit check; verified keyboard dimensions showed it was 0.5 mm over, not hopeless.
- **Status:** Decided: the final enclosure is the Apache 2800, modified as above.
- **Options considered and rejected:** touchscreen (the finished deck is keyboard-only), e-ink display.

#### Screen orientation
- **Origin:** Oct 11–14, a portrait-mounted 1920x1280 screen.
- **Versions:** v1 (Oct 11–14) portrait only → current (Oct 31) every app fits landscape **and** portrait at 150% scaling.
- **Why it changed:** I hadn't settled the mount, so the layout had to survive both.
- **Status:** Kept.
- **Options considered and rejected:** none recorded.

### B. Power

#### Tool-battery supply
- **Origin:** Oct 11–14: Milwaukee M12 pack → low-voltage cutoff → two separate buck converters (one for the board, one for the screen).
- **Versions:** v1 (Oct 11–14) as above, cutoff set near 9.6 V in the plan → current (unchanged): M12 → LVD → two bucks.
- **Why it changed:** it didn't; separate bucks avoid ever paralleling two converter outputs, and I tested from wall power first.
- **Status:** Kept. Real cutoff voltage and pack capacity still need measuring.
- **Options considered and rejected:** one shared buck for board and screen.

#### Battery gauge and safe shutdown
- **Origin:** Nov 8, after the deck died mid-session from a flat M12 pack.
- **Versions:** v1 (Nov 8): small microcontroller plus a current/voltage sensor after the cutoff, reporting over USB; a battery % chip in every app; safe shutdown at a conservative per-cell voltage.
- **Why it changed:** n/a, first version; it exists because a sudden power loss risks the SSD and the case notes.
- **Status:** Code written; parts not bought; nothing tested on hardware.
- **Options considered and rejected:** voltage-only reading (replaced by voltage plus current counting with load compensation).

#### Hot-swap and backup power
- **Origin:** Nov 8, same incident.
- **Versions:**
  - v1 (Nov 8): two 3S 18650 packs, each behind an ideal-diode module and its own sensor, so either can be pulled while running.
  - current (Nov 8): a single-cell UPS board on the board's 5 V rail only, to ride through an M12 swap or give time to shut down (15 min cap).
- **Why it changed:** the small UPS covers the real need (swap without a crash) with far fewer parts.
- **Status:** Changed; switchover gap untested.
- **Options considered and rejected:** wiring packs straight in parallel (a full pack dumps into an empty one), a supercapacitor-only buffer (too large for a 10 s swap), 4S packs (would need new bucks and cutoff).

#### Vehicle 12 V side
- **Origin:** Oct 11, breakout-box idea: only probe the car's electrics when needed (no link, network codes, multi-sensor "ghost" codes), e.g. K-line idle voltage and ground drop at the plug.
- **Versions:** v1 (Oct 11) idea only. No version of the deck draws power from the car.
- **Why it changed:** not pursued yet; software came first.
- **Status:** Parked.
- **Options considered and rejected:** none recorded.

### C. Vehicle interfaces and protocols

#### Cables: K-line cable vs OBDLink EX
- **Origin:** Oct 6–14, a bare FTDI K-line (KKL) cable for Subaru, with an OBDLink EX on the parts list.
- **Versions:**
  - v1 (Oct 14): the app's OBD tab used a generic OBD library that needs an ELM-type adapter; it could never work on the bare KKL cable.
  - v2 (Oct 19, on the car): KKL reads full Subaru live data and ECU ID; the EX does generic OBD-II and was the only one that cleared Subaru codes; neither returned a VIN on the 2002 (normal pre-2005).
  - current (Oct 19 onward): each job uses the cable that actually worked; ports are picked by stable serial ID after both cables grabbed the same port.
- **Why it changed:** the car test showed which cable does what.
- **Status:** Kept. Subaru protocol over the EX researched but unproven.
- **Options considered and rejected:** a single "universal" cable for every make's enhanced protocol (doesn't exist cheaply); J2534 tools on the EX (it isn't a J2534 device).

#### Subaru live data (SSM2 over K-line)
- **Origin:** Oct 6–15, a telemetry logger for the 2002 Forester at 4800 baud.
- **Versions:**
  - v1 (Oct): logger with several wrong memory addresses and an invented header and baud switch.
  - v2 (Oct 16): rebuilt from saved chats after the board wipe; addresses corrected against the open RomRaider definitions.
  - current (approx. Oct 19–25): the ECU's own init reply says which values and tests it supports, so no per-ROM table is needed.
- **Why it changed:** an alarming "-22% long-term fuel trim" turned out to be atmospheric pressure read from the wrong address.
- **Status:** Kept.
- **Options considered and rejected:** importing closed or standalone tools' data (no API); fabricated command tables.

#### Generic OBD-II and newer cars
- **Origin:** Oct 19, generic OBD-II through the EX for non-Subaru cars.
- **Versions:**
  - v1 (Oct 19): 12 fixed PIDs.
  - v2 (Nov 1): every PID the car says it supports, including O2 sensors, plus Mode 06 test results.
  - v3 (Nov 3): open, CC-BY-SA signal definitions for extended CAN PIDs (read-only) across 36 makes; GM-specific read-only data list for the Tahoe.
  - current: one front door for the EX that searches protocols, retries and remembers the last good one (the Tahoe needed a manual protocol pick).
- **Why it changed:** stage 2 means every car, and fixed PIDs missed what each car offers.
- **Status:** Kept. Extended CAN reads untested on a real CAN car.
- **Options considered and rejected:** anything needing security access or undocumented OEM IDs. Mode 08 (the one standard two-way service) was noted as rare and left unbuilt.

#### Vehicle ID and the "universal launcher"
- **Origin:** Oct 14: plug in, read the VIN, auto-launch the right make's software.
- **Versions:**
  - v1 (Oct 14): auto-launch closed tools and drive them.
  - v2 (Oct 16): VIN decode picks the make's database and protocol module inside the app.
  - current (approx. Oct 19–21): detect order is OBD VIN, then Subaru ECU reply (mapped to model by an open ECU-ID list), then protocol hint, then typed; offline VIN decoder on PC and deck; makes with closed tools get an "OPEN <tool>" button only.
- **Why it changed:** closed tools have no API, and driving or extracting them isn't something I'll do.
- **Status:** Changed.
- **Options considered and rejected:** extracting closed tools' internals, leaked dealer data files.

### D. Safety interlocks

#### Read-only by default
- **Origin:** Oct 10–16, from the first planning chats.
- **Versions:** v1 (Oct 10–16) deck never writes to the car → current: READ CAR sends only identify and read requests; the language model can never send ECU commands.
- **Why it changed:** the earlier assistant suggested letting the model "cut fuel to an injector" and flashing to delete codes; I ruled both out (code deletion by flashing is emissions tampering).
- **Status:** Kept.
- **Options considered and rejected:** a free-form `/actuate` command; autonomous two-way K-line commands.

#### TESTS screen
- **Origin:** Oct 18–19, open-source research showed real, documented Subaru actuator tests exist.
- **Versions:** v1 (Oct 19, approved by me): separate "6. TESTS" screen → current: TEST MODE off at every launch; only tests the ECU advertises; test connector in and engine off; confirm each test; 30 s auto-stop; STOP ALL never asks; also clear Subaru ECU memory and OBD-II clear codes. Everything else is refused in code.
- **Why it changed:** n/a; designed with the interlocks from the start.
- **Status:** Kept. Passes fake-ECU tests, and **works on my Forester**: in test mode I ran the ECU's actuator tests and switched the fans, relays, pumps and the rest on and off from the deck. In practice I rarely use it, because plugging and unplugging the green test connector is a chore.
- **Options considered and rejected:** flashing, security-access unlocks, adjustments, any command not documented by an open-source project.

#### Banned command data
- **Origin:** Oct 16 triage.
- **Versions:** v1 (Oct 16): a cross-make "actuation" table from the early phase flagged as invented (bad checksums, made-up addresses) → current: never sent to any car; excluded from the public repo.
- **Why it changed:** nothing about it could be traced to a source.
- **Status:** Dropped (quarantined).
- **Options considered and rejected:** "fixing" it by hand.

#### Model memory guard
- **Origin:** Oct 19, loading the 4B model froze the deck.
- **Versions:** v1 (Oct 19) unload others when memory is low → current: 4B trial-loads in a child process first, with crash logs; the app never autostarts at boot.
- **Why it changed:** a frozen deck mid-diagnosis is a field failure.
- **Status:** Kept.
- **Options considered and rejected:** autostart on boot (I want to choose when it runs).

### E. Software and UI

#### Agents vs a two-model app
- **Origin:** Oct 10, a seven-role agent stack (router, expert, skeptic, foreman, wildcard, parts, procedure) was proposed.
- **Versions:**
  - v1 (Oct 10–16): four-button GUI (Intake/Scan, Brainstorm/Chat, Finalize Diagnosis, Repair Guide) with two models.
  - v2 (Oct 25): separate test app ("diagent") where a model walks the manual's tree step by step, a human does the checks, every tool is read-only.
  - current: main app stays two-model; diagent stays a test bench.
- **Why it changed:** I wanted to test whether a model can walk a tree honestly without putting an agent in charge of the real tool.
- **Status:** Kept (main app); diagent in testing.
- **Options considered and rejected:** agent frameworks, model routers and gateways, a custom inference stack.

#### On-deck models
- **Origin:** Oct 13, Qwen2.5 3B (chat) + 7B (verdict).
- **Versions:**
  - v1 (Oct 13): Qwen2.5 3B + 7B.
  - v2 (Oct 16): Qwen3-1.7B (fast) + Qwen3-4B-Instruct (deep), 8-bit, on the NPU. A 4B "thinking" model is planned for Think Harder but not exported.
  - current (approx. Oct 21, with Home mode): once brainstorming moved to the big PC models, the deck models' job became strict, manual-bound answers: low temperature (0.1 on the deep model, 0.4 on the fast one), prompts that allow only the case, live data and manual text with real numbers quoted, and a grounding check that flags any spec not on a page in the prompt. (I tried fixed output caps per mode, but they cut answers off mid-sentence, so since Nov 1 the only limit is the context window.)
- **Why it changed:** the 3B was poor at diagnosis; the 7B failed to load on the current runtime; the Qwen3 pair fits 16 GB with the app. Then, with the big models doing the thinking, the deck's job became a dependable offline backup. The answers are repetitive and don't brainstorm, but they say exactly what the manual says to do. That trade-off is intentional.
- **Status:** Kept.
- **Options considered and rejected:** the 1.7B "thinking" as a Think Harder fallback (I said no, Oct 31); several model names the earlier assistant claimed existed and didn't.

#### Home mode (big models on my PC)
- **Origin:** Oct 21: I actually reach for the big models.
- **Versions:** v1 (Oct 21): chat on a 35B model, Final Diagnosis and Think Harder on 80B models, over a private network → v2 (Nov 1): Think Harder is two-pass (plan, then review) → current: falls back to the deck models when the PC is unreachable, rechecked every 60 s.
- **Why it changed:** much better answers whenever the PC is reachable; offline still works.
- **Status:** Kept. Works on the PC; calling it from the deck failed once (Nov 3), deck side still to check.
- **Options considered and rejected:** cloud APIs.

#### Intake
- **Origin:** Oct 11: scan first, auto-fill the car, separate "verified good" and "reused/defective" fields.
- **Versions:** v1 (Oct 11) as above → v2 (Oct 19 morning): preset chips for codes and symptoms → current (Oct 19): every launch starts empty, all fields shown, LOAD LAST CASE is a button; codes and symptoms typed, dictated or filled by READ CAR.
- **Why it changed:** the chips were my own car's history; "there's so many things with cars".
- **Status:** Kept.
- **Options considered and rejected:** auto-loading the last case; preselected codes.

#### Chat and slash commands
- **Origin:** Oct 11–14, my command list.
- **Versions:**
  - v1 (Oct 14): `/debate` gives two opposing arguments only when asked; nothing saves without `/save`; `/torque`, `/fluids` with no model; `/question` with no case context.
  - v2 (Oct 16): `/wildcard` "Dr House" mode: five unlikely-but-possible causes.
  - v3 (Oct 19): chat is short and conversational, manual text only, rebuilt from scratch every turn.
  - v4 (Nov 1): chat adds cards and owner-confirmed fixes; must weigh my own proposed causes (fits, doesn't fit, quickest test).
  - current (Nov 8): wildcard keeps long-shot ideas but every part, pin or number must be cited or marked "not in manual"; no persona, no anecdotes.
- **Why it changed:** I pasted a wildcard answer that invented parts, pins and anecdotes; the rule moved from "be creative" to "creative ideas, cited facts".
- **Status:** Kept.
- **Options considered and rejected:** splitting every message into two answers (muddied the case); `/actuate`.

#### Final Diagnosis
- **Origin:** Oct 10–16, the big model writes the verdict and repair guide.
- **Versions:** v1 (Oct 16) one verdict → current (approx. Oct 31–Nov 1): SUMMARY plus up to five issue tabs and FULL TEXT; full procedure on save; community ideas added last, labelled "ideas only".
- **Why it changed:** one block of text hid the alternatives; I like the tabbed layout.
- **Status:** Kept.
- **Options considered and rejected:** made-up probabilities ("85–90%") from the early phase.

#### Voice
- **Origin:** Oct 7–14: always-on wake word plus dictation; spoken replies for information only, **never for diagnoses**.
- **Versions:** v1 (Oct 7–14) plan → current (Oct 19): offline dictation into whichever box has focus, text-to-speech at a slower rate, listener pauses while the deck speaks.
- **Why it changed:** in the field my hands are dirty; diagnoses stay on screen where I can check sources.
- **Status:** Kept; mic fix unverified on the deck.
- **Options considered and rejected:** an extra speech engine that would compete for the NPU.

#### Look and feel
- **Origin:** Oct 26, my palette.
- **Versions:** v1 (Oct 26) palette with a light-mode toggle → current (same day): dark only; colours always mean one thing (green = your turn, teal = live data, purple = source, amber = out of spec, always with a word, never colour alone); fully keyboard-driven.
- **Why it changed:** I don't want a light mode at all.
- **Status:** Kept.
- **Options considered and rejected:** light mode; flashy UI.

#### Side apps
- **Origin:** various.
- **Versions:**
  - Chat-only app: dropped Oct 20 (the main app covers it).
  - No-model "shopdeck" chart walker and work order: built approx. Oct 18–21, parked Oct 21 as "broken and buggy".
  - Quick Ask (chat over any make DB without intake): built Nov 8.
  - Wheel alignment: an early long-term idea (Oct 7), built as a deck screen (Nov 2), split into its own project (Nov 4).
- **Why it changed:** each either duplicated the main app or belonged in its own project.
- **Status:** mixed (see above).
- **Options considered and rejected:** keeping alignment inside the deck.

### F. Data, accuracy and the audit

#### Data honesty: auditing AI-drafted data
- **Origin:** Oct 14, I noticed my databases were empty and asked whether a dramatic block of output text was a placeholder. It was.
- **Versions:**
  - v1 (Oct 6–14): several databases and scripts drafted with an earlier AI assistant, presented as verified.
  - v2 (Oct 16): triage checked each against the cite-everything rule: "hand-verified" torque rows with no source and fake part numbers, an ECU command table with bad checksums, a fake parts DB, hand-typed "engineering" DBs, wrong live-data addresses, false progress reports.
  - current: all of it excluded; only tables that trace to a real source are used.
- **Why it changed:** a diagnostic tool built on invented data is worse than none.
- **Status:** Kept as a standing rule.
- **Options considered and rejected:** patching the invented rows instead of dropping them.

#### Factory manual conversion
- **Origin:** Oct 12–13: the PC converts manuals once; the deck only looks things up.
- **Versions:**
  - v1 (Oct 12–13): OCR-based converter; ran out of memory, cut table cells, produced gigabytes of junk on two manuals.
  - v2 (Oct 25): text-layer extraction plus ruled-table parsing, no OCR; image-based YES/NO icons matched in code. One manual went from 34 to 185 walkable charts.
  - current (Oct 21 onward): diagrams and spec tables that are pictures are read by a 35B vision model on demand; a 3B vision model was rejected after it invented steps.
- **Why it changed:** cut chart cells meant the diagnostic trees couldn't be walked.
- **Status:** Kept. Manual data itself is excluded from the public repo for copyright.
- **Options considered and rejected:** small vision models for OCR; an "OCR model" the earlier assistant named that doesn't exist.

#### One database per make, and coverage
- **Origin:** Oct 14 "all cars" idea; narrowed Oct 16.
- **Versions:**
  - v1 (Oct 14): all cars at once.
  - v2 (Oct 16): Subaru first, one SQLite file per make, picked by VIN.
  - v3 (Oct 21–26): one resumable script builds any make from an archive of manufacturer service information, NHTSA data and forum fixes.
  - current (Nov 7): 53 make databases; a script brings one specific car up to my Forester's level.
- **Why it changed:** a tool tuned to one car only proves it memorised that car; per-make files keep the deck small.
- **Status:** Kept. Private data: never in the public repo.
- **Options considered and rejected:** one giant database; scraping sites whose terms or robots rules forbid it.

#### Search and retrieval
- **Origin:** Oct 11–13: hybrid keyword + vector search; Oct 13 I picked 768-d embeddings.
- **Versions:**
  - v1 (Oct 16): keyword search only.
  - v2 (Oct 18): keyword + vectors (Qwen3 embedding, 8-bit), DTC procedure first: 25/34 → 31/34 top-3 on my test set.
  - v3 (Oct 29): table-of-contents pass.
  - v4 (Nov 4–8): hard-negative eval over 53 makes (1,490 cases): 75.8% → 80.2% with multi-query, 83.1% with engine; whole split procedures returned together; fast approximate search for big unscoped queries.
  - current: spec lookups never fall back to a loose match.
- **Why it changed:** every answer starts from search; bad search means bad answers.
- **Status:** Kept.
- **Options considered and rejected:** a CPU reranker (Nov 3; 3.6 s minimum and mixed gains, off until it can run on a GPU); comparing vectors across different embedding models.

#### Baked cards
- **Origin:** Oct 16, my Game Boy question: how did those games fit so much into so little?
- **Versions:** v1 (Oct 18): 176 per-code cards for 1998–2002 Foresters, written on the PC from forum fixes, TSBs and the manual → v2 (Oct 31): part numbers masked for all makes → current (Nov 1): 1,686 cards for the 2003 Tahoe; secondary to the manual tree.
- **Why it changed:** card outlines came from truncated manual text, so they stay a backup source only.
- **Status:** Kept. Fine-tuning data prepared, not trained (parked).
- **Options considered and rejected:** assembly or custom NPU kernels (no real gain; the bottleneck is memory bandwidth).

#### Community knowledge (forums and owners)
- **Origin:** Oct 13: a separate forum DB of owner-confirmed fixes only.
- **Versions:**
  - v1 (Oct 16–18): card filter kept ~11% of threads and only "the fix".
  - v2 (Oct 25–31): two tiers: fix-reported threads get full extraction (tests, readings, ruled-out causes); all others become searchable, labelled "unchecked".
  - v3 (Nov 3): blind A/B between a 30B and a 9B local model: the 9B was faster and had fewer quote failures.
  - current (Nov 10): Subaru extraction done; merge into the database stopped mid-run, needs a rerun; Chevy paused.
- **Why it changed:** most useful reasoning was being thrown away.
- **Status:** Changed; in progress.
- **Options considered and rejected:** rented GPUs and paid APIs (too expensive); bypassing Cloudflare or robots blocks; logged-in forum crawling (parked, ban risk).

#### Failure-engineering layer
- **Origin:** Oct 14–15, my idea: manuals assume a factory-condition car; drag racing, rally, delivery routes and similar communities are accelerated life tests with one load path isolated; each upgrade moves the weak point down the drivetrain.
- **Versions:** v1 (Oct 14): the earlier assistant produced "engineering" DBs that turned out to be ~15 hand-typed rows → v2 (Oct 16): a small curated layer with a source on every row ("this car probably isn't stock" checks, parts known to arrive bad, what seizes on teardown).
- **Why it changed:** the first version was fabricated; the second was planned but not built.
- **Status:** Parked. The idea lives on in how community data sits beside the manual.
- **Options considered and rejected:** a mass-download "vault"; datasets that turned out not to exist.

#### Python does the exact work
- **Origin:** Oct 7–11: the model only reasons; code checks every number.
- **Versions:**
  - v1 (Oct 11): Python compares readings to spec.
  - v2 (Oct 19): live readings reach the model already labelled; manual step tables parsed so the model sees real steps, and steps that ask "which codes are set?" are answered from the scan.
  - current (Oct 25–29): live-data zones come from the manual's own tables, each quoting its source row; generic ranges are marked generic.
- **Why it changed:** a model read a normal cold-engine coolant temperature as overheating; and step tables were being cut before any step appeared.
- **Status:** Kept.
- **Options considered and rejected:** letting the model do arithmetic.

#### Grounding check, bias removal and audits
- **Origin:** Oct 17 first independent audit; Oct 18 my bias call-out.
- **Versions:**
  - v1 (Oct 17): audit with break tests, 10 patches.
  - v2 (Oct 18): removed my car from default prompts, hint text and the repair keyword list.
  - v3 (Nov 8): full audit found my car's fault still in a fallback parts list, Subaru-only synonyms applied to every make, Mode 06 leaking between cases; all fixed.
  - current (Nov 8): every spec, part number, price and labour time in an answer must appear on a page in the prompt; 200/200 true values kept, 188/200 invented values flagged.
- **Why it changed:** bias crept back in through quiet defaults, so the check had to be automatic.
- **Status:** Kept.
- **Options considered and rejected:** trusting the model to cite correctly without a check.

#### Parts and pricing
- **Origin:** Oct 7 brand-trust list; Oct 14 junkyard interchange idea.
- **Versions:** v1 (Oct 16–17): parts lookup, QR links to parts sites, junkyard quotes → v2 (Nov 1): OEM part numbers from the service-information archive; a "parts" DB from the early phase was fake and dropped → current: part names extracted from the repair line.
- **Why it changed:** the fake DB couldn't be trusted.
- **Status:** Kept; real prices open (no live price source chosen).
- **Options considered and rejected:** crawling a junkyard inventory site (its terms forbid it); a general web-search link.

### G. Testing

#### Search evals
- **Origin:** Oct 18, a 34-question test set.
- **Versions:** v1 (Oct 18) 34 questions → v2 (Oct 26–31) per-make scores for every make → current (Nov 4) hard-negative eval (similar but wrong pages planted).
- **Why it changed:** questions drawn from a DB's own rows flatter it; hard negatives are fairer.
- **Status:** Kept. It measures search, not diagnosis.
- **Options considered and rejected:** none recorded.

#### Unit tests, break tests and CI
- **Origin:** Oct 17 audit.
- **Versions:** v1 (Oct 17) break tests per fix → current (Nov 4–8) test suite on every push, including an ARM64 job and a vector-search smoke test.
- **Why it changed:** fixes from several workstreams kept overwriting each other.
- **Status:** Kept.
- **Options considered and rejected:** none recorded.

#### Scripted mechanic scenarios
- **Origin:** Oct 25, diagent tests.
- **Versions:** v1 (Oct 25) scripted model, 20–25 cases pass → v2 (Oct 29) first real 35B run 3/25 → current (Oct 30) 24/32 after three fix batches.
- **Why it changed:** scripted passes hid real-model failures (re-asking declined steps, stalling on no-code cases).
- **Status:** In progress.
- **Options considered and rejected:** examples from my car in the briefs (I flagged them as bias, Oct 25).

#### Real-car tests
- **Origin:** Oct 19, first run on the Forester.
- **Versions:** v1 (Oct 19) cables, live data, clear codes on the Forester → v2 (Nov 2) Tahoe live read → v3: TESTS screen on the Forester, all actuator tests worked → current: a full diagnosis on either car still to do.
- **Why it changed:** n/a.
- **Status:** Open.
- **Options considered and rejected:** none.

#### Diagnosis accuracy scorecard
- **Origin:** Oct 7–8, a "close ticket" step recording the actual fix.
- **Versions:** v1 (Oct 7–8) idea → v2 (Oct 25) roadmap phase 2: "deck said vs actual fix" per job → current: not built; an idea to hide the confirmed fix in forum threads and score the top 3 is also unbuilt.
- **Why it changed:** n/a.
- **Status:** Open.
- **Options considered and rejected:** none.

---

## Dead ends and lessons

| Idea | Why it died | Lesson |
|---|---|---|
| Seven-agent stack | Too heavy for the board; hid where answers came from | Give the model the smallest job; code does the rest |
| AI-drafted "verified" data tables | Invented: no sources, bad checksums, fake part numbers | Nothing goes in without a traceable source |
| Universal cable that launches any make's tool | Closed tools have no API; no cheap cable speaks every protocol | Build your own read side; link to closed tools, don't drive them |
| "All cars" from day one | No verified data behind it | Prove one car, then one make, then widen |
| OCR manual conversion | Out of memory, cut table cells, junk output | Use the text layer first; read pictures only where needed |
| Small vision model for charts | Invented steps | A wrong step is worse than a missing one |
| Preset code/symptom chips | Encoded my car's history | Defaults are bias too |
| Qwen2.5 3B/7B pair | 3B weak at diagnosis; 7B wouldn't load | Pick models that fit the runtime you actually have |
| Light-mode toggle | Not wanted | Decide the field conditions, then design |
| Cross-make actuation tables | Fabricated and unsafe | Writes only from documented, open sources, behind interlocks |
| CPU reranker | Slow, mixed results | Measure before adopting |
| Two-pack hot-swap | More parts than the problem needed | Solve the actual failure (ride through a swap) |
| Separate chat-only app | Duplicated the main app | One tool, several modes |
| Wheel alignment inside the deck | Different problem, different hardware | Spin off projects that dilute focus |

---

## Key decisions

- **D1** (Oct 16) Subaru first; my car is test data only; nothing hard-coded for it.
- **D2** (Oct 16) No agents on the deck; two models plus Python.
- **D3** (Oct 16) Qwen3-1.7B (fast) + Qwen3-4B-Instruct (deep), 8-bit on the NPU.
- **D4** (Oct 16) One SQLite database per make, chosen from the VIN.
- **D5** (Oct 16) Exclude every table that can't cite a source.
- **D6** (Oct 14–19) Read-only by default; ECU writes only on the TESTS screen with interlocks.
- **D7** (Oct 16) Heavy work on the PC ahead of time; the deck only looks up.
- **D8** (Oct 19) Source priority: manual tree, then card, then other manual pages, then general knowledge.
- **D9** (Oct 19) Empty beats wrong: no loose-match fallbacks for specs.
- **D10** (Oct 19) Python labels live data and answers scan-only tree steps.
- **D11** (Oct 19) Intake starts empty, no preset chips.
- **D12** (Oct 21) Home mode uses PC models over a private network; deck is the fallback.
- **D23** (approx. Oct 21) Deck models run at low temperature with manual-only prompts; output limited only by the context window (fixed caps dropped Nov 1).
- **D13** (Oct 26) Dark-only UI with fixed colour meanings; keyboard-only.
- **D14** (Oct 31) Community data in two tiers; unchecked threads labelled "ideas only".
- **D15** (Oct 31) No rented GPUs or paid APIs for extraction.
- **D16** (Nov 1) Chat must weigh my own proposed causes, not just agree.
- **D17** (Nov 2) Code public under GPL-3; manual-derived data private.
- **D18** (Nov 3) 9B local model for forum extraction after a blind A/B.
- **D19** (Nov 4) Wheel alignment becomes a separate project.
- **D20** (Nov 8) Automatic grounding check on every number in an answer.
- **D21** (Nov 8) Wildcard: long-shot ideas allowed, facts must be cited.
- **D22** (Nov 8) Battery gauge with safe shutdown; small UPS for pack swaps.

---

## Open questions

- What are the real cutoff voltage and pack capacity, and does the UPS switchover actually hold the board up?
- Should the deck ever take power from the vehicle's 12 V side, and with what protection?
- How accurate are full diagnoses on real jobs? (No scorecard yet.)
- Is the TESTS screen worth making easier to use (the green test connector is a hassle), or is it a rarely needed extra?
- Can the Subaru protocol run reliably over the OBDLink EX, so one cable covers more?
- Do the extended CAN PIDs read correctly on a real CAN car?
- Why did Home mode fail from the deck on Nov 3?
- Should the failure-engineering layer be built, and how to source it honestly?
- Is a proper GM factory manual needed for the Tahoe, since the archive lacks its step tables?
- Fix or retire the no-model chart walker?
- When to finish the community merge for Subaru and run Chevy?
