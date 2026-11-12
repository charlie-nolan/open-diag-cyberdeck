# Build guide

How to build an Open Diagnostic Cyberdeck from scratch: the hardware, the Pi's OS, the models, the knowledge
databases, and your first scan. It follows the order the original deck was built in. Expect a weekend for the
hardware and software, and days of unattended PC time for the full databases. Each step says which machine it runs on.

> **Just want to try it on one car?** You don't need any of the hardware below. [SINGLE_CAR_LAPTOP.md](SINGLE_CAR_LAPTOP.md)
> builds a database for one car and runs the app on any laptop with a model you already have.

> **About the times and speeds in this guide:** they were all measured on the original hardware: a Ryzen 9 5900X,
> 62 GB RAM and an RTX 3070 (8 GB) on the PC, and a 16 GB Orange Pi 5 on the deck. Yours will vary with your CPU,
> GPU, VRAM, RAM and disks. A bigger GPU can be several times faster, and CPU-only builds can take many times longer.
> Use the numbers to judge relative cost, not as promises.

Contents
1. [What you need](#1-what-you-need)
2. [Power and enclosure](#2-power-and-enclosure)
3. [Pi OS and drivers](#3-pi-os-and-drivers)
4. [PC setup](#4-pc-setup)
5. [Get the code](#5-get-the-code)
6. [Models: export Qwen3 for the NPU](#6-models-export-qwen3-for-the-npu)
7. [Knowledge databases](#7-knowledge-databases)
8. [Copy everything to the deck](#8-copy-everything-to-the-deck)
9. [Voice (optional)](#9-voice-optional)
10. [Cables and your first scan](#10-cables-and-your-first-scan)
11. [Home mode: big models from your PC](#11-home-mode-big-models-from-your-pc)
12. [Optional extras](#12-optional-extras)
13. [Settings reference](#13-settings-reference)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. What you need

Every part below has a minimum that should work, the part the original deck uses (known to work), and notes on
going up or down. Swap freely; the software doesn't care about brands except where noted.

**Deck**
| Part | Minimum | Original (known to work) | Going up or down |
|---|---|---|---|
| Board | Any RK3588/RK3588S board with RKLLM support, 8 GB RAM | Orange Pi 5 v1.2, 16 GB | 8 GB runs the fast model and swaps the 4B in when needed (slower tab switches). 16 GB keeps both loaded. A 32 GB board (e.g. Orange Pi 5 Plus) gives more headroom, but other boards need their own OS image and NPU driver. Without an RK3588 there's no NPU: the app can still run against Ollama on a PC. |
| Storage | 32-64 GB microSD | 256 GB M.2 NVMe | microSD is fine for one or two makes, but slower to load. NVMe for many makes; see the sizing table below. |
| Display | Any HDMI screen, 1280x720 or more | 1920x1280 panel at 150% scaling | The UI reflows for landscape or portrait and small screens. Touch is optional. |
| Input | Any USB keyboard + mouse | Keyboard + trackball | Everything is keyboard-driven, so a small keyboard alone works. |
| Network | Ethernet or the board's Wi-Fi | USB RTL8821CU Wi-Fi adapter (morrownr driver) | Only needed for Home mode and pulling DBs from the PC. Tailscale over a phone hotspot works in the field. |
| USB hub | None | None yet (a powered hub is planned) | A powered hub helps once cables, mic, keyboard and Wi-Fi share the ports. |
| Microphone | None (voice is optional) | Board's onboard mic; Bluetooth earbuds | Any USB/3.5 mm headset. Bluetooth needs the headset (HFP) profile. |
| Power | 5 V / 4 A USB-C supply (bench use) | Milwaukee M12 battery -> low-voltage disconnect -> two buck converters | Any tool-battery or 12 V pack with an LVD and a 5 V buck works. The car's 12 V socket works too, but it's noisy at cranking, so add a good buck converter. See section 2. |

**PC** (optional: only for building databases and Home mode)
| Part | Minimum | Original (known to work) | Going up or down |
|---|---|---|---|
| OS | Any 64-bit Linux | Manjaro KDE | Windows isn't supported by the scripts; WSL2 may work but is untested. |
| CPU | 4 cores | Ryzen 9 5900X (12 cores) | More cores speed up PDF conversion and CPU embedding. |
| RAM | 16 GB (build from PDFs) | 62 GB | 32 GB+ to export the 4B models in float32 (or use `--dtype bfloat16` on less). About 64 GB for Home mode's 80B models. |
| GPU | None (CPU-only works, slowly) | RTX 3070, 8 GB | An NVIDIA card with 8 GB+ makes embedding, card baking and community extraction practical. More VRAM = faster Home mode. |
| Disk | ~50 GB for a make or two | Main SSD + 458 GB SSD + 5 TB USB HDD | See the PC sizing table below: about 1 TB for all of CHARM with pictures, 2 TB+ with LEMON. |

**How big does it need to be?** Size the build to how much data you want. The original is being built out for
*any* car, so it can diagnose whatever pulls into the shop: every make's database, all the models, and a big PC
for the builds. If you only want it for **your own car**, you need one make's database (or even just the factory
manual for your model and year) and the fast model. That build is small and light: it fits on a microSD card and runs
on the minimum hardware, and the database build is a few commands instead of days of PC time. Sizes are approximate: they come from the original build, rounded.

*On the deck*
| Piece | Size |
|---|---|
| OS + Python environment | ~10 GB |
| Fast model (Qwen3-1.7B) | ~2 GB on disk, ~4.5 GB RAM loaded |
| Deep model (Qwen3-4B), optional think model | ~5 GB each on disk, ~8 GB RAM each loaded |
| Embedding model (optional, for vector search) | ~1.2 GB |
| Voice models (optional) | ~0.3 GB |
| One make's knowledge DB | 10 MB to 6 GB (Subaru ~0.8 GB, plus ~0.5 GB of forum threads) |
| Every make's DB | ~70 GB |
| Shared tables (VIN, ECU ID, general) | under 0.1 GB |

| Deck setup | RAM | Storage |
|---|---|---|
| **Minimum**: fast model only, one or two makes, keyword search | 8 GB | 32-64 GB microSD |
| **Recommended**: fast + deep models, embeddings, a handful of makes | 16 GB | 128 GB+ (NVMe preferred) |
| **Everything**: all makes, all models, voice | 16 GB | 256 GB NVMe (what the original uses) |

Using Home mode, where the big models run on your PC, adds nothing on the deck.

*On the PC* (only if you build databases yourself)
| What you build | Disk needed |
|---|---|
| One or two makes from your own factory PDFs | ~10-50 GB (PDFs, converted markdown with figures, the DB) |
| NHTSA data for every make | ~5 GB |
| Reddit/forum archives for every make | ~15-30 GB, plus the per-make thread DBs |
| **All of CHARM, text only** | ~60 GB archive, plus ~70-100 GB of built DBs and work files |
| **All of CHARM with pictures** | add ~700 GB for the picture archive, so about **1 TB** in total |
| LEMON Manuals, the whole torrent | ~1.1 TB more, so **2 TB+** with everything (usually you only take the cars you need) |

The raw archives can sit on a cheap external USB hard drive (`CYBERDECK_ARCHIVE`). Put the DBs you're actively building
on an SSD. For RAM, 16 GB is enough to build a make from PDFs; 32 GB+ is needed to export the 4B models and to run the
community extraction. Card baking and embedding need an NVIDIA GPU (8 GB VRAM works); everything else runs on the CPU, just slower.

**Car cables** (at least one; the original has both)
| Cable | Use |
|---|---|
| OBDLink EX (USB) | Any OBD-II car (1996+): codes, all live PIDs, VIN, mode 06, plus Subaru SSM2 and GM mode 22 through it. Start with this one. |
| KKL USB cable (FTDI-based "VAG-COM" style) | Subaru SSM2 on the K-line, which is the most reliable way to read 1999-2005-ish Subarus. Cheap. |

Home mode needs [Ollama](https://ollama.com) on the PC; the 80B MoE models run at about 30 tok/s on the original's 62 GB RAM + 8 GB VRAM.

## 2. Power and enclosure

The original runs from a **Milwaukee M12 tool battery**: battery -> low-voltage disconnect (protects the pack) ->
two buck converters, one 5 V supply for the Pi and one for the screen and USB hub.

- Give the Pi its own clean 5 V supply that can deliver at least 4 A. Brownouts during NPU loads show up as random
  crashes.
- Set the LVD cutoff for your battery chemistry so the pack is never run flat.
- Fuse the battery lead.

> **Builder's notes (to fill in):** exact buck converter and LVD models, screen model, case/enclosure files.

## 3. Pi OS and drivers

**On the Pi.**

1. Flash **Ubuntu 24.04 for Rockchip** ([Joshua Riek's ubuntu-rockchip](https://github.com/Joshua-Riek/ubuntu-rockchip),
   Orange Pi 5 image) to the NVMe or an SD card. The original runs kernel `6.1.0-1025-rockchip`, which ships
   **RKNPU driver 0.9.7**. That version works with RKLLM runtime 1.3.1. Check it with:
   ```bash
   sudo cat /sys/kernel/debug/rknpu/version
   ```
2. Updates and basics:
   ```bash
   sudo apt update && sudo apt upgrade
   sudo apt install git python3-venv python3-pip build-essential cmake rsync \
        qt6-image-formats-plugins espeak-ng libportaudio2
   sudo usermod -aG dialout $USER        # serial access for the OBD cables; log out and back in
   ```
3. USB Wi-Fi (RTL8821CU only): install the [morrownr 8821cu](https://github.com/morrownr/8821cu-20210916) DKMS
   driver and blacklist the in-kernel `rtw88_8821cu` so they don't fight.
4. Install [Tailscale](https://tailscale.com/download/linux) if you want Home mode or to pull databases from your PC
   anywhere (a phone hotspot works).
5. **Don't autostart the app.** Start it by hand or from the menu icon, so a bad update never locks you out of the desktop.

## 4. PC setup

**On the PC.**
```bash
sudo apt install git python3-venv build-essential rsync     # or your distro's equivalents
# NVIDIA driver + CUDA as your distro recommends, then check: nvidia-smi
curl -fsSL https://ollama.com/install.sh | sh                # Ollama, for builders and Home mode
```
Optional: Tailscale (same tailnet as the deck), and PostgreSQL if you want the offline VIN decoder on the PC.

## 5. Get the code

**On both machines**, in the same folder under your home directory (`tools/deck_pull.sh` assumes the deck's copy has the same folder name as the PC's):
```bash
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git ~/open-diag-cyberdeck
cd ~/open-diag-cyberdeck
python3 -m venv venv
venv/bin/pip install -r requirements.txt            # deck
venv/bin/pip install -r requirements-voice.txt      # optional: voice (section 9)
venv/bin/pip install -r requirements-build.txt      # PC only: builders (torch, sentence-transformers, ...)
cp app/config.example.json app/config.json          # edit later (section 13)
```

## 6. Models: export Qwen3 for the NPU

**RKLLM runtime on the Pi.** Get Rockchip's [rknn-llm](https://github.com/airockchip/rknn-llm) **1.3.1** and put it at
`~/open-diag-cyberdeck/rknn-llm`, so that the runtime library is at
`rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so` (or copy that file to `/usr/lib/`).
Toolkit and runtime versions must match.

**Export on the PC** (x86 Linux), in its own venv with Rockchip's `rkllm-toolkit` 1.3.1 wheel from the same repo:
```bash
python3 -m venv ~/rkllm-env
~/rkllm-env/bin/pip install <rknn-llm>/rkllm-toolkit/packages/rkllm_toolkit-1.3.1-*.whl huggingface_hub
~/rkllm-env/bin/python ~/open-diag-cyberdeck/tools/export_qwen3_rkllm.py               # fast + deep + think
~/rkllm-env/bin/python ~/open-diag-cyberdeck/tools/export_qwen3_rkllm.py --only fast   # just one
```
Files land in `models/rkllm/` (`Qwen3-1.7B_W8A8_RK3588.rkllm` and so on). Add `--dtype bfloat16` if the 4B export
runs out of RAM.

**Embedding model.** Vector search uses Qwen3-Embedding-0.6B. On the PC it downloads by itself. On the deck, copy the
Hugging Face snapshot folder to `models/qwen3emb/`. Without it, search falls back to keywords (FTS5).

## 7. Knowledge databases

This is the big one, and it all runs on the PC. Every builder is resumable and logs to `logs/`. **Only use sources you have the
right to use, and don't redistribute manual-derived data.**

The result is one file per make, `kb/<make>.db`, holding:
- `section`: manual pages split at real procedures, with figures and cross-references
- `spec`: torque and other specs as printed, each with its page
- `dtc_procedure`: the diagnostic chart for each code
- `labor`: book hours and OEM part numbers (from CHARM)
- NHTSA complaints/TSBs/recalls, community fixes (`comm_*`), telemetry zones (`telem_*`), and vectors (`section_vec`)

Plus shared tables: `kb/vin.db` (vPIC makes by WMI), `kb/romid.db` (Subaru ECU IDs), and `kb/general.db`.

### Where the manuals come from
Use whichever of these you can get. The deck cites each page's source, and when two sources cover the same
procedure, the factory PDF is preferred.

| Source | What it is | How it gets in |
|---|---|---|
| **Subaru factory service manuals (PDF)** | Subaru's own FSMs, which owners share freely online. The original deck uses ten: Forester 1999, 2000, 2001, 2002 and 2004; Impreza 2002; Legacy/Outback 2004, 2005, 2008 and 2009. Their file names are listed in `kb/load_manuals.sh`. | 7a: put the PDFs in `manual_pdfs/`, convert with `pdf_to_md.py`, load with `load_manuals.sh` |
| **Operation CHARM** | An archive of service information for most makes up to the early 2010s (text plus a separate picture archive). | 7b: `tools/build_charm_all.sh` / `tools/make_full.sh` |
| **LEMON Manuals** (optional) | Free manuals for about 10,000 US/Canada vehicles, 1960-2025, distributed as a ~1.1 TB torrent. Useful for newer cars CHARM doesn't cover. | [lemon-manuals.la](https://lemon-manuals.la/) offers a single-car download for each make and model, so take only the vehicles you need. Where they're PDFs, convert and load them like 7a (`pdf_to_md.py --pdf <file>`, then `build_subaru_kb.py --db <make>.db fsm ...`). Their pages are then cited as manual pages next to CHARM. A dedicated LEMON loader with its own citation tag isn't built yet. |

These manuals are copyrighted. Use them for your own repairs, keep the built databases to yourself, and don't
redistribute either.

### 7a. Factory service manuals (PDF)
```bash
cd ~/open-diag-cyberdeck/kb
../venv/bin/python pdf_to_md.py --list                     # what's queued / done
../venv/bin/python pdf_to_md.py                            # PDF -> markdown, keeps chart tables intact
# (put the PDFs where pdf_to_md.py's list expects them; see the top of the script)
../venv/bin/python build_subaru_kb.py --db subaru.db fsm "<manual>.md" \
    --title "<name>" --model Forester --chassis SF --years 2002 --engines EJ251   # one manual
bash load_manuals.sh                                       # or a list of them (edit the list inside)
```
`convert_big_manuals.py` handles very large PDFs, and `ocr_bench.py` compares OCR approaches for hard pages.

### 7b. CHARM (Operation CHARM archive), every make
If you have a CHARM LMDB archive, put it under `$CYBERDECK_ARCHIVE/charm/`, then:
```bash
bash tools/build_charm_all.sh          # every make in kb/charm_make_groups.txt; hours per make, days in total
bash tools/make_full.sh acura          # one make all the way: CHARM, NHTSA, Reddit fixes, vectors, finish, score
bash tools/make_full.sh --all --dry-run
```
`kb/charm_images.py link --all` links CHARM pictures to their pages, and `tools/charm_pic_server.py` serves them to
the deck (section 12).

### 7c. Public data
```bash
venv/bin/python build_subaru_diag_db.py download nhtsa index                # NHTSA flat files
venv/bin/python build_subaru_diag_db.py romraider --xml <logger_STD_EN_v370.xml> index
venv/bin/python kb/add_nhtsa.py ...                                         # NHTSA into each make DB
venv/bin/python kb/add_stackexchange.py ...                                 # Mechanics StackExchange dump
bash tools/run_general_kb.sh                                                # general automotive KB
bash tools/vpic_setup.sh                                                    # offline VIN decoder (PostgreSQL)
```
Put the RomRaider logger definition at `raw/romraider/v370/logger_STD_EN_v370.xml`. Both the SSM2 tables and the
telemetry builder use it.

### 7d. Live-data reference
```bash
venv/bin/python kb/build_telemetry.py --all params zones paths merge report
```
This builds the canonical definition of every live value per make and cable, plus the manual's specified ranges.

### 7e. Community fixes (optional, GPU)
```bash
bash tools/reddit_all_makes.sh                                   # Reddit archive per make
venv/bin/python kb/build_community.py --make subaru prefilter
venv/bin/python kb/build_community.py --make subaru extract      # LLM extraction via Ollama
venv/bin/python kb/build_community.py --make subaru tables embed merge
```
Respect each site's terms and rate limits. The Row52 crawler is off by default because its terms forbid it.

### 7f. Vectors and finish
```bash
cd kb && ../venv/bin/python build_subaru_kb.py --db subaru.db embed --missing && ../venv/bin/python build_subaru_kb.py --db subaru.db finish
```

### 7g. DTC cards (optional, GPU, about 6 h per model)
```bash
bash tools/bake_cards.sh toyota                       # list models
bash tools/bake_cards.sh toyota Camry 1997-2001       # bake one
```
See `card_baking/README.md`. The first run downloads the card model (about 22 GB).

Check progress at any time with `bash tools/status.sh` (or `status.sh charm|nhtsa|dbs|disk|gpu|...`). Run only one GPU
job at a time; `tools/gpu_guard.sh` enforces it.

## 8. Copy everything to the deck

**On the deck:**
```bash
export CYBERDECK_PC=you@your-pc            # Tailscale name or LAN address
bash ~/open-diag-cyberdeck/tools/deck_pull.sh                 # app + core DBs
bash ~/open-diag-cyberdeck/tools/deck_pull.sh toyota honda    # plus these makes
bash ~/open-diag-cyberdeck/tools/deck_pull.sh --all-makes     # everything (~70 GB, use ethernet)
```
It skips any make the PC is still building and only sends changed files. Copy `models/rkllm/` and `models/qwen3emb/`
the first time with `rsync`.

Optional offline VIN decoding on the deck: `bash tools/vpic_setup_deck.sh` (about 1.3 GB).

## 9. Voice (optional)

**On the deck:**
```bash
mkdir -p ~/open-diag-cyberdeck/models/voice && cd ~/open-diag-cyberdeck/models/voice
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && unzip vosk-model-small-en-us-0.15.zip
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build && cmake --build build -j4 --config Release
sh ./models/download-ggml-model.sh base.en && sh ./models/download-ggml-model.sh tiny.en
cp models/ggml-base.en.bin models/ggml-tiny.en.bin ../
```
Pick the mic in the app's mic dropdown. If any piece is missing, the app runs without it and says which part is off.

## 10. Cables and your first scan

1. Plug the cable into the car and the deck. Key ON (engine off is fine for a first read).
2. Start the app:
   ```bash
   cd ~/open-diag-cyberdeck/app && ~/open-diag-cyberdeck/venv/bin/python cyberdeck_gui.py
   ```
   Over ssh, prefix it with `DISPLAY=:0 QT_QPA_PLATFORM=xcb`.
3. In **Intake & Scan**, pick the cable (OBDLink EX or KKL) and press **READ CAR**. It finds the port by itself
   (`/dev/serial/by-id`). The log at the bottom of Intake says which protocol answered.
4. If no VIN comes back, type it in. On a Subaru, the deck binds it to the ECU ID for next time.
5. Pick the make DB if it didn't load by itself, then go to **Chat** or **Final Diagnosis**.

Cable tips:
- Older GM trucks use J1850 VPW. If the EX's automatic search fails, the app tries each protocol directly and
  remembers the one that worked.
- Pre-2005 Subarus often won't give a VIN or full data over generic OBD-II. Use SSM2 (either cable).
- Only one program can use a cable at a time. Close FORScan and similar tools before READ CAR.

The other two apps:
```bash
~/open-diag-cyberdeck/venv/bin/python -m diagent gui
~/open-diag-cyberdeck/venv/bin/python -m diagent gui --install-icon      # add a menu icon (never autostarts)
cd ~/open-diag-cyberdeck/shopdeck && ../venv/bin/python -m shopdeck [--tui]
```

## 11. Home mode: big models from your PC

**On the PC:**
```bash
ollama pull qwen3.6:35b-a3b-q4_K_M
ollama pull qwen3-next:80b-a3b-instruct-q4_K_M
ollama pull qwen3-next:80b-a3b-thinking-q4_K_M
```
Make Ollama listen on your tailnet address (set `OLLAMA_HOST` in its service), then **on the deck** set
`"home_url": "http://<pc-tailscale-ip>:11434"` in `app/config.json` and start the app with `--home`. It falls back to the
deck's models whenever the PC is unreachable. Avoid Home mode while a GPU build is running on the PC.

Testing on a desktop without an NPU: `ollama pull qwen3:1.7b qwen3:4b-instruct-2507-q4_K_M`. The app uses Ollama
automatically when there's no RKLLM. To use an LLM desktop app or API instead, set `"backend"` to `"openai"` or `"claude"`
(see [SINGLE_CAR_LAPTOP.md, section 5](SINGLE_CAR_LAPTOP.md#5-pick-where-the-answers-come-from)).

## 12. Optional extras

- **CHARM picture server** (PC -> deck): edit and install `tools/charm-pics.service` as a systemd user unit, and set
  `pic_url` on the deck. When the GPU is free, it also reads chart pictures with the vision model.
- **Launchers for closed tools:** add `"launchers": {"Ford": {"name": "FORScan", "cmd": "..."}}` to `app/config.json`.
- **Parts sourcing:** set `home_zip` in `parts_sourcing/parts_sourcing/sourcing.json` for local store and yard links.
  Yard inventory alerts can read a mailbox (`alert_mail.py`). The password comes from an environment variable or
  a `chmod 600` file, never from the repo.
- **GM extended live data:** `chevrolet/make_pipeline.py <profile> gm-pids` builds the mode 22 list from PCMHammer.

## 13. Settings reference

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
| `CYBERDECK_OPENAI_URL` | app | An OpenAI-compatible server for `"backend": "openai"` (default `http://127.0.0.1:1234/v1`) |
| `OPENAI_API_KEY` | app | Key for that server, if it needs one (the variable's name is the `openai_key_env` setting) |
| `ANTHROPIC_API_KEY` | app | Key for `"backend": "claude"` |
| `CHARM_IMAGES` | `tools/build_car_db.sh`, `kb/car_db.py images` | Folder of the CHARM images LMDB, to store CHARM pictures in a one-car DB |

Every other app setting (model paths, context length, voice, cable, DB paths) is in `DEFAULT_CONFIG` at the top of
`app/cyberdeck_gui.py`. Override any of them in `app/config.json`.

## 14. Troubleshooting

| Symptom | Check |
|---|---|
| `rkllm_init` fails / the 4B won't load | Runtime and toolkit versions must match (1.3.1), and the NPU driver should be 0.9.7+. Check free RAM: the 4B needs about 8 GB. The app falls back to the 1.7B and says why in the top bar; crash details go to `logs/app_crash.log`. |
| READ CAR says no adapter | Are you in the `dialout` group? Is the cable listed in `/dev/serial/by-id`? Is another program holding it? |
| EX connects but "UNABLE TO CONNECT" | Engine running / key ON? The app retries each protocol; see the Intake log. |
| First chat answer is slow | The first lookup loads the embedder (about 10 s); later ones take 1-5 s. With 16 GB, the fast model and one 4B stay loaded. |
| Pictures don't show | Is `pic_url` set, is the PC's picture server reachable (`curl <pic_url>/ping`), and is Tailscale up? |
| Wi-Fi drops (RTL8821CU) | Use the morrownr driver and blacklist `rtw88_8821cu`. |
