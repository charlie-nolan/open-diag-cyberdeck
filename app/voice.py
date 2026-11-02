"""
Offline voice for the cyberdeck, built to the user's plan (Gemini chats Oct 7-14, review section 9).

  Vosk         always on, in a separate process pinned to the A55 cores (0-3), closed phrase
               list so shop noise is ignored. Acts only after "hey deck" (or "hey cyberdeck").
  whisper.cpp  idle until Vosk hears "intake note" / "customer complaint" (or DICTATE is pressed),
               records until ~2 s of quiet, returns a punctuated paragraph. base.en, tiny.en fallback.
  TTS          pyttsx3, else espeak-ng/espeak, or Piper if configured. Speaks answers to information
               asks only (torque, fluids, next step). Diagnoses, quotes and prices are never spoken.

The GUI never touches the microphone: VoiceClient starts `python3 voice.py --worker`, reads its JSON
lines (cmd / note / status) and writes "dictate" / "pause" / "resume" to its stdin. A stuck mic
can't freeze the screen, and the NPU and A76 cores stay free for Qwen.

Everything degrades quietly: missing vosk / sounddevice / models / whisper-cli turns voice off and
the top bar says which part is missing.
"""
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

RATE = 16000
MIC_LOST_S = 5.0   # no audio at all for this long = the mic is gone (earbuds dropped)
MIC_LOST = 3       # worker exit code for that
WAKE = re.compile(r"\b(hey|ok|okay) (deck|cyber ?deck)\b")
NOTE = re.compile(r"\b(intake note|customer complaint)\b")

# spoken phrase (after the wake phrase) -> command
COMMANDS = [
    (r"^(read|scan) (the )?car", "read_car"),
    (r"^save (the )?case", "save_case"),
    (r"^new case", "new_case"),
    (r"^(build |show )?(the )?verdict", "verdict"),
    (r"^torque (for |on |of )?(the )?(?P<arg>.+)", "torque"),
    (r"^fluid (capacity|type)? ?(for |of )?(the )?(?P<arg>.*)", "fluid"),
    (r"^(what'?s the )?(next step|step after) ?(i do |doing |after )?(?P<arg>.*)", "next_step"),
    (r"^(go to|show|open) (the )?(?P<arg>intake|chat|verdict|repair|procedure|parts)", "goto"),
    (r"^(stop|cancel|quiet)", "stop"),
]
# closed vocabulary for Vosk (plus part words the GUI passes in from the kb)
BASE_WORDS = ("hey ok okay deck cyber cyberdeck intake note customer complaint read scan the car save case new "
              "build show verdict torque for on of fluid capacity type what's next step after i do doing "
              "removing installing go to open chat repair procedure parts stop cancel quiet bolt bolts nut nuts")


def _whisper_paths(cfg):
    wbin = os.path.expanduser(cfg["whisper_bin"])
    wbin = wbin if os.path.isfile(wbin) else shutil.which(cfg["whisper_bin"]) or shutil.which("whisper-cli")
    models = [os.path.expanduser(cfg["whisper_model"]), os.path.expanduser(cfg.get("whisper_model_fallback", ""))]
    return wbin, next((m for m in models if m and os.path.isfile(m)), None)


def voice_status(cfg):
    """(listen_ok, dictate_ok, reason) without opening the microphone. Either half works without the other."""
    for mod in ("sounddevice", "numpy"):
        if importlib.util.find_spec(mod) is None:
            return False, False, f"voice off: {mod} not installed"
    listen = importlib.util.find_spec("vosk") is not None and os.path.isdir(os.path.expanduser(cfg["vosk_model"]))
    dictate = all(_whisper_paths(cfg))
    if listen and dictate:
        return True, True, "voice: say 'hey deck', or press MIC (F2)"
    if dictate:
        return False, True, "voice: press MIC to talk (no 'hey deck': Vosk not set up)"
    if listen:
        return True, False, "voice: 'hey deck' only (whisper.cpp not set up, so no dictation)"
    return False, False, "voice off: Vosk and whisper.cpp not set up (see README)"


def input_devices(refresh=False):
    """[(index, name)] of microphones, e.g. a USB headset. refresh re-scans (PortAudio caches the list)."""
    try:
        import sounddevice as sd
        if refresh:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception:
        return []


def parse_command(text):
    for pat, cmd in COMMANDS:
        m = re.search(pat, text)
        if m:
            return cmd, (m.groupdict().get("arg") or "").strip()
    return None, ""


# =========================================================================== worker process
def _emit(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def _all_cores():
    try:
        os.sched_setaffinity(0, range(os.cpu_count() or 8))
    except (AttributeError, OSError):
        pass


def _whisper(cfg, wav):
    wbin, model = _whisper_paths(cfg)
    if not wbin or not model:
        return None, "dictation off: whisper-cli or its model not found (see README)"
    # the listener sits on the small cores; whisper gets every core so a note doesn't take a minute
    out = subprocess.run([wbin, "-m", model, "-f", wav, "-nt", "-l", "en", "-t", str(cfg.get("whisper_threads", 4))],
                         capture_output=True, text=True, timeout=120, preexec_fn=_all_cores)
    text = " ".join(line.strip() for line in out.stdout.splitlines() if line.strip())
    return re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text).strip(), None  # drop [BLANK_AUDIO], (engine revving)


def _record_until_quiet(audio_q, max_s=25, quiet_s=1.5, min_level=120, levels=None, finish=None):
    """Energy VAD: stop after quiet_s of silence once speech started (Silero VAD could replace this).
    Speech = louder than 3x the room noise measured over the first half second (headset and onboard
    mics differ a lot in gain, so a fixed level missed quiet ones). Once you've spoken, anything under
    a fifth of your loudest counts as quiet too, so a fan or an idling engine can't keep it recording.
    finish (an Event) ends it early: pressing MIC again. levels gets (noise, loudest, needed)."""
    import numpy as np
    chunks, silent, spoke, start = [], 0.0, False, time.time()
    noise, loud, heard = [], 0.0, 0.0
    while time.time() - start < max_s and not (finish is not None and finish.is_set()):
        try:
            data = audio_q.get(timeout=1.0)
        except queue.Empty:
            continue
        a = np.frombuffer(data, dtype=np.int16)
        if not len(a):
            continue
        chunks.append(a)
        rms = float(np.sqrt(np.mean(a.astype(np.float32) ** 2)))
        if heard < 0.5:
            noise.append(rms)
            heard += len(a) / RATE
            continue
        need = max(min_level, 3 * float(np.median(noise)))
        loud = max(loud, rms)
        if rms > max(need, 0.2 * loud if spoke else 0):
            spoke, silent = True, 0.0
        else:
            silent += len(a) / RATE
        if (spoke and silent >= quiet_s) or (not spoke and silent >= 8):
            break
    if levels is not None:
        levels[:] = [float(np.median(noise)) if noise else 0.0, loud,
                     max(min_level, 3 * float(np.median(noise))) if noise else min_level]
    return np.concatenate(chunks) if spoke and chunks else None


def _resampler(sr, put):
    """Callback that turns sr-Hz int16 blocks into RATE-Hz ones (Bluetooth and raw hw: mics often run at 48 kHz)."""
    import numpy as np

    def cb(data, *_):
        x = np.frombuffer(bytes(data), dtype=np.int16).astype(np.float32)
        if sr % RATE == 0:      # 48000 -> 16000: average each 3 samples (also a crude low-pass)
            k = sr // RATE
            y = x[:len(x) // k * k].reshape(-1, k).mean(axis=1)
        else:
            n = int(len(x) * RATE / sr)
            y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x) if n else x[:0]
        if len(y):
            put(y.astype(np.int16).tobytes())
    return cb


def _open_mic(sd, want, put):
    """Start the mic, 16 kHz mono int16 into put(). Tries the picked mic, then the PipeWire/Pulse default
    source (where Bluetooth headsets show up), then the system default; at 16 kHz first, else at the
    device's own rate with resampling. Returns (stream, mic name, rate opened)."""
    names = [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]
    tries = ([want] if want else []) + [n for n in ("pipewire", "pulse", "default") if n in names and n != want]
    tries.append(None)
    err = None
    for dev in tries:
        for native in (False, True):
            try:
                sr = int(sd.query_devices(dev, "input")["default_samplerate"]) if native else RATE
                if native and sr == RATE:
                    continue
                cb = _resampler(sr, put) if sr != RATE else (lambda data, *_: put(bytes(data)))
                st = sd.RawInputStream(samplerate=sr, blocksize=sr // 4, dtype="int16", channels=1,
                                       device=dev, callback=cb)
                st.start()
                return st, dev or "system default", sr
            except Exception as e:
                err = e
    raise RuntimeError(err)


def worker(cfg_path, words_path=None):
    with open(cfg_path) as f:
        cfg = json.load(f)
    try:
        os.sched_setaffinity(0, {0, 1, 2, 3})  # A55 efficiency cores on the RK3588
    except (AttributeError, OSError):
        pass
    try:
        import sounddevice as sd
    except Exception as e:
        _emit(status=f"voice off: {e}")
        return
    rec = None  # no Vosk = no "hey deck", but the MIC button (whisper.cpp) still works
    try:
        import vosk
        vosk.SetLogLevel(-1)
        model = vosk.Model(os.path.expanduser(cfg["vosk_model"]))
        words = set(BASE_WORDS.split())
        if words_path and os.path.isfile(words_path):
            with open(words_path) as f:
                words |= set(json.load(f))
        rec = vosk.KaldiRecognizer(model, RATE, json.dumps(sorted(words) + ["[unk]"]))
    except Exception:
        pass

    audio_q, control = queue.Queue(), queue.Queue()
    dictating, finish = threading.Event(), threading.Event()

    def read_stdin():
        for line in sys.stdin:
            line = line.strip()
            if line == "dictate" and dictating.is_set():
                finish.set()  # MIC pressed again while dictating = done talking
            else:
                control.put(line)
        control.put("quit")

    threading.Thread(target=read_stdin, daemon=True).start()
    paused = False
    last_audio = [time.time()]

    def dictate():
        dictating.set()
        finish.clear()
        try:
            _dictate()
        finally:
            dictating.clear()
            finish.clear()
            last_audio[0] = time.time()  # whisper's run isn't a silent mic
            while not audio_q.empty():  # what the mic heard while whisper ran isn't a wake word
                audio_q.get_nowait()

    def _dictate():
        _emit(status="dictating... (pause, or press MIC/F2, to finish)")
        while not audio_q.empty():
            audio_q.get_nowait()
        lv = []
        audio = _record_until_quiet(audio_q, levels=lv, finish=finish)
        if audio is None:
            if lv and lv[1] < 1:
                why = "the mic sent silence (wrong mic, or muted)"
            elif lv:
                why = f"loudest {lv[1]:.0f}, speech needs {lv[2]:.0f}: speak closer or pick another mic"
            else:
                why = "no sound came from the mic"
            _emit(status=f"dictation: heard nothing ({why})", note="")
            return
        _emit(status="transcribing...")
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "note.wav")
            with wave.open(wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(RATE)
                w.writeframes(audio.tobytes())
            try:
                text, err = _whisper(cfg, wav)
            except Exception as e:
                text, err = None, f"whisper error: {e}"
        if err is None and not text:
            err = "dictation: whisper heard no words"
        if err:
            _emit(status=err, note="")
        else:
            _emit(status="note transcribed", note=text)
        if rec is not None:
            rec.Reset()

    try:
        stream, mic, sr = _open_mic(sd, cfg.get("mic_device") or None, audio_q.put)
    except Exception as e:
        _emit(status=f"voice off: no microphone opened ({e}). Bluetooth earbuds: switch them to the "
                      "headset profile, then pick 'Mic: system default'")
        return
    want = cfg.get("mic_device")
    where = f"mic: {mic}" + (f" at {sr} Hz" if sr != RATE else "") + (f"; '{want}' didn't open" if want and mic != want else "")
    try:
        last_audio[0] = time.time()
        with stream:
            _emit(status=(f"voice ({where}): say 'hey deck', or press MIC (F2)" if rec is not None
                          else f"voice ({where}): press MIC to talk"))
            while True:
                while not control.empty():
                    c = control.get_nowait()
                    if c == "quit":
                        return
                    if c in ("pause", "resume"):
                        paused = c == "pause"
                        if not paused:  # drop what the mic heard while the deck was talking
                            while not audio_q.empty():
                                audio_q.get_nowait()
                            if rec is not None:
                                rec.Reset()
                    if c == "dictate":
                        dictate()
                try:
                    data = audio_q.get(timeout=0.3)
                    last_audio[0] = time.time()
                except queue.Empty:
                    if not paused and time.time() - last_audio[0] > MIC_LOST_S:
                        # earbuds dropped or the device vanished: say so and exit, MIC starts it again
                        _emit(status="mic lost: press MIC to restart")
                        return MIC_LOST
                    continue
                if paused or rec is None or not rec.AcceptWaveform(data):
                    continue
                text = json.loads(rec.Result()).get("text", "").replace("[unk]", "").strip()
                text = re.sub(r"\s+", " ", text)
                if not text:
                    continue
                m = WAKE.search(text)
                if m:
                    rest = text[m.end():].strip()
                    if not rest:  # "hey deck" alone: hand straight to whisper, like pressing MIC
                        _emit(cmd="dictating", arg="here", heard=text)
                        dictate()
                        continue
                else:
                    continue
                if NOTE.search(rest):
                    _emit(cmd="dictating", arg="intake", heard=text)
                    dictate()
                    continue
                cmd, arg = parse_command(rest)
                if cmd:
                    _emit(cmd=cmd, arg=arg, heard=text)
                else:  # not a command word: whisper takes it from here (say it again)
                    _emit(cmd="dictating", arg="here", heard=f"{text} (not a command: say it again)")
                    dictate()
    except Exception as e:
        _emit(status=f"voice off: microphone error ({e})")


# =========================================================================== GUI side
try:
    from PyQt6.QtCore import QThread, pyqtSignal

    class VoiceClient(QThread):
        """Runs the worker process and turns its JSON lines into Qt signals."""
        command = pyqtSignal(str, str)
        note = pyqtSignal(str)
        status = pyqtSignal(str)

        def __init__(self, cfg, words=()):
            super().__init__()
            self.cfg, self.words, self.proc = cfg, list(words), None

        def run(self):
            d = tempfile.mkdtemp(prefix="deckvoice_")
            cfg_path, words_path = os.path.join(d, "cfg.json"), os.path.join(d, "words.json")
            with open(cfg_path, "w") as f:
                json.dump(self.cfg, f)
            with open(words_path, "w") as f:
                json.dump(self.words, f)
            self.proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker", cfg_path, words_path],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
            for line in self.proc.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "status" in msg:
                    self.status.emit(msg["status"])
                if "cmd" in msg:
                    self.status.emit(f"heard: {msg.get('heard', '')}")
                    self.command.emit(msg["cmd"], msg.get("arg", ""))
                if "note" in msg:
                    self.note.emit(msg["note"])
            code = self.proc.wait()
            if code not in (0, None):  # crashed: say so instead of leaving "dictating..." up
                if code != MIC_LOST:  # mic lost already said so itself
                    self.status.emit("voice stopped: press MIC to restart it")
                self.note.emit("")

        def send(self, line):
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass

        def stop(self):
            self.send("quit")
            if self.proc:
                try:
                    self.proc.wait(2)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
except ImportError:  # the worker process doesn't need Qt
    pass


_SPEAK_LOCK = threading.Lock()
_speaking = []  # the running espeak/aplay process, stopped when a newer answer starts


def _stop_speaking():
    while _speaking:
        p = _speaking.pop()
        if p.poll() is None:
            p.terminate()


def speak(cfg, text):
    """Speak the answer to an information ask. Never call this with a diagnosis, quote or price.
    Returns False when no speech engine is installed."""
    if not text:
        return True
    text = text[:300]
    _stop_speaking()  # two quick asks: the newer answer replaces the older one instead of talking over it
    piper, voice_model = cfg.get("piper_bin") or "", os.path.expanduser(cfg.get("piper_model") or "")
    if piper and shutil.which(piper) and os.path.isfile(voice_model) and shutil.which("aplay"):
        scale = 175 / max(60, int(cfg.get("tts_rate", 145)))  # piper: >1 is slower
        p1 = subprocess.Popen([shutil.which(piper), "--model", voice_model, "--output-raw",
                               "--length_scale", f"{scale:.2f}"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        _speaking.append(subprocess.Popen(["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-t", "raw", "-"],
                                          stdin=p1.stdout))
        _speaking.append(p1)
        p1.stdin.write(text.encode())
        p1.stdin.close()
        return True
    if importlib.util.find_spec("pyttsx3") is not None:
        def _say():
            with _SPEAK_LOCK:  # one pyttsx3 engine at a time; a second ask waits its turn
                import pyttsx3
                engine = pyttsx3.init()
                engine.setProperty("rate", int(cfg.get("tts_rate", 145)))
                engine.say(text)
                engine.runAndWait()
        threading.Thread(target=_say, daemon=True).start()
        return True
    cmd = shutil.which("espeak-ng") or shutil.which("espeak")
    if cmd:
        _speaking.append(subprocess.Popen([cmd, "-s", str(int(cfg.get("tts_rate", 145))), text],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return True
    return False


if __name__ == "__main__" and len(sys.argv) >= 3 and sys.argv[1] == "--worker":
    sys.exit(worker(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None) or 0)
