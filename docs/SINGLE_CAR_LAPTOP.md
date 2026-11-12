# One car, any laptop

This is the cheap way to try the project: build a knowledge database for **one car** (yours), plug a USB OBD
cable into a laptop, and run the app with whatever model you already have, whether that's a model on your own
PC, an LLM desktop app, or an API key.

No Orange Pi, no 1 TB of manuals, no GPU needed.

## Why this version exists

I started this project to push the Orange Pi 5 and see what it could really do: run local Qwen models on its
NPU, search a stack of service manuals, and walk a factory diagnostic chart offline in the field. Along the way I
started streaming a self-hosted open-weight model from my desktop to the deck to see how much better the answers
got, and then, since why not, I wired Claude, agy and Qwen together from my PC as a team.

The project was built around the Orange Pi 5, and that's a limiting factor now that they're fairly pricey. But the
streaming versions don't actually need the Pi. Anything that serves a model works: an LLM desktop app, local
models on your PC, or a cloud API. So you can run the same app on a laptop and stream the answers from whatever you
have.

And for one car, storage stops mattering. A whole make's database is gigabytes, but one car is tens of megabytes,
so the manual pages **and their pictures** go in a single file you can copy anywhere.

## Read this first: lots of cars may not work

Be realistic before you spend an afternoon on this:

- **It has only been tested on two cars:** a 2002 Subaru Forester (K-line, Subaru SSM through a KKL cable) and a
  2003 Chevy Tahoe. Everything else is unproven.
- **Newer cars are the least likely to work well.** Generic OBD-II (codes, freeze frame, standard live data)
  should work on most 1996+ US cars through an ELM327-type adapter, but manufacturer-specific data, two-way tests
  and module access on CAN-era cars vary by make and need protocols the app doesn't speak.
- **The manuals decide how good the answers are.** The PDF converter was written for Subaru factory manuals. Other
  makes' manuals may convert badly (scanned pages, odd layouts), which means worse search and fewer usable
  diagnostic charts.
- **Cables differ.** Cheap ELM327 clones drop commands or lie about protocols. The two cables that are known to
  work are a KKL (FTDI) cable for old Subarus and an OBDLink EX for generic OBD-II.
- **Windows and macOS are untested.** The app is plain Python and PyQt6 and the serial code handles COM ports and
  `/dev/cu.*`, but it was written and run on Linux.

If your car doesn't work, the manual search, specs and chat can still be useful with no cable at all. Issues and
fixes are welcome.

## What you need

| | |
|---|---|
| A laptop | Linux, Windows or macOS, 8 GB RAM or more. Python 3.11+. |
| A cable | ELM327-type USB adapter (an OBDLink EX is the tested one), or a KKL/FTDI cable for old K-line cars. |
| Your car's manual | Easiest source: **[LEMON Manuals](https://lemon-manuals.la/)** has a single-car download for every make and model it covers, so you only grab your own car. A factory service manual PDF works too. |
| A model | Any one of: Ollama on the laptop or on a PC, an LLM desktop app with an OpenAI-compatible server (LM Studio, Jan, llama.cpp server), an OpenAI-compatible API, or a Claude API key. |

Keep what you build private. Service manuals are copyrighted; this repository holds code only, and the database
you build is for your own use.

## 1. Install

```bash
git clone https://github.com/charlie-nolan/open-diag-cyberdeck.git
cd open-diag-cyberdeck
python3 -m venv venv
venv/bin/pip install -r requirements-build.txt     # app + builders (pulls in torch for the embeddings)
venv/bin/pip install anthropic                     # only if you'll use the Claude backend
venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('Qwen/Qwen3-Embedding-0.6B')"
```

The last line downloads the embedding model once (about 1.2 GB). The builder and the app never download anything
by themselves after that, so everything runs offline.

On Windows use `venv\Scripts\pip` and `venv\Scripts\python`. Voice is an optional add-on
(`pip install -r requirements-voice.txt`) and isn't needed for any of this.

If you only want to run the app with a database someone already built for you, `requirements.txt` is enough.

## 2. Get the manual and convert it

1. Download your car from LEMON Manuals (just your car, not the whole collection), or use a factory PDF you
   already have.
2. Convert the PDF to the markdown the builder reads. This keeps the diagnostic chart tables intact and saves the
   figures next to the text:

   ```bash
   venv/bin/python kb/pdf_to_md.py --pdf /path/to/your_manual.pdf
   # -> test_batch_out/<manual name>/<manual name>.fixed.md, plus its figures
   ```

   Try `--pages 0-40 --out /tmp/try` first to see whether your manual converts cleanly.

If your manual is several PDFs, convert each one and build with the biggest (usually the engine/powertrain
section) first; see `kb/load_manuals.sh` for how multi-file manuals were loaded for the original deck.

## 3. Build the car's database

```bash
tools/build_car_db.sh --make <Make> --model <Model> --year <Year> \
    --fsm "test_batch_out/<manual>/<manual>.fixed.md" \
    --chassis <chassis code> --engines <engine code[,engine code]>
```

For example, a car with chassis code `XX1` and engine `ABC20`:

```bash
tools/build_car_db.sh --make Honda --model Civic --year 2004 \
    --fsm "test_batch_out/2004 Civic/2004 Civic.fixed.md" --chassis XX1 --engines ABC20
```

(Those codes are placeholders. Your manual's introduction lists the real ones.)

What it does, in order, skipping any step whose source you don't have:

1. Loads your manual's pages and diagnostic charts.
2. Adds CHARM pages if you have CHARM mounted (optional, 1982-2013 cars).
3. Adds public data if you've downloaded it: NHTSA TSBs, complaints and recalls, and Mechanics StackExchange
   (see section 7c of the [build guide](BUILD_GUIDE.md)).
4. Removes everything that isn't your car.
5. Embeds the text for meaning-based search. **On a laptop CPU this is the slow part, roughly 1 to 3 hours.**
   Add `--no-embed` to skip it and use keyword search only; you can run the build again later to add vectors.
6. Stores the manual's figures **inside the database**, so there's no picture server and nothing else to copy.
7. Prints where the database is and the one setting the app needs.

Useful options: `--dry-run` prints every step without running it, `--device cuda` embeds on an NVIDIA GPU,
`--no-images` leaves the pictures out, `--charm-images DIR` adds CHARM pictures from a CHARM images LMDB.

The result is `kb/cars/<make>_<model>_<year>.db` plus a folder `kb/cars/<make>_<model>_<year>/` that the app
reads. For reference, the test build of the 2002 Forester came out at 52 MB of text and vectors (against
1.15 GB for the whole Subaru database), and gave the same top three search results on every test question.

Check it:

```bash
venv/bin/python kb/car_db.py check --db kb/cars/<make>_<model>_<year>.db
```

## 4. Point the app at it

```bash
cp app/config.example.json app/config.json
```

Edit `app/config.json` and set `kb_dir` to the folder the build printed:

```json
{
  "kb_dir": "/home/you/open-diag-cyberdeck/kb/cars/honda_civic_2004"
}
```

**On Windows:** `tools/build_car_db.sh` is a bash script, so build the database on Linux (or in WSL) and copy it
over. The folder the build makes holds a symlink, which doesn't survive a copy to Windows. Instead, make the folder
yourself and put the database in it under the make's name, for example `kb\cars\honda_civic_2004\honda.db`.
To try Windows without touching the laptop's own disk, Rufus can install Windows onto a USB SSD ("Windows To Go");
a plain flash stick is too slow.

Then pick a model backend (next section) and start the app:

```bash
cd app && ../venv/bin/python cyberdeck_gui.py
```

## 5. Pick where the answers come from

Set `"backend"` in `app/config.json`. Keys are only ever read from environment variables, never from the config.

**Ollama on the laptop** (the default on a laptop, `"backend": "auto"` picks it):

```bash
ollama pull qwen3:4b-instruct-2507-q4_K_M
```
```json
{ "ollama_fast": "qwen3:4b-instruct-2507-q4_K_M", "ollama_deep": "qwen3:4b-instruct-2507-q4_K_M",
  "ollama_think": "qwen3:4b-thinking-2507-q4_K_M" }
```

**A bigger model on your PC** (stream from your desktop over your LAN or Tailscale): run Ollama on the PC, set
`"home_url": "http://<pc address>:11434"` and the `home_fast/home_deep/home_think` model names, and start the app
with `--home`.

**Any LLM desktop app or OpenAI-compatible server** (LM Studio, Jan, llama.cpp `llama-server`, vLLM, or a hosted
API that speaks the OpenAI chat format):

```json
{ "backend": "openai", "openai_url": "http://127.0.0.1:1234/v1",
  "openai_fast": "<model name as the server lists it>", "openai_key_env": "OPENAI_API_KEY" }
```

A local server usually needs no key. For a hosted one, put the key in the environment variable you named.

**Claude** (needs an API key from the Anthropic Console, which is billed separately from a Claude.ai
subscription):

```bash
export ANTHROPIC_API_KEY=...        # Windows: setx ANTHROPIC_API_KEY ...
```
```json
{ "backend": "claude" }
```

Chat uses low effort for speed; Final Diagnosis and Think Harder use high effort. Change `claude_effort` or the
`claude_fast/deep/think` model names if you want.

Team mode (`python -m teamdeck`, several models cross-checking each other) runs from a PC service and isn't set up
for laptops yet; see [TEAM_MODE.md](TEAM_MODE.md).

## 6. Plug in the cable

- **Linux:** add yourself to the serial group, then log out and back in. That's `dialout` on Debian, Ubuntu and
  Mint, and `uucp` on Arch and Manjaro:
  ```bash
  sudo usermod -aG dialout $USER      # Arch/Manjaro: sudo usermod -aG uucp $USER
  ```
- **Windows:** FTDI-based cables may need the FTDI VCP driver. The app finds COM ports by itself.
- **macOS:** the cable shows up as `/dev/cu.usbserial-*`; FTDI cables work with the built-in driver on recent
  macOS.

Key on, engine off for the first scan. In the app, fill in the car on **Intake & Scan** and press **READ CAR**.
If it says no adapter was found, the cable isn't visible to the OS yet (driver or permissions); see the
troubleshooting table in [section 10 of the build guide](BUILD_GUIDE.md#10-cables-and-your-first-scan).

Two-way tests (actuator tests) only exist for old Subarus over SSM and are best left alone on any other car.

## What's in the single-car database

Same tables as a full make database (`kb/<make>.db`), so nothing in the app changes. In addition:

| Table | What it holds |
|---|---|
| `picture` | Manual figures (grayscale WebP, 1000 px) keyed `fsm/<doc id>/<figure>`, and CHARM pictures keyed by their CHARM path, when you had them |
| `picture_read` | Chart readings a vision model already made, when you had them |
| `meta` row `pictures` | How many pictures went in, and how many were missing |

The app looks a picture up in the database first and only falls back to a picture server if one is set.
