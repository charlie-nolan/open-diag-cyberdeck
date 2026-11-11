# Home app: two-model diagnostic app

No agents. Two local Qwen3 models with fixed jobs, grounded in your FSM databases:

| Tab | Model | What it does |
|---|---|---|
| 1. Intake & Scan | none | READ CAR (codes + live data over either cable), or tap bubbles by hand: vehicle, trouble codes, symptoms, already checked. Typing boxes for anything else. Whatever is on this screen follows you to the others, no SAVE needed |
| 2. Brainstorm / Chat | DEEP: Qwen3-4B (the 1.7B if the 4B is missing or won't load; `"chat_model": "fast"` in config.json forces the 1.7B) | Back-and-forth brainstorming that already knows the case. Each turn is rebuilt from the case, the manual's steps for its codes and the last few exchanges, so it never fills up. Buttons: TORQUE and MANUAL (plain lookups of what you typed), DEBATE (Parts Cannon vs Frugal Skeptic; SAVE DEBATE adds it to the case), WILDCARD (5 unlikely-but-possible causes on the 4B at high temperature, never saved), CLEAR. The slash commands still work. Nothing writes to the case except SAVE DEBATE |
| 3. Verdict | DEEP: Qwen3-4B-Instruct-2507 | Most likely cause, what rules out the rest, next test. Tick THINK HARDER to use Qwen3-4B-Thinking-2507 instead (slower, shows its reasoning) |
| 4. Repair Guide | DEEP | Tap a repair (or it takes the one named in the verdict). Specs from the database first, then a numbered procedure built from FSM excerpts |
| 5. Parts | none | Tap a job (or it takes the repair/verdict): CHARM part numbers for this car, which other Subarus share the part, QR codes for dealer / aftermarket / junkyard searches, and the junkyard quote (Pick-n-Pull price + CHARM book hours). PRINT SHEET opens a printable page. Needs `~/open-diag-cyberdeck/parts_sourcing/` and `pip install segno`. `/part <name or number>` does the same in Chat |

Torque specs are always looked up, never generated. The app uses `kb/subaru.db` (FSM-sourced specs with page numbers) when it exists, and falls back to `subaru_diag.db` + `test_subaru_knowledge.db` otherwise (where hand-entered specs show as `UNVERIFIED`).

## Reading the car: two cables

**Other programs per make:** add them to `"launchers"` in `config.json`, e.g. `"Ford": {"name": "FORScan", "cmd": "wine '~/.wine/drive_c/Program Files (x86)/FORScan/FORScan.exe'"}`. When the car's make has one, Intake shows an OPEN button. READ CAR waits until that program is closed, because only one program can use the cable. Nothing opens by itself.

**Which car, which database:** each make has its own knowledge DB (`kb/subaru.db`, later `kb/chevrolet.db` and so on). Intake loads it from the VIN (read over OBD-II, or typed in), from the SSM reply (any reply = Subaru), or from the Make dropdown. If a make has no DB yet, the app says so, and codes, chat and verdict still work. Details: `VIN_DECODING.md`.

Pick the cable in the Intake tab (the app remembers it). READ CAR only reads; the TESTS screen is the only place that writes (see below). If the cable or driver is missing, the reason shows in the Intake log and the app keeps running.

| Cable | What it reads | Needs |
|---|---|---|
| **OBDLink EX** (`obdlink`, default) | First generic OBD-II: stored DTCs, RPM, coolant, trims, MAP, MAF, timing, load, volts, and the VIN (asked of every car). Then, unless the VIN names another make, Subaru SSM2 through the same EX: the ECU's own DTC table, engine ID, knock correction and more. The EX settings that work are remembered (`ex_ssm_variant`); every byte of the SSM attempt goes to `cache/ex_ssm_last.log` | `pip install obd pyserial`, user in the `dialout` group |
| **Bare KKL cable** (`kkl`, fallback) | Subaru SSM2 only, straight on the K-line | `pip install pyserial`, user in the `dialout` group |

The SSM code is in `app/ssm/` (see `app/ssm/README.md`). READ CAR only sends 0xBF (identify) and 0xA8 (read). Each SSM read saves the ECU's init reply to `cache/ecu_init_reply_ex.hex` (EX) or `cache/ecu_init_reply.hex` (KKL), which pins down exactly which live values your ECU supports. The cylinder roughness (misfire) counters are left out until that file confirms the 2002 ECU has them. SSM through the EX uses `app/ssm/ex_ssm2_probe.py`. Tested against a simulated ECU and adapter only, not the car yet.

## Two-way tests (6. TESTS screen)

TEST MODE is off every time the deck starts. Switching it on asks first, then the deck holds the cable (READ CAR and OPEN buttons wait). Every action asks again before anything is sent, and **STOP ALL** never asks.

| Button | What it does | Checks before sending |
|---|---|---|
| CONNECT TO SUBARU ECU | Lists the actuator tests this ECU advertises in its own init reply (fuel pump, fans, purge valve, ...) | |
| RUN SELECTED TEST | Switches that one actuator on until STOP ALL, or 30 s | Test-mode connector joined (address 0x61 bit 5), engine off (<= 100 rpm) |
| CLEAR SUBARU ECU MEMORY | Writes 0x40 to 0x60: stored codes and learned values erased | Engine off |
| CLEAR CODES, ANY CAR | OBD-II mode 04 through the OBDLink EX (also resets readiness monitors) | Engine off |
| STOP ALL | Ends the test and writes 0x00 to every actuator byte; also switches off actuators that started by themselves at key-on | |

Leaving the TESTS screen, switching TEST MODE off or closing the app stops a running test. The Subaru commands and checks are ported from FreeSSM (GPL-3) in `app/ssm/ssm_tests.py`, which refuses any write that isn't one of this ECU's advertised tests or clear memory. No flashing, adjustments or security access. Tested on simulated ECUs (KKL and EX), and on a 2002 Forester in test mode, where the fans, relays and pumps switched on and off from the deck.

## Voice (offline)

Two engines with separate jobs. Voice runs in its own background process, pinned to the A55 cores (0-3), so the NPU and A76 cores stay free for Qwen and a stuck mic can't freeze the screen.

- **Vosk** (`vosk-model-small-en-us-0.15`) is always listening, but only acts after **"hey deck"** ("hey cyberdeck" works too). It uses a closed phrase list (the commands plus the part names in the kb), so it ignores compressors and shop talk. Quick asks, answered from the kb, not a model:
  - *torque for &lt;part&gt;*: spoken, and shown in Chat with the FSM page
  - *next step after &lt;what you just did&gt;*: matched against the open repair procedure's numbered steps and the next one is read out. Stateless, so you never say "step 3 done".
  - *fluid capacity &lt;system&gt;*: there's no fluid table yet, so it puts the closest manual section on screen
  - *read the car*, *save case*, *new case*, *verdict*, *go to chat / intake / verdict / repair*, *stop*
- **🎤 MIC** (bottom bar, every screen) uses whisper.cpp: on Intake the words go to Notes, on Chat they're sent as your question, on Repair Guide they set the repair. It works even without Vosk. Pick your headset in the mic dropdown next to it (remembered), and press 🔊 TEST to hear the speaker.
- **whisper.cpp** (`base.en`, falls back to `tiny.en`) is idle until you say **"hey deck, intake note"** (or "customer complaint"), or press MIC. It stops after about 1.5 s of quiet (or 25 s), or when you press MIC/F2 again. The punctuated text lands in the Intake **Notes** box. The raw text is kept, and the fast model pulls out three lines (symptoms, parts already replaced or checked, when it happens). SAVE CASE stores both, and Verdict sees both.
- **Spoken replies** use pyttsx3, falling back to espeak-ng (Piper optional, via `piper_bin`/`piper_model` in config). Only information asks are spoken. Diagnoses, verdicts, quotes and prices stay on screen.

Setup on the Pi (models go in `~/open-diag-cyberdeck/models/voice/`, about 700 MB of RAM for both engines while running):

```
sudo apt install espeak-ng libportaudio2 cmake build-essential
pip install vosk sounddevice numpy pyttsx3
# Vosk small English model
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip && unzip vosk-model-small-en-us-0.15.zip -d ~/open-diag-cyberdeck/models/voice/
# whisper.cpp built on the Pi, plus base.en (and tiny.en as the fallback)
git clone https://github.com/ggml-org/whisper.cpp ~/open-diag-cyberdeck/models/voice/whisper.cpp
cd ~/open-diag-cyberdeck/models/voice/whisper.cpp && cmake -B build && cmake --build build -j4 --config Release
sh ./models/download-ggml-model.sh base.en && sh ./models/download-ggml-model.sh tiny.en
cp models/ggml-base.en.bin models/ggml-tiny.en.bin ../
```

Mic: use a USB or 3.5 mm headset and pick it in the mic dropdown. Speech output goes to the system's default output, so set the headset as the output in the desktop's sound settings.

If any piece is missing the app still runs, and the top bar says which part is off. Not done yet: Silero VAD (the end-of-speech check is a simple loudness gate for now), and a fluids table in the kb. Tested only with stand-in audio, not a real mic.

## Make the models (desktop, x86 Linux)

In the venv that has `rkllm-toolkit` 1.3.1 (same version as the Pi runtime):

```
pip install huggingface_hub
python ~/open-diag-cyberdeck/tools/export_qwen3_rkllm.py            # all three models (fast, deep, think)
python ~/open-diag-cyberdeck/tools/export_qwen3_rkllm.py --only fast
```

float32 export of the 4B needs about 32 GB RAM; add `--dtype bfloat16` if it runs out.
Other models work too, e.g. `--hf Qwen/Qwen3-8B --name Qwen3-8B`, then point `deep_model` in `app/config.json` at it.

## Run on the Orange Pi

Copy `~/open-diag-cyberdeck` (at least `app/`, `models/rkllm/`, `kb/`, `rknn-llm/rkllm-runtime/`, and `session_active.db`) to the Pi, then:

```
sudo apt install python3-pyqt6      # or: pip install PyQt6
pip install obd pyserial
python3 ~/open-diag-cyberdeck/app/cyberdeck_gui.py
```

The app uses RKLLM automatically when it finds `librkllmrt.so` on an aarch64 board.
With 16 GB RAM both models stay loaded; on 8 GB they swap (a few seconds when you switch tabs).
Models: fast is Qwen3-1.7B, deep is Qwen3-4B-Instruct-2507, think is Qwen3-4B-Thinking-2507. When the deep or think file is missing, or the 4B won't load on the NPU, the 1.7B stands in and the top bar says why (there is no Qwen2.5-7B fallback: that export fails to load on RKLLM 1.3.1). `app/config.json` is optional: put only the keys you want to change in it.

## Testing on the desktop

Without the NPU the app falls back to Ollama: `ollama pull qwen3:1.7b` and
`ollama pull qwen3:4b-instruct-2507-q4_K_M` (plus `qwen3:4b-thinking-2507-q4_K_M` for Think Harder), then run the same command.

The only writes the app can send are on the TESTS screen (above).
