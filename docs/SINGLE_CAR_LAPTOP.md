# One car, any laptop: the complete beginner's guide

This guide gets the Open Diagnostic Cyberdeck running on an ordinary laptop for **one car, your car**. It assumes
you have never used a terminal, Python or Git before. Every command is written out in full: copy it, paste it,
press Enter.

When you're done you will have:

- a small database (about 100-150 MB) built from your own car's service manual, with its pictures inside it;
- the cyberdeck app running on your laptop;
- a USB cable that reads your car's trouble codes and live data into the app;
- an AI model of your choice answering questions, grounded in your manual, with every step cited.

No Orange Pi, no special hardware, no 1 TB of manuals. A GPU is nice but not required.

**Time:** about 2-4 hours the first time, most of it waiting for downloads and for the database build.

---

## Contents

0. [Why this version exists](#0-why-this-version-exists)
1. [Read this first: lots of cars may not work](#1-read-this-first-lots-of-cars-may-not-work)
2. [What you need](#2-what-you-need)
3. [Open a terminal](#3-open-a-terminal)
4. [Install the basic tools](#4-install-the-basic-tools)
5. [Download this project](#5-download-this-project)
6. [Install the Python packages](#6-install-the-python-packages)
7. [Download your car's manual](#7-download-your-cars-manual)
8. [Build your car's database](#8-build-your-cars-database)
9. [Choose where the AI answers come from](#9-choose-where-the-ai-answers-come-from)
10. [Write the settings file](#10-write-the-settings-file)
11. [Start the app](#11-start-the-app)
12. [Plug in the cable and do your first scan](#12-plug-in-the-cable-and-do-your-first-scan)
13. [Using the app](#13-using-the-app)
14. [Troubleshooting](#14-troubleshooting)
15. [Optional extras](#15-optional-extras)
16. [What's inside the database](#16-whats-inside-the-database)

---

## 0. Why this version exists

I started this project to push the Orange Pi 5 and see what it could really do: run local Qwen models on its
NPU, search a stack of service manuals, and walk a factory diagnostic chart offline in the field. Along the way I
started streaming a self-hosted open-weight model from my desktop to the deck to see how much better the answers
got, and then, since why not, I wired Claude, agy and Qwen together from my PC as a team.

The project was built around the Orange Pi 5, and that's a limiting factor now that they're fairly pricey. But the
streaming versions don't actually need the Pi. Anything that serves a model works: an LLM desktop app, local
models on your PC, or a cloud API. So you can run the same app on a laptop and stream the answers from whatever you
have.

And for one car, storage stops mattering. A whole make's database is gigabytes, but one car is about 100 MB, so
the manual pages **and their pictures** go in a single file you can copy anywhere.

---

## 1. Read this first: lots of cars may not work

Please be realistic before you spend an afternoon on this:

- **Only two cars have been tested:** a 2002 Subaru Forester (old K-line, Subaru SSM through a KKL cable) and a
  2003 Chevy Tahoe. Everything else is unproven.
- **Newer cars are the least likely to work well.** Basic OBD-II (trouble codes, freeze frame, standard live data)
  should work on most 1996-and-newer US cars through an ELM327-type adapter. Manufacturer-specific data, two-way
  tests and module access on newer CAN cars vary by make and need protocols this app doesn't speak.
- **The manual decides how good the answers are.** A LEMON download gives you the manual's pages, labor times and
  pictures. Its diagnostic flowcharts are pictures, though, so out of the box the app can show them but not walk
  you through them one step at a time. A factory service manual PDF with real text fixes that (see
  [section 15](#15-optional-extras)), and so does the optional vision step.
- **Cheap cables cause trouble.** Many cheap "ELM327" clones drop commands or lie about what they support. The two
  cables known to work are an **OBDLink EX** (any 1996+ car, generic OBD-II) and a **KKL / FTDI cable** (old
  Subarus and other K-line cars).
- **Windows and macOS are untested.** The app is plain Python and should run there, but it was written and tested
  on Linux. Expect rough edges and please report them.

If the cable part doesn't work on your car, the manual search, specs and chat are still useful without it.

**Copyright:** service manuals are copyrighted. This repository contains code only. The database you build is
for your own personal use: don't share or upload it.

---

## 2. What you need

| Item | Details |
|---|---|
| A laptop | Linux (easiest), Windows or macOS. **8 GB of RAM minimum, 16 GB better.** About **15 GB of free disk space** (Python packages and models are big). |
| An internet connection | For the downloads. After setup the app works offline if you use a local model. |
| A USB OBD cable | **OBDLink EX** (recommended, about $60-70) for any 1996+ car. Or a **KKL / FTDI** "VAG-COM style" cable for old K-line cars. Bluetooth and Wi-Fi adapters are not supported. |
| Your car's manual | A free **LEMON Manuals** single-car download (section 7). A factory service manual PDF is a great extra. |
| An AI model | One of: a free local model through Ollama (section 9, option A), a bigger model on another computer, any LLM desktop app such as LM Studio or Jan, or a paid API key (OpenAI-compatible or Claude). |

You don't need any programming knowledge.

---

## 3. Open a terminal

A terminal is a window where you type commands. Every grey box in this guide is something you paste into it.

- **Linux:** press `Ctrl` + `Alt` + `T`, or search your apps for "Terminal" (on Manjaro it may be called
  "Konsole").
- **Windows:** you will use two things:
  - **PowerShell** to run the app: press the Windows key, type `PowerShell`, press Enter.
  - **WSL (Linux inside Windows)** to build the database, because the build script is a Linux script. You'll
    install it in section 4.
- **macOS:** press `Cmd` + `Space`, type `Terminal`, press Enter.

Tips:
- To paste into a Linux terminal use `Ctrl` + `Shift` + `V` (plain `Ctrl` + `V` doesn't work there).
- When a command asks for your password, type it and press Enter. **Nothing appears while you type.** That's
  normal.
- Lines starting with `#` inside the grey boxes are comments for you to read. Pasting them does nothing.
- Wait for each command to finish (the prompt comes back) before pasting the next one.

---

## 4. Install the basic tools

You need **Git** (downloads the project) and **Python 3.11 or newer** (runs it). Follow the part for your system.

### Ubuntu, Linux Mint, Pop!_OS, Debian

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip libxcb-cursor0
python3 --version
```

The last line must print `Python 3.11` or higher. If it prints 3.10 or lower, your system is too old: upgrade
to Ubuntu 24.04 or newer (or Mint 22 or newer).

### Arch, Manjaro, EndeavourOS

```bash
sudo pacman -Syu --needed git python python-pip
python --version
```

On these systems the command is `python`, not `python3`. Wherever this guide says `python3`, type `python`.

### Fedora

```bash
sudo dnf install -y git python3 python3-pip
python3 --version
```

### Windows 10 or 11

1. **Install Python.** Go to <https://www.python.org/downloads/>, download the latest Python 3 installer and run
   it. **On the first screen, tick "Add python.exe to PATH"** before clicking Install. This matters.
2. **Install Git.** Go to <https://git-scm.com/download/win>, download, run the installer and accept every default.
3. **Install WSL** (for the database build). Open PowerShell **as administrator** (right-click PowerShell, "Run as
   administrator") and paste:
   ```powershell
   wsl --install -d Ubuntu
   ```
   Restart the computer when it asks. After the restart an Ubuntu window opens and asks you to pick a user name
   and password (anything you like; remember the password). Then, **in that Ubuntu window**, follow the Ubuntu
   instructions above. Use the Ubuntu window for sections 5-8, and PowerShell for sections 9-12.
4. Close and reopen PowerShell, then check:
   ```powershell
   py --version
   git --version
   ```

### macOS

1. In Terminal, paste `xcode-select --install` and click Install in the window that appears (this gives you Git).
2. Download Python 3 from <https://www.python.org/downloads/macos/> and run the installer.
3. Check: `python3 --version` and `git --version`.

---

## 5. Download this project

Paste this in your terminal (on Windows: in the **Ubuntu (WSL)** window):

```bash
cd ~
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git
cd open-diag-cyberdeck
```

You now have a folder called `open-diag-cyberdeck` in your home folder. **Every later command assumes you are
inside it.** If you close the terminal, get back in with:

```bash
cd ~/open-diag-cyberdeck
```

---

## 6. Install the Python packages

Python packages go into a private folder called a "virtual environment" (`venv`), so nothing on your system
changes.

### Linux and macOS (and the Ubuntu window on Windows)

```bash
cd ~/open-diag-cyberdeck
python3 -m venv venv
venv/bin/pip install --upgrade pip
```

**If your laptop has no NVIDIA graphics card** (most laptops), install the small CPU-only version of PyTorch first.
It saves about 4 GB of downloads:

```bash
venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
```

Then install everything else (this takes 5-15 minutes):

```bash
venv/bin/pip install -r requirements-build.txt
```

Finally, download the search model once (about 1.2 GB). After this, nothing downloads by itself and search works
offline:

```bash
venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('Qwen/Qwen3-Embedding-0.6B')"
```

### Windows (PowerShell, for running the app)

The database is built in WSL, but the app runs in Windows itself, so it needs its own install. In PowerShell:

```powershell
cd ~
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git
cd open-diag-cyberdeck
py -m venv venv
venv\Scripts\pip install --upgrade pip
venv\Scripts\pip install torch --index-url https://download.pytorch.org/whl/cpu
venv\Scripts\pip install -r requirements.txt sentence-transformers huggingface_hub
venv\Scripts\python -c "from huggingface_hub import snapshot_download as d; d('Qwen/Qwen3-Embedding-0.6B')"
```

(`torch` and `sentence-transformers` are what the app uses for meaning-based search. Without them it still works,
with keyword search only.)

### Optional packages

```bash
venv/bin/pip install anthropic                    # only if you'll use Claude (section 9, option D)
venv/bin/pip install -r requirements-voice.txt    # only if you want voice; not needed
```

---

## 7. Download your car's manual

**LEMON Manuals** offers free service manuals for about 10,000 US and Canadian vehicles, 1960-2025, and lets you
download **just your car**.

1. Go to <https://lemon-manuals.la/>.
2. Find your car's make, model and year.
3. Download the single-car package. You get a `.zip` file.
4. **Don't unzip it.** The build reads the zip directly.
5. Note where it saved. Usually that's your Downloads folder, for example
   `~/Downloads/2004 Honda Civic.zip`.

**On Windows:** your Windows Downloads folder is visible from the Ubuntu (WSL) window as
`/mnt/c/Users/<your Windows user name>/Downloads/`. For example:
`/mnt/c/Users/alex/Downloads/2004 Honda Civic.zip`.

**Tip for file names with spaces:** always wrap the whole path in quotes, like `"~/Downloads/2004 Honda Civic.zip"`.
Or rename the file to something without spaces, like `civic.zip`.

Have a factory service manual PDF too? Keep it. You'll add it in the next step, and it gives the app step-by-step
diagnostic charts.

---

## 8. Build your car's database

This turns the manual into a searchable database for your one car. Run it in Linux, macOS, or the **Ubuntu (WSL)**
window on Windows.

### 8.1 Run the build

Replace the make, model, year and zip path with yours. Keep the quotes.

```bash
cd ~/open-diag-cyberdeck
tools/build_car_db.sh --make "Honda" --model "Civic" --year 2004 \
    --manuals "$HOME/Downloads/2004 Honda Civic.zip" --no-charm
```

The `\` at the end of the first line just continues the command onto the next line. Paste both lines together.

**With a factory PDF as well** (recommended if you have one), add a second `--manuals`:

```bash
tools/build_car_db.sh --make "Honda" --model "Civic" --year 2004 \
    --manuals "$HOME/Downloads/2004 Honda Civic.zip" \
    --manuals "$HOME/Downloads/civic_service_manual.pdf" --no-charm
```

What the options mean:

| Option | Meaning |
|---|---|
| `--make`, `--model`, `--year` | Your car. Use plain names, the way the manual writes them. |
| `--manuals <file or folder>` | A LEMON zip, a PDF, a folder of PDFs, or an already converted `.md`. Use it once per file. |
| `--no-charm` | Don't look for the old CHARM archive. Always use this unless you have CHARM set up. |
| `--chassis <code>` `--engines <code>` | Optional. The chassis and engine codes from the manual's introduction (e.g. `--engines K20A3`). They help when a manual covers several engines. |
| `--no-embed` | Skip the slow "meaning search" step (keyword search only). Good for a quick first try. |
| `--device cuda` | Use an NVIDIA graphics card for the slow step. Much faster. |
| `--dry-run` | Only print what it would do. |

### 8.2 What you'll see and how long it takes

The build prints one line per step with the time it took. Steps whose data you don't have print `skip ...`. That
is normal: a first-time builder won't have NHTSA, forum or diagnostic-card data, and the build works without them.

Rough times for one car (details in [BUILD_ESTIMATES.md](BUILD_ESTIMATES.md)):

| Step | Typical laptop | With an NVIDIA GPU |
|---|---|---|
| Reading a LEMON zip | under 1 minute | under 1 minute |
| Converting a factory PDF (if added) | 5-10 minutes | same |
| "Embedding" (meaning search) | **20 minutes to 1.5 hours** | 1-2 minutes |

You can keep using the laptop while it runs. If you close the terminal, the build stops. Just run the same command
again.

### 8.3 When it finishes

The last two lines tell you where the database is and print the exact `kb_dir` line for the settings file. They
look like this:

```
... done: /home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004.db (112M), total 1834 s
... app: set "kb_dir": "/home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004" in app/config.json (the folder holds honda.db -> ../honda_civic_2004.db)
```

**Copy that path somewhere.** You need it in section 10.

Check the database (it prints counts and runs a few test searches):

```bash
venv/bin/python kb/car_db.py check --db kb/cars/honda_civic_2004.db
```

(Use your own file name. It's `<make>_<model>_<year>.db` in lowercase, with spaces turned into `_`.)

### 8.4 Windows only: copy the database over to Windows

The build made the database inside WSL. Copy it into your Windows copy of the project, under the make's name. In
the **Ubuntu (WSL)** window (replace `alex` with your Windows user name and the car names with yours):

```bash
mkdir -p /mnt/c/Users/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004
cp ~/open-diag-cyberdeck/kb/cars/honda_civic_2004.db \
   /mnt/c/Users/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004/honda.db
```

The file inside the folder must be named after the **make** in lowercase (`honda.db`, `toyota.db`,
`chevrolet.db`...). Your `kb_dir` on Windows is then `C:/Users/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004`.
Use forward slashes in the settings file.

---

## 9. Choose where the AI answers come from

The app sends your car's evidence, plus the right manual pages, to a language model. Pick **one** of these options.

### Option A: a free model on the laptop itself (Ollama). Simplest, fully offline.

1. Install Ollama:
   - **Linux:** `curl -fsSL https://ollama.com/install.sh | sh`
   - **Windows / macOS:** download it from <https://ollama.com/download> and run the installer.
2. Download a model (about 2.5 GB each). With 8 GB RAM use these small ones:
   ```bash
   ollama pull qwen3:4b-instruct-2507-q4_K_M
   ollama pull qwen3:4b-thinking-2507-q4_K_M
   ```
   With 16 GB or more RAM and a decent GPU you can try bigger ones, such as `qwen3:8b`, and put their names in the
   settings instead.
3. Leave Ollama running (it starts by itself after install).

Small local models are much weaker than big ones. In our tests the big cloud model got far more cases right. Use
Option A to try things out, and B, C or D for serious diagnosis.

### Option B: a bigger model on another computer (your desktop PC)

If you have a desktop with a good graphics card:

1. Install Ollama on the **desktop** and pull a bigger model (for example `ollama pull qwen3:32b`).
2. Make the desktop's Ollama reachable from your network: set the environment variable `OLLAMA_HOST=0.0.0.0` for
   the Ollama service and restart it (see Ollama's FAQ, "How do I configure Ollama server?").
3. Find the desktop's address (for example `192.168.1.50`, or its Tailscale address if you use Tailscale to reach
   it from anywhere).
4. Use the `home_*` settings in section 10 and start the app with `--home`.

### Option C: an LLM desktop app or any OpenAI-compatible service

LM Studio, Jan and llama.cpp's `llama-server` all run a local server that speaks the "OpenAI-compatible" format.
Many paid cloud services do too.

- **LM Studio:** load a model, open the Developer / Local Server tab and click Start Server. The address is usually
  `http://127.0.0.1:1234/v1`. Note the model's name as LM Studio shows it.
- **A paid cloud service:** find its "OpenAI-compatible base URL" and create an API key on its website.

### Option D: Claude (Anthropic API)

This gave the best results in testing. It needs an **API key** from <https://console.anthropic.com/>, which is
billed separately from a Claude.ai subscription (you pay per use).

1. Create a key in the Anthropic Console (Settings, API Keys).
2. Install the package if you skipped it: `venv/bin/pip install anthropic` (Windows: `venv\Scripts\pip install anthropic`).
3. Store the key in an environment variable. **Never put the key in the settings file.**
   - **Linux / macOS:**
     ```bash
     echo 'export ANTHROPIC_API_KEY="paste-your-key-here"' >> ~/.bashrc
     source ~/.bashrc
     ```
     (On macOS use `~/.zshrc` instead of `~/.bashrc`.)
   - **Windows (PowerShell):**
     ```powershell
     setx ANTHROPIC_API_KEY "paste-your-key-here"
     ```
     Then close PowerShell and open a new one.

---

## 10. Write the settings file

The app reads its settings from `app/config.json`. Create it with a text editor:

- **Linux:** `nano app/config.json` (paste the text, then `Ctrl`+`O`, Enter to save, `Ctrl`+`X` to exit).
- **macOS:** `open -e app/config.json` after creating it with `touch app/config.json`.
- **Windows:** `notepad app\config.json` (say Yes to creating it).

Paste **one** of the examples below and change the `kb_dir` line to the path from section 8.3. Keep the quotes and
commas exactly as shown.

**Option A, Ollama on the laptop:**

```json
{
  "kb_dir": "/home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004",
  "backend": "ollama",
  "ollama_fast": "qwen3:4b-instruct-2507-q4_K_M",
  "ollama_deep": "qwen3:4b-instruct-2507-q4_K_M",
  "ollama_think": "qwen3:4b-thinking-2507-q4_K_M"
}
```

**Option B, a model on your desktop PC** (start the app with `--home`):

```json
{
  "kb_dir": "/home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004",
  "backend": "ollama",
  "home_url": "http://192.168.1.50:11434",
  "home_fast": "qwen3:32b",
  "home_deep": "qwen3:32b",
  "home_think": "qwen3:32b"
}
```

**Option C, LM Studio or another OpenAI-compatible server:**

```json
{
  "kb_dir": "/home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004",
  "backend": "openai",
  "openai_url": "http://127.0.0.1:1234/v1",
  "openai_fast": "the-model-name-as-the-server-shows-it",
  "openai_key_env": "OPENAI_API_KEY"
}
```

A local server needs no key. For a paid service, set the key in the `OPENAI_API_KEY` environment variable, the
same way as in Option D.

**Option D, Claude:**

```json
{
  "kb_dir": "/home/alex/open-diag-cyberdeck/kb/cars/honda_civic_2004",
  "backend": "claude"
}
```

Claude uses `claude-opus-5-5`, with low effort for quick chat and high effort for Final Diagnosis. You can change
`claude_fast`, `claude_deep`, `claude_think` and `claude_effort` if you want.

---

## 11. Start the app

**Linux / macOS:**

```bash
cd ~/open-diag-cyberdeck/app
../venv/bin/python cyberdeck_gui.py
```

Add `--home` at the end for Option B.

**Windows (PowerShell):**

```powershell
cd ~\open-diag-cyberdeck\app
..\venv\Scripts\python cyberdeck_gui.py
```

The app window opens. The first question can take a little longer while the search model loads.

---

## 12. Plug in the cable and do your first scan

### 12.1 Let your user talk to the cable (Linux only, once)

Linux only lets members of a special group use serial cables. Add yourself, then **log out and back in** (or
restart):

```bash
# Ubuntu, Mint, Debian, Fedora:
sudo usermod -aG dialout $USER
# Arch, Manjaro, EndeavourOS:
sudo usermod -aG uucp $USER
```

**Windows:** plug the cable in and wait a minute. Windows Update installs the FTDI driver by itself. If it doesn't
show up as a "COM" port in Device Manager (under "Ports (COM & LPT)"), install the "VCP" driver from
<https://ftdichip.com/drivers/vcp-drivers/>.

**macOS:** recent macOS has the FTDI driver built in. The cable shows up as `/dev/cu.usbserial-...`.

### 12.2 Scan the car

1. Plug the cable into the laptop.
2. Plug the other end into the car's OBD-II port. It's under the dashboard on the driver's side, near the steering
   column.
3. Turn the key to **ON** (dash lights on) but **don't start the engine** for the first scan.
4. In the app, open **Intake & Scan**. Enter the year, make and model (the same as your build).
5. Press **READ CAR**. After a few seconds the codes and live data appear.
6. To see live data with the engine running, start the engine and press READ CAR again. Never work under the hood
   or near moving parts while the engine runs.

If it says no adapter was found, see [Troubleshooting](#14-troubleshooting).

---

## 13. Using the app

- **Intake & Scan:** the car, its codes and data, plus what you know: symptoms, when it happens, work already
  done. The **Suspect** box is for your own guess. On its first pass the app deliberately hides it from the model,
  so the diagnosis comes from the evidence. Your guess is then checked against that evidence.
- **Data check:** before any model sees the case, the app checks it in plain code. It flags readings that can't be
  true together (like 0 rpm at highway speed), codes that aren't valid, and parts your car's manual never mentions.
  If you see flags, rescan or fix the input first.
- **Final Diagnosis:** the ranked likely causes, each with the manual steps to confirm it and the source page cited.
- **Chat:** ask follow-up questions in plain words. `/torque <part>` and `/fsm <topic>` look things up in your
  manual directly, without the AI.
- **Repair Guide and Parts:** the procedure and part details from your manual, with labor times.

**Always confirm a diagnosis with the manual's tests before buying parts.** The app cites its sources so you can
check them.

---

## 14. Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found` | On Arch/Manjaro type `python`. On Windows use `py`. Otherwise Python isn't installed: see section 4. |
| `No module named venv` / `ensurepip is not available` | Ubuntu/Debian: `sudo apt install python3-venv`, then repeat section 6. |
| `pip install` fails building a package | Upgrade pip (`venv/bin/pip install --upgrade pip`) and try again. Make sure Python is 3.11+. |
| `Could not load the Qt platform plugin "xcb"` | Ubuntu/Debian: `sudo apt install libxcb-cursor0`. |
| `tools/build_car_db.sh: Permission denied` | Run it as `bash tools/build_car_db.sh ...` instead. |
| Build says `no such file` for your zip | The path is wrong or has spaces without quotes. Put the whole path in quotes. On Windows the Downloads folder in WSL is `/mnt/c/Users/<name>/Downloads/`. |
| Build is very slow at "embed" | Normal on a laptop CPU (up to ~1.5 h). Use `--no-embed` for a quick test, or `--device cuda` with an NVIDIA GPU. |
| App says "No database for <make>" | `kb_dir` in `app/config.json` doesn't point at the folder from section 8.3, or (on Windows) the file inside isn't named `<make>.db`. Also check the make you picked in Intake matches the build. |
| App says it can't reach the model | Option A: is Ollama running (`ollama list` should answer)? Option B: is the desktop on and reachable? Option C: is the server started? Option D: open a new terminal after setting the key, and check that `anthropic` is installed. |
| READ CAR: no adapter found | Linux: did you log out and back in after `usermod`? Is the cable listed in `ls /dev/serial/by-id`? Windows: does Device Manager show a COM port? Is another program (such as a phone app or other scanner software) using the cable? |
| READ CAR finds the adapter but no data | Key must be ON. Some cars need the engine running for live data. Older cars may need the KKL cable, not the OBDLink EX. Your car's protocol may not be supported (see section 1). |
| Anything else | Open an issue on GitHub with the exact error message (remove any VIN or personal details first). |

---

## 15. Optional extras

### Add a factory service manual PDF (step-by-step diagnostic charts)

A factory PDF with real text gives the app diagnostic charts it can walk one step at a time, plus many more torque
specs. In testing on one car: 74 walkable diagnostic trees and 454 torque specs with the factory PDF, against 0
and 14 from LEMON alone. Add it with a second `--manuals` (section 8.1). To check first whether your PDF converts
cleanly (it needs a real text layer: scanned pages aren't read):

```bash
venv/bin/python kb/pdf_to_md.py --pdf "/path/to/manual.pdf" --pages 0-40 --out /tmp/try
```

Then open the `.md` file in `/tmp/try` with a text editor and look at the text.

### Read the manual's pictures with a vision model

LEMON's diagnostic charts and many spec tables are pictures. A vision model can read them into text, so the app
can walk those charts and look up those specs. Add `--vision <model>` to the build, for example:

```bash
tools/build_car_db.sh --make "Honda" --model "Civic" --year 2004 \
    --manuals "$HOME/Downloads/2004 Honda Civic.zip" --no-charm \
    --vision qwen3.6:35b-a3b-q4_K_M
```

This needs that vision model in Ollama (`ollama pull <model>`) and, realistically, a desktop with a GPU. In testing
it took about 12 seconds per picture on an RTX 3070, about 2 hours for one car's ~550 charts and tables. Small
vision models read charts badly. Everything a vision model reads is marked **unverified** in the app, so check it
against the picture.

### Public recall and complaint data (NHTSA)

NHTSA publishes recalls, technical service bulletins and owner complaints. To include them, download and index the
NHTSA files once (several GB), then build again:

```bash
venv/bin/python build_subaru_diag_db.py download nhtsa index
venv/bin/python kb/add_nhtsa.py --help
```

See section 7c of the [build guide](BUILD_GUIDE.md) for details.

### Forum fixes for your car (advanced)

Real-world fixes extracted from forum and Reddit threads ("what actually fixed it"). This needs a forum dump and a
GPU running a local model for a few hours. See [COMMUNITY_DATA.md](COMMUNITY_DATA.md),
`kb/car_db.py community-select --help`, and the build's `--community-work` option.

### Bigger and better models

The app's answers get much better with bigger models. If you have a desktop with a strong GPU, Option B in
section 9 is the best free setup. Team mode (several models cross-checking each other) runs from a PC service; see
[TEAM_MODE.md](TEAM_MODE.md).

### Trying Windows from a USB drive

To try Windows without touching the laptop's own disk, Rufus (<https://rufus.ie>) can install Windows onto a USB
SSD ("Windows To Go" option). A plain flash drive is too slow.

---

## 16. What's inside the database

Your car's database has the same tables as the project's full per-make databases (`kb/<make>.db`), so the app
treats it the same way. In addition:

| Table | What it holds |
|---|---|
| `picture` | The manual's pictures and figures, stored inside the database (WebP), so nothing else needs copying |
| `picture_read` | Text a vision model read from chart and spec-table pictures, marked unverified |
| `meta` row `pictures` | How many pictures went in, and how many were missing |

The app looks pictures up in the database first, and only uses a separate picture server if you set one.

How long each build step takes, how it scales to other cars and other hardware, and the full factory-PDF vs LEMON
comparison: [BUILD_ESTIMATES.md](BUILD_ESTIMATES.md).
