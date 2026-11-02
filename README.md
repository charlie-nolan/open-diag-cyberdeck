# Open Diagnostic Cyberdeck

An offline car diagnostic tool for the Orange Pi 5. It reads the car over OBD-II and Subaru SSM2, looks things up in
service-manual databases you build yourself, and runs local Qwen3 models on the RK3588 NPU (RKLLM) to talk
through the diagnosis. Every step, spec and price it shows cites the manual page it came from.

It started with a 2002 Subaru Forester and is growing to every Subaru, then every make: one knowledge database per
make (`kb/subaru.db`, `kb/chevrolet.db`, ...), picked from the VIN.

> **This repository is code only.** No service manuals, no CHARM content, no built databases, no scraped text and
> no models are included. The builders in `kb/` make them from sources you have the right to use.

## What's here

| Folder | What it is |
|---|---|
| `app/` | The deck app (PyQt6): Intake & Scan, Chat, Verdict, Repair Guide, Parts, Tests. Fast + deep Qwen3 models on the NPU, or a bigger PC over Ollama with `--home`. See `app/README.md`. |
| `app/ssm/` | Subaru SSM2 over a KKL cable or an OBDLink EX: live data, DTCs, switches, ECU capability bits. See `app/ssm/README.md`. |
| `diagent/` | A second, stricter diagnosis app: walks the manual's diagnostic chart step by step, Python checks every reading, the model may only cite sources it was given. See `diagent/README.md`. |
| `shopdeck/` | A no-LLM chart walker (GUI and TUI). Experimental. |
| `kb/` | Knowledge-base builders and the retriever: manuals (PDF -> markdown -> sections, specs, DTC procedures), CHARM per make, NHTSA complaints/TSBs/recalls, community threads, telemetry definitions, embeddings (Qwen3-Embedding-0.6B + sqlite-vec). |
| `card_baking/` | Bakes one diagnostic "card" per DTC from the manual plus owner-confirmed fixes, on a PC GPU. |
| `chevrolet/` | The per-make pipeline (`make_pipeline.py`), make profiles, and a GM VPW live-data reader. |
| `parts_sourcing/` | Part-number interchange, store/junkyard QR links and junkyard quotes. |
| `tools/` | PC/deck glue: full per-make builds, pulling finished DBs to the deck, the CHARM picture server, the Qwen3 -> RKLLM exporter. |
| `cyberdeck_forge/charm_ingest.py` | Reader for a CHARM LMDB archive (used by `kb/build_subaru_kb.py`). |

## Safety

- Reading is the default. The only writes the deck will send to a car are on the **TESTS** screen: FreeSSM's
  documented Subaru actuator tests, clearing ECU memory, a mode 04 clear, and STOP ALL. Anything else is refused.
- No flashing, no security-access unlocks, no invented commands. Don't add any.
- This is a hobby project with no warranty (see LICENSE). Check every reading and spec against the factory manual
  before you put a wrench on a car.

## Hardware

- Orange Pi 5 (RK3588S, 16 GB recommended), NVMe, a touch screen. Tested on Ubuntu 24.04 (Rockchip 6.1 kernel,
  RKNPU 0.9.7) with RKLLM runtime 1.3.1.
- Cables: an OBDLink EX (generic OBD-II, and SSM2 through it), or a plain KKL cable for Subaru SSM2 (K-line, 4800 baud).
- Optional: a PC with an NVIDIA GPU for building databases and baking cards, and for "home" mode over Ollama.

## Setup

The scripts still expect the checkout at `~/subaru_cyberdeck` (its original name) on both the deck and the PC, so clone it there.

```bash
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git ~/subaru_cyberdeck
cd ~/subaru_cyberdeck
python3 -m venv venv
venv/bin/pip install -r requirements.txt          # deck
venv/bin/pip install -r requirements-build.txt    # PC (database builders)
cp app/config.example.json app/config.json        # then edit it
```

RKLLM on the deck: build or download `librkllmrt.so` from Rockchip's
[rknn-llm](https://github.com/airockchip/rknn-llm) (1.3.1) and export the models on the PC with
`tools/export_qwen3_rkllm.py` (in its own venv with Rockchip's rkllm-toolkit). The app looks for them in
`models/rkllm/` (paths in `DEFAULT_CONFIG`, `app/cyberdeck_gui.py`).

Run the app (it never starts at boot by itself):

```bash
cd ~/subaru_cyberdeck/app && ~/subaru_cyberdeck/venv/bin/python cyberdeck_gui.py          # on the deck
cd ~/subaru_cyberdeck/app && ~/subaru_cyberdeck/venv/bin/python cyberdeck_gui.py --home   # models on the PC
~/subaru_cyberdeck/venv/bin/python -m diagent gui
```

### Settings for your own machines

Nothing personal is hard-coded. Set these in your shell (or in `app/config.json` for the URLs):

| Variable | Used by | Meaning |
|---|---|---|
| `CYBERDECK_HOME_URL` | app, diagent | Your PC's Ollama, e.g. `http://<pc-tailscale-ip>:11434` (default `127.0.0.1`) |
| `CYBERDECK_PIC_URL` | app | The CHARM picture server on the PC (default `http://127.0.0.1:8765`) |
| `CYBERDECK_PIC_HOST` | `tools/charm_pic_server.py` | Address the picture server listens on (default `127.0.0.1`) |
| `CYBERDECK_PC` | `tools/deck_pull.sh` | `user@host` of the PC to pull databases from |
| `CYBERDECK_DECK` | `tools/push_home_models_to_deck.sh` | The deck's address |
| `CYBERDECK_ARCHIVE` | builders, `tools/*.sh` | A big drive for raw downloads and large builds (default `~/cyberdeck_archive`) |
| `CYBERDECK_SCRATCH` | `tools/run_general_kb.sh` | Fast scratch space (default `~/cyberdeck_scratch`) |

## Building the knowledge bases

You need your own copies of the sources. Start with `kb/README.md`; the short version:

1. Manuals: `kb/pdf_to_md.py` / `kb/convert_big_manuals.py`, then `kb/build_subaru_kb.py fsm ...` and `kb/load_manuals.sh`.
2. A CHARM archive, if you have one: `tools/make_full.sh <make>` (or `--all`) builds `kb/<make>.db`.
3. Public data: `build_subaru_diag_db.py download nhtsa romraider index`, `kb/add_nhtsa.py`, `kb/add_stackexchange.py`.
4. Telemetry definitions: `kb/build_telemetry.py` (needs the RomRaider logger XML in `raw/romraider/`).
5. Embeddings: `kb/build_subaru_kb.py embed --missing` (Qwen3-Embedding-0.6B; the deck needs it at `models/qwen3emb`).
6. Copy finished DBs to the deck: `tools/deck_pull.sh` on the deck.

Respect each source's terms. Community scrapers are rate-limited; the Row52 crawl is off by default for that reason.

## Tests

```bash
cd kb && ../venv/bin/python -m pytest tests            # telemetry, OBD PID decoding, community refs
cd shopdeck && ../venv/bin/python -m pytest tests
cd parts_sourcing && ../venv/bin/python -m pytest tests
venv/bin/python -m diagent test                          # needs kb/subaru.db + generated scenarios
```

Tests that need data you haven't built are skipped.

## License and credits

GPL-3.0-or-later (see `LICENSE`). `app/ssm/freessm_ssm2_tables.json` is derived from
[FreeSSM](https://github.com/Comer352L/FreeSSM) (GPL-3), which is why the whole project is GPL.

Also used, but not bundled: RomRaider logger definitions, NHTSA vPIC / complaints / TSBs / recalls (public domain),
python-OBD, Rockchip rknn-llm, Qwen3 models (Apache-2.0), Vosk, whisper.cpp.

Not affiliated with Subaru, Rockchip, OBDLink or any parts seller. Trademarks belong to their owners.
