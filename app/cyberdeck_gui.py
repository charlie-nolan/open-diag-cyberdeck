#!/usr/bin/env python3
"""
Cyberdeck diagnostic GUI (one knowledge DB per make): two local models, no agents.

  FAST model  (default Qwen3-1.7B)              -> 2. Chat / brainstorm
  DEEP model  (default Qwen3-4B-Instruct-2507)  -> 3. Final verdict, 4. Repair SOP

Answers are grounded in the make's knowledge DB, kb/<make>.db (factory manual sections, DTC procedures, specs,
TSBs), through kb/kb_retriever.py. Torque specs are looked up, never generated. A Subaru deck without
kb/subaru.db falls back to the legacy retriever (app/retrieval.py: subaru_diag.db + test_subaru_knowledge.db).

Backend: RKLLM on the Orange Pi (aarch64 + librkllmrt.so present), Ollama on the desktop.
Settings: app/config.json (optional; only the keys you want to change. The mic and cable picks are saved there).
"""
import datetime
import faulthandler
import glob
import hashlib
import html
import json
import os
import platform
import queue
import re
import shlex
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error

from PyQt6.QtCore import QObject, QProcess, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (QColor, QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut, QTextCharFormat, QTextCursor,
                         QTextDocument)
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
                             QTabWidget,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow, QMessageBox,
                             QPlainTextEdit,
                             QPushButton, QScrollArea,
                             QStackedWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ssm"))  # ssm_tests, telemetry_logger
from llm_backend import (ClaudeModel, OllamaModel, OpenAICompatModel, RKLLMModel, mem_available_gb, mem_total_gb, ollama_models,  # noqa: E402
                         probe_rkllm)
from retrieval import Retriever as LegacyRetriever  # noqa: E402
from vehicle_id import MAKES, car_from_rom, decode_vin, kb_path_for, make_from_ssm, nhtsa_decode  # noqa: E402
import car_records  # one SQLite history file per car (records/<VIN>.db)
import charm_pics
import common_issues
import drive_file
import live_data
import maintenance
import procedures
import sanity_check  # DATA CHECK: impossible/contradictory case data, in Python, before the models
import obd_all  # every OBD-II PID the car offers, through the EX
import telemetry_ref  # live readings vs the car's manual zones (kb telem_* tables, kb/build_telemetry.py)
import voice  # noqa: E402
import theme as T  # The user's palette, shared with diagent (dark only)
from ui_fit import FitRoot, ReflowRow  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "vehicle": "",  # no default car: pick it in Intake (remembered per session)
    "backend": "auto",  # auto | rkllm | ollama | openai | claude
    # backend "openai": any OpenAI-compatible server (LM Studio, llama.cpp, Jan, vLLM, OpenRouter, ...). Its key, if
    # it needs one, is read from the environment variable named in openai_key_env, never stored here.
    "openai_url": os.environ.get("CYBERDECK_OPENAI_URL", "http://127.0.0.1:1234/v1"),
    "openai_key_env": "OPENAI_API_KEY",
    "openai_fast": "", "openai_deep": "", "openai_think": "",   # model names as the server lists them
    # backend "claude": the Claude API (pip install anthropic; key in ANTHROPIC_API_KEY or `ant auth login`)
    "claude_fast": "claude-opus-5-5", "claude_deep": "claude-opus-5-5", "claude_think": "claude-opus-5-5",
    "claude_effort": {"fast": "low", "deep": "high", "think": "high"},
    # Lists = first file that exists wins, so the Qwen3 exports take over from Qwen2.5 with no config edit.
    "rkllm_lib": [f"{ROOT}/rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so", "/usr/lib/librkllmrt.so"],
    "fast_model": [f"{ROOT}/models/rkllm/Qwen3-1.7B_W8A8_RK3588.rkllm",
                   f"{ROOT}/models/rkllm/qwen2.5-3b-w8a8-rk3588.rkllm"],
    # No Qwen2.5-7B fallback: that old export fails rkllm_init on runtime 1.3.1. Missing 4B = the fast model.
    "deep_model": f"{ROOT}/models/rkllm/Qwen3-4B-Instruct-2507_W8A8_RK3588.rkllm",
    "think_model": f"{ROOT}/models/rkllm/Qwen3-4B-Thinking-2507_W8A8_RK3588.rkllm",  # opt-in "think harder"
    "ollama_fast": "qwen3:1.7b",
    "ollama_deep": "qwen3:4b-instruct-2507-q4_K_M",
    "ollama_think": "qwen3:4b-thinking-2507-q4_K_M",
    "max_context_len": 4096,
    # Cyberdeck Home (--home, or CYBERDECK_HOME=1): the models run on the home PC's Ollama over Tailscale;
    # the deck's own models take over whenever the PC can't be reached
    "home": False,
    "home_url": os.environ.get("CYBERDECK_HOME_URL", "http://127.0.0.1:11434"),  # your PC's Ollama, e.g. its Tailscale address
    # 10-21: Qwen3.6 35B for chat, Qwen3-Next 80B instruct for verdict/repair, its thinking twin for
    # THINK HARDER; Ollama swaps them in the PC's RAM as needed
    "home_fast": "qwen3.6:35b-a3b-q4_K_M",
    "home_deep": "qwen3-next:80b-a3b-instruct-q4_K_M",
    "home_think": "qwen3-next:80b-a3b-thinking-q4_K_M",
    "home_context_len": 16384,
    "keep_both_loaded": "auto",  # auto = both if >= 12 GB RAM, else swap on demand
    "verdict_blind_first": True,  # FINAL DIAGNOSIS in two passes: evidence only, then the opinions (OPINION_FIELDS)
    "kb_dir": f"{ROOT}/kb",  # one knowledge DB per make: kb/subaru.db, kb/chevrolet.db ... picked in Intake
    "vin_db": f"{ROOT}/kb/vin.db",  # optional: NHTSA vPIC makes for every WMI (see VIN_DECODING.md)
    "rom_db": f"{ROOT}/kb/romid.db",  # optional: Subaru ECU id -> year/model (vehicle_id.py --build-romdb)
    "kb_db": "",  # set from the car's make at startup and whenever the make changes
    "make": "",
    "kb_model": None,  # follows the car picked in Intake
    "kb_year": None,
    "diag_db": f"{ROOT}/subaru_diag.db",
    "knowledge_db": f"{ROOT}/test_subaru_knowledge.db",
    "session_db": f"{ROOT}/session_active.db",
    "cache_dir": f"{ROOT}/cache",
    "pic_url": os.environ.get("CYBERDECK_PIC_URL", "http://127.0.0.1:8765"),  # home PC's tools/charm_pic_server.py: CHARM page pictures
    "charm_pics_max": 8,  # most CHARM pictures shown per page
    "voice_enabled": True,  # Vosk "hey deck" + whisper.cpp dictation; off automatically if not installed
    "vosk_model": f"{ROOT}/models/voice/vosk-model-small-en-us-0.15",
    "whisper_bin": f"{ROOT}/models/voice/whisper.cpp/build/bin/whisper-cli",  # or "whisper-cli" on PATH
    "whisper_model": f"{ROOT}/models/voice/ggml-base.en.bin",
    "whisper_model_fallback": f"{ROOT}/models/voice/ggml-tiny.en.bin",  # used if base.en is missing
    "whisper_threads": 4,
    "piper_bin": "",  # optional nicer voice, e.g. "piper"; else pyttsx3, else espeak-ng
    "piper_model": "",
    "mic_device": "",  # empty = system default
    "tts_rate": 145,  # speaking speed, words per minute (espeak's own default of 175+ was too fast)
    "obd_adapter": "obdlink",  # obdlink = OBDLink EX: OBD-II + VIN, then Subaru SSM over it | kkl = bare KKL fallback
    "ex_ssm_variant": "",  # STN settings that last got an SSM answer through the EX (remembered)
    # Other programs per make, opened from Intake with the cable handed over, e.g.
    # "Ford": {"name": "FORScan", "cmd": "wine '~/.wine/drive_c/Program Files (x86)/FORScan/FORScan.exe'"}
    "launchers": {},
    "obd_port": "",  # empty = auto (ssm/cables.py tells the KKL cable and the OBDLink EX apart)
    "embed_model": "",  # vector search: "" = find Qwen3-Embedding-0.6B by itself (models/qwen3emb, then the
                        # Hugging Face cache), a folder path = use that, "off" = keyword search only
    "embed_threads": 4,
    "parts_sourcing_dir": f"{ROOT}/parts_sourcing",  # Parts tab (part numbers, QR links, junkyard quote)
}

OLD_HOME_MODELS = {"qwen3:8b", "qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3:30b-a3b-thinking-2507-q4_K_M"}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg.update(json.load(f))
        except (OSError, ValueError) as e:  # half-written file: start on the defaults instead of not at all
            print(f"config.json unreadable ({e}); using defaults")
    # PC model names from an earlier setup left in config.json would hide newer defaults: drop them
    for key in ("home_fast", "home_deep", "home_think"):
        if cfg.get(key) in OLD_HOME_MODELS:
            cfg[key] = DEFAULT_CONFIG[key]
    cfg = {k: os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v for k, v in cfg.items()}
    for key in ("rkllm_lib", "fast_model", "deep_model", "think_model"):
        # config value(s) first, then the defaults; a stale path in config.json falls through
        cands = [cfg[key]] if isinstance(cfg[key], str) else list(cfg[key])
        default = DEFAULT_CONFIG[key]
        cands += [default] if isinstance(default, str) else default
        cands = [os.path.expanduser(c) for c in cands]
        cfg[key] = next((c for c in cands if os.path.isfile(c)), cands[0])
    # The deep (and think) model not copied yet: use the fast one and say so, rather than fail on a missing file.
    cfg["stand_in"] = {}
    for key in ("deep_model", "think_model"):
        if not os.path.isfile(cfg[key]) and os.path.isfile(cfg["fast_model"]):
            cfg["stand_in"][key[:-6]] = "file not on the deck yet"
            cfg[key] = cfg["fast_model"]
    # Voice files: models/voice/ (README) or models/vosk/ + models/whisper/ (The user's PC). A path in
    # config.json that doesn't exist must not switch voice off when the files are in the other layout.
    for key, exists in (("vosk_model", os.path.isdir), ("whisper_model", os.path.isfile),
                        ("whisper_model_fallback", os.path.isfile)):
        name = os.path.basename(DEFAULT_CONFIG[key])
        cands = [os.path.expanduser(cfg[key]), DEFAULT_CONFIG[key],
                 f"{ROOT}/models/{'vosk' if key == 'vosk_model' else 'whisper'}/{name}"]
        cfg[key] = next((c for c in cands if exists(c)), cands[0])
    return cfg


CFG = load_config()

PS_DIR = os.path.expanduser(CFG["parts_sourcing_dir"])
sys.path.insert(0, PS_DIR)
try:
    import parts_sourcing as PS
    from parts_sourcing import qr as PSQR, sheet as PSSHEET
    from parts_sourcing.interchange import is_part_number   # any make's format, not only Subaru's
except ImportError:
    PS = PSQR = PSSHEET = None

    def is_part_number(s):
        return False


FAST_SYSTEM = ("You are an experienced mechanic talking a colleague through a car problem. The vehicle is the one "
               "named in the CASE. Reply like a conversation: answer exactly what was asked in a few plain "
               "sentences, no headings, no lists, no summary. Prefer cheap checks before parts. Use the FSM "
               "data when it helps; if it doesn't cover the question, say so in one sentence. Never invent "
               "torque specs, pinouts or part numbers. Use the manual's diagnostic tree as your guide and the "
               "live data meanings the deck worked out. Never invent facts about the car: if you don't know, "
               "say \"I don't know\" and what test would tell. An FSM excerpt about an unrelated part is not "
               "evidence; ignore it.")
CHAT_STYLE = ("(Answer the mechanic's last line conversationally in 2-5 sentences, no headings or lists. "
              "Let the manual's diagnostic tree guide what you suggest checking next, but talk it through in "
              "your own words instead of reciting steps. "
              "Only say what the CASE, FSM DATA or well-known mechanics support; if you don't know, say so and "
              "name the test that would tell. Don't assume anything the case doesn't say.)")
DEEP_SYSTEM = ("You are a master automotive technician. The vehicle is the one named in the CASE. Trust, in order: the "
               "manual's diagnostic procedure, the diagnostic card, other manual pages, then general knowledge. "
               "Use the live data meanings the deck worked out instead of judging raw numbers. Base conclusions on the "
               "evidence and FSM excerpts provided. If the evidence does not support a conclusion, say what "
               "test is still needed. Never invent torque specs, pinouts or part numbers; only use values "
               "that appear in the provided data.")


class NullRetriever:
    """No knowledge DB for this make (or no make picked): codes, chat and verdict still work, without manuals."""
    db = None

    def context_for(self, *a, **k):
        return ""

    def __getattr__(self, name):  # search_fsm, specs, labor ...: nothing, never a crash
        return lambda *a, **k: []


def kb_status(make):
    """(path, exists, plain message) for a make's knowledge DB."""
    if not make:
        return "", False, "No car picked yet: choose a Make, or type the VIN, to load its manuals."
    path, ok = kb_path_for(make, CFG["kb_dir"])
    if ok:
        return path, True, f"Manuals: {os.path.basename(path)}"
    return path, False, (f"No database for {make} yet ({os.path.basename(path)} is not in {CFG['kb_dir']}). "
                         "Codes, chat and verdict still work, without manual pages, specs or parts.")


def ecu_vins():
    """{ECU id: VIN} typed in for cars that don't report a VIN themselves."""
    try:
        with open(os.path.join(CFG["cache_dir"], "ecu_vins.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def remember_ecu_vin(ecu, vin):
    known = ecu_vins()
    if known.get(ecu) == vin:
        return
    known[ecu] = vin
    try:
        os.makedirs(CFG["cache_dir"], exist_ok=True)
        with open(os.path.join(CFG["cache_dir"], "ecu_vins.json"), "w") as f:
            json.dump(known, f, indent=1)
    except OSError:
        pass


def use_make(make):
    """Point CFG at a make's knowledge DB. Returns the plain status message."""
    CFG["make"] = make or ""
    CFG["kb_db"], _, msg = kb_status(make)
    return msg


def find_embed_model(cfg):
    """Folder of Qwen3-Embedding-0.6B for vector search, or '' (keyword search only). Never downloads."""
    want = (cfg.get("embed_model") or "").strip()
    if want.lower() == "off":
        return ""
    if want:
        return os.path.expanduser(want)
    hubs = [os.environ.get("HF_HUB_CACHE", ""), os.path.join(os.environ.get("HF_HOME", ""), "hub"),
            os.path.expanduser("~/.cache/huggingface/hub"), f"{ROOT}/models/hf_cache/.cache/huggingface/hub"]
    folders = [f"{ROOT}/models/qwen3emb"] + [d for h in hubs if h for d in sorted(
        glob.glob(os.path.join(h, "models--Qwen--Qwen3-Embedding-0.6B", "snapshots", "*")), reverse=True)]
    return next((d for d in folders if os.path.isfile(os.path.join(d, "config.json"))), "")


def squeeze(t):
    """Markdown table rules ("|------|") waste the small models' context."""
    return re.sub(r"-{4,}", "---", t)


FIG_W = 460  # widest a manual figure is drawn, in px


def fsm_hits(r, text, k=3, chars=900):
    """search_fsm, plus each hit's manual figures: [(text, [figure files])]. The files are
    images/<doc id>/<name>.webp next to the kb (kb/ship_images.py makes them), in the section's own
    order, which is the order of its "[fig]" marks. A file that isn't there is skipped when shown."""
    kb = os.path.expanduser(CFG.get("kb_db") or "")
    if not all(hasattr(r, a) for a in ("search_ids", "_row", "_fmt", "db")) or not os.path.isfile(kb):
        return [(h, []) for h in r.search_fsm(text, k=k, chars=chars)]
    img_dir = os.path.join(os.path.dirname(kb), "images")
    out, seen = [], set()
    for sid in r.search_ids(text, k * 3):  # same picks as kb_retriever.search_fsm
        row = r._row(sid)
        if row is None:
            continue
        path, page, body, kind = row
        base = path.split(" — ")[0]
        if base in seen:
            continue
        seen.add(base)
        figs = []
        meta = r.db.execute("SELECT doc_id, images FROM section WHERE id=?", (sid,)).fetchone()
        if meta and meta[1]:
            try:
                figs = []
                for n in json.loads(meta[1]):
                    if charm_pics.is_charm(n):            # CHARM: from the DB or the PC when shown
                        figs.append(n)
                        continue
                    f = os.path.join(img_dir, str(meta[0]), os.path.splitext(n)[0] + ".webp")
                    if not os.path.isfile(f):             # a one-car DB keeps its figures inside it
                        f = charm_pics.fsm_figure(kb, CFG["cache_dir"], meta[0], n) or f
                    figs.append(f)
            except (ValueError, TypeError):
                figs = []
        out.append((squeeze(r._fmt(path, page, body, chars, kind)), figs))
        if len(out) == k:
            break
    return out


def log_caught(where, e):
    """For handlers that degrade instead of crashing (a field tool keeps going): say what was caught."""
    print(f"[{where}] {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def load_figure(path):
    """QPixmap of a figure, or a null one. Pillow decodes it if Qt has no WebP plugin."""
    pm = QPixmap(path)
    if pm.isNull():
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("L")
                qi = QImage(im.tobytes(), im.width, im.height, im.width, QImage.Format.Format_Grayscale8)
                pm = QPixmap.fromImage(qi.copy())
        except Exception as e:
            log_caught(f"figure {os.path.basename(path)}", e)
    if not pm.isNull() and pm.width() > FIG_W:
        pm = pm.scaledToWidth(FIG_W)
    return pm


def spaced(text):
    """A blank line between every line of a result, so each bit of info stands apart."""
    return re.sub(r"\n\s*\n*", "\n\n", (text or "").strip())


# Words that start a repair job but aren't part of a part's name. Vocabulary only (no car or cause in it).
JOB_VERBS = ("replace", "replacing", "install", "reinstall", "remove", "renew", "repair", "rebuild", "clean", "inspect",
             "check", "test", "tighten", "retighten", "torque", "retorque", "re-torque", "adjust", "reseal",
             "resurface", "fit", "swap", "change", "flush", "bleed", "refill", "top up", "lubricate", "reconnect",
             "secure", "order", "buy", "fix", "machine", "cut", "drain", "fill", "reset", "clear", "verify")


def parts_from_job(job):
    """The parts a repair line names, for the parts search: 'Replace exhaust manifold gasket, clean mating
    surfaces, torque to spec.' -> ['exhaust manifold gasket']; 'Replace spark plugs and ignition coil' ->
    ['spark plugs', 'ignition coil']. Text without an action word comes back as it is (already a part name or
    a part number)."""
    if not job:
        return []
    v = "|".join(re.escape(x) for x in JOB_VERBS)
    t = re.sub(r"\([^)]*\)", " ", job.strip())                              # '(belt, tensioner, idlers)'
    t = re.sub(r"(?i)^\s*(?:(?:%s)\b\s*(?:(?:and|&|,|/)\s*)?)+" % v, "", t)   # 'Clean and inspect ...'
    t = re.split(r"[,;:.]|\s+(?:and then|then|and)\s+(?=(?:%s)\b)" % v, t, maxsplit=1)[0]  # stop at the next job
    t = re.sub(r"(?i)\s+(?:if|as needed|per|with|to spec|on|at|for|from|in)\b.*$", "", t)
    out = []
    for n in re.split(r"(?i)\s+(?:and|&)\s+|\s*/\s*", t):
        n = re.sub(r"(?i)^(?:the|a|an|new|both|all)\s+", "", n.strip())
        n = re.sub(r"(?i)^(?:#\s*\d+|no\.\s*\d+|cylinder\s*\d+)\s+", "", n).strip(" .-")
        if n:
            out.append(n)
    return out or [job.strip()]


def part_from_job(job):
    """The first part a repair line names (see parts_from_job)."""
    p = parts_from_job(job)
    return p[0] if p else ""


def confirmed_fixes(r, text, k=3, chars=400):
    """Forum fixes (knowledge kind forum_fix) the owner confirmed, for this car's model and years, closest to
    text first. Every make DB has them (make_full step e). [] when none or on any error."""
    try:
        ids = r.knowledge_ids(text, k=k * 4, kinds=["forum_fix"])
        out = []
        for i in ids:
            row = r.db.execute("SELECT model, year_min, title, body, url, confirmed FROM knowledge WHERE id=?",
                               (i,)).fetchone()
            if not row or not row[5]:
                continue
            model, yr, title, body, url, _ = row
            body = " ".join((body or "").split())
            body = body if len(body) <= chars else body[:chars].rsplit(" ", 1)[0] + " …"
            out.append(f"- {model} {yr or ''}: {title or ''} {url or ''}\n  {body}")
            if len(out) == k:
                break
        return out
    except Exception as e:
        print(f"confirmed fixes skipped: {e}")
        return []


def community_block(r, text, codes, cap, raw=True):
    """Community reports for a case, below the manual and the cards (10-31): what owners extracted threads
    say (comm_* tables, when the make has them), then the closest unchecked threads (kb/<make>.threads.db). Every
    line keeps its link. Empty when the make has neither, or on any error."""
    path = getattr(r, "kb_path", None)
    if not path or cap < 200:
        return ""
    try:
        import community_ref
        model, year, embed = getattr(r, "model", None), getattr(r, "year", None), getattr(r, "embed", None)
        code = codes[0] if codes else None
        lines = []
        try:
            c = community_ref.community(path, code=code, text=text, model=model, year=year, embed=embed, top=4)
        except sqlite3.Error:
            c = {"causes": []}
        for x in c["causes"]:
            lines.append(f"- {x['cause']}: {x['threads']} threads, {x['op_confirmed']} confirmed fixed by the owner "
                         f"({x['car_match']})" + (f" {x['urls'][0]}" if x["urls"] else ""))
        if lines:
            lines.insert(0, "[COMMUNITY REPORTS: from owner threads; they suggest what to test, the manual confirms]")
        if not raw:     # chat: forum fixes the owner said fixed it, found by the case's and the question's words
            fx = confirmed_fixes(r, text, k=3, chars=max(150, cap // 5))
            if fx:
                lines.append("[CONFIRMED OWNER FIXES: same model; the owner reported this fixed it]")
                lines += fx
        t = (community_ref.raw_threads(path, text=text, code=code, model=model, year=year, embed=embed, k=2,
                                       excerpt=max(200, min(1200, cap // 3))) if raw else {"threads": []})
        if t["threads"]:
            lines.append("[OWNER THREADS: unchecked posts, ideas only, never a spec or a step]")
            for x in t["threads"]:
                lines.append(f"- {x['title']} ({x['car_match']}) {x['url'] or ''}\n  "
                             + " ".join(re.sub(r"\[S\d+[^\]]*\]", " ", x["excerpt"]).split()))
        # whole entries only: slicing the joined text cut a thread off mid-sentence. A header with nothing
        # under it is dropped too.
        out, used = [], 0
        for i, ln in enumerate(lines):
            if used + len(ln) + 1 > cap:
                break
            out.append(ln)
            used += len(ln) + 1
        while out and out[-1].startswith("["):
            out.pop()
        return "\n".join(out)
    except Exception as e:
        print(f"community lookup skipped: {e}")
        return ""


def case_context(r, text, budget=2500, scraped=True, verified_only=False):
    """Manual text for a case, manual first (10-19). Each code's diagnostic procedure comes from
    procedures.py as whole numbered steps (the old cut kept ~600-1500 chars of a 3-18k step table, so the
    models never saw a step), with the steps that only ask which codes are set answered from the case.
    Codes the manual handles in one procedure (several codes can point to one chart) share it. Then the
    card's likely causes (forum/TSB, secondary), then the best other manual page. scraped=False leaves out
    everything not from a manual (cards, TSB/complaint/forum snippets). verified_only=True keeps the manual first
    and adds only fixes owners confirmed (the card's causes, extracted community fixes), not the unchecked
    threads: chat uses that since 11-01 (it dismissed real ideas for not being on the tree)."""
    codes = list(dict.fromkeys(c.upper() for c in re.findall(r"\b[PBCU]\d{4}\b", text.upper())))
    if not codes or not hasattr(r, "card") or not getattr(r, "db", None):
        if scraped or not hasattr(r, "search_fsm"):
            comm = community_block(r, text, [], budget // 6, raw=not verified_only) if scraped else ""
            if verified_only and hasattr(r, "search_fsm"):     # manual pages only (no complaint/TSB snippets)
                manual = "\n\n".join(r.search_fsm(text, k=2))[:max(0, budget - len(comm))]
            else:
                manual = r.context_for(text, budget=budget - len(comm))
            return "\n\n".join(x for x in (manual, comm) if x)
        return "\n\n".join(r.search_fsm(text, k=2))[:budget]
    head = (f"[CODES TOGETHER] {', '.join(codes)}: this car has all of them at once; treat them as one problem."
            if len(codes) > 1 else "")
    defs, procs, seen = [], [], set()
    for code in codes:
        first = (r.dtc(code, chars=0) or "").split("\n", 1)[0]
        if first.startswith("[DTC]"):
            defs.append(first)
        p = procedures.fetch(r, code)
        key = p["title"] if p else code
        if key in seen:
            continue
        seen.add(key)
        procs.append((p, code))
    share = int(budget * 0.85 / max(1, len(procs)))
    blocks = []
    for p, code in procs:
        if p:
            blocks.append(procedures.render(p, codes, share))
        else:  # no step table in the kb for this code: the raw text, cleaned
            raw = squeeze(re.sub(r"(?m)^\|?[\s\-:|]+\|?$|^\[DTC\].*$", "", r.dtc(code, chars=share) or ""))
            if raw.strip():
                blocks.append("[MANUAL TEXT for " + code + "]\n" + raw)
    cards = []
    for code in (codes if scraped else []):
        c = r.card(code)
        if c:  # likely causes only: the card's "manual procedure outline" was summarised from cut-off text
            c = c.split("Manual procedure outline:", 1)[0].strip()
            cards.append(c[:max(200, budget // (6 * len(codes)))])
    cards = (["[REPORTED CAUSES: forum and TSB fixes counted for this car; secondary to the manual]"] + cards
             if cards else [])
    # the codes' own names find the right manual page when the question's words don't ("explain deeper")
    query = " ".join([text] + [d.split(":", 1)[-1] for d in defs])
    # a page about one of the case's own codes is already covered by its procedure above
    pages = [h for h in r.search_fsm(query, k=2) if not any(c in h.split("\n", 1)[0] for c in codes)][:1]
    parts = [x for x in [head] + defs + blocks + cards + pages if x]
    comm = (community_block(r, query, codes, budget // 6, raw=not verified_only)   # last, in its own share
            if scraped else "")
    out, used = [], len(comm)
    for part in parts:
        part = _fit(part, budget - used)
        if part:
            out.append(part)
            used += len(part) + 2
    return "\n\n".join(out + ([comm] if comm else []))


def _fit(text, room):
    """text cut to room chars at a line end (else a word end, marked '…'), never mid-word."""
    if len(text) <= room:
        return text
    if room < 40:
        return ""
    cut = text[:room]
    nl = cut.rfind("\n")
    if nl > room // 2:
        return cut[:nl]
    return cut[:room - 1].rsplit(" ", 1)[0] + "…"


def make_retriever(cfg):
    """The make's kb (sourced torque specs, whole procedures) when it exists. Subaru alone keeps the older two
    DBs as a fallback; any other make without a kb gets an empty retriever (Intake says so)."""
    kb = os.path.expanduser(cfg.get("kb_db", ""))
    if not (kb and os.path.isfile(kb)) and cfg.get("make") != "Subaru":
        return NullRetriever()
    if kb and os.path.isfile(kb):
        sys.path.insert(0, os.path.dirname(kb))
        if f"{ROOT}/kb" not in sys.path:    # a one-car DB lives in kb/cars/<car>/: the retriever stays in kb/
            sys.path.insert(1, f"{ROOT}/kb")
        from kb_retriever import Retriever
        embed = None
        model_dir = find_embed_model(cfg)
        if model_dir:                       # Qwen3-Embedding-0.6B folder: turns on vector search
            try:
                import embedder
                embed = embedder.shared(model_dir, threads=cfg.get("embed_threads", 4))
                embed.warm()                # loads in the background, so the first question doesn't wait
            except Exception as e:
                print(f"vector search off: {e}")
        r = Retriever(kb, model=cfg.get("kb_model"), year=cfg.get("kb_year"), embed=embed)
        r.kb_path = kb                      # community_block finds kb/<make>.threads.db next to it
        ctx, fsm = r.context_for, r.search_fsm
        r.context_for = lambda *a, **k: squeeze(ctx(*a, **k))
        r.search_fsm = lambda *a, **k: [squeeze(h) for h in fsm(*a, **k)]
        return r
    return LegacyRetriever(cfg["diag_db"], cfg["knowledge_db"], cfg["cache_dir"])


# Words that say where or what kind of fastener, not which part ("front" pulled in ABS sensors and brake hoses).
GENERIC = set("front rear left right upper lower inner outer side new old gasket gaskets seal bolt bolts nut nuts "
              "screw screws replace replacement repair removal install installation leak leaking cause causing "
              "most likely bad worn cylinder cylinders bank misfire".split())
STOPWORDS = set("the and for with from this that what which into onto off out".split())
SHOP_WORDS = {"exhaust manifold": "exhaust pipe", "cam belt": "timing belt", "valve cover": "rocker cover",
              "o2 sensor": "oxygen sensor", "lug nut": "wheel nut"}


def focus_specs(job, specs):
    """Keep spec rows about the part being repaired. The kb search ORs every word, so rows that only share
    'front' or 'gasket' with the job used to rank in. For a short job ('exhaust manifold gasket') its first
    part word must appear; for verdict text, rows that share more part words come first."""
    low = job.lower()
    low += " " + " ".join(v for k, v in SHOP_WORDS.items() if k in low)
    words = [w for w in dict.fromkeys(re.findall(r"[a-z0-9]{3,}", low)) if w not in GENERIC and w not in STOPWORDS]
    if not words:
        return specs
    has = lambda w, t: re.search(rf"\b{w}", t) is not None  # noqa: E731  (prefix match: gasket ~ gaskets)
    scored = [(sum(has(w, r.lower()) for w in words), r) for r in specs]
    if len(job.split()) <= 6:
        scored = [(n, r) for n, r in scored if has(words[0], r.lower())]
    return [r for n, r in sorted(scored, key=lambda x: -x[0]) if n > 0]


# --------------------------------------------------------------------------- session state
CASE_MAX_CHARS = 2500
CHART_READS_MAX = 6000   # chart readings in the case (PC models only, after the cut)
MAINT_PAGE = 6           # the Maintenance screen's place in the page stack (shown second in the nav bar)
# A test drive is added after the case (its own room). On the PC models there's room for more readings.
DRIVE_KEY = {"key": lambda: "drive_log_short"}   # the app points it at "drive_log" while the PC models answer


# Intake fields that hold opinions, not evidence: guesses, another shop's idea, earlier unconfirmed diagnoses.
# The blind first pass of FINAL DIAGNOSIS ("verdict_blind_first") leaves these out; pass 2 weighs them on the evidence.
# Decided by field, never by words in the text. (key, label for pass 2)
OPINION_FIELDS = (("suspect", "Suspect / known defects (the owner's or another shop's idea)"),
                  ("earlier_visits", "Earlier visits and their verdicts (deck's records; verdicts not confirmed by a test)"),
                  ("saved_debate", "Saved debate (competing explanations weighed, unresolved)"),
                  ("saved_wildcard", "Saved wildcard ideas (long shots the mechanic wanted kept)"))


class Session:
    """The open case, one key per field, in a small sqlite file. One connection for the app's lifetime
    (opening one per read meant ~15 opens per chat message); the lock lets worker threads share it."""
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS session_state (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS chat_log (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT,
                    message TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP);
                -- every case filed under its car's VIN, so the car's past visits come back when it does
                CREATE TABLE IF NOT EXISTS visit (id INTEGER PRIMARY KEY AUTOINCREMENT, vin TEXT, started TEXT,
                    updated TEXT, vehicle TEXT, case_json TEXT);
                CREATE INDEX IF NOT EXISTS visit_vin ON visit(vin);
                -- TEST DRIVE: one row per reading (kept even if the app closes mid-drive)
                CREATE TABLE IF NOT EXISTS drive_sample (drive_id TEXT, t REAL, codes TEXT, live TEXT);
                CREATE INDEX IF NOT EXISTS drive_sample_id ON drive_sample(drive_id);
            """)

    def add_drive_sample(self, drive_id, t, codes, live):
        with self._lock, self._db:
            self._db.execute("INSERT INTO drive_sample VALUES (?,?,?,?)", (drive_id, t, codes, live))

    def drive_ids(self):
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT drive_id FROM drive_sample")]

    def drive_samples(self, drive_id):
        with self._lock:
            return self._db.execute("SELECT t, codes, live FROM drive_sample WHERE drive_id=? ORDER BY t",
                                    (drive_id,)).fetchall()

    def save_visit(self, visit_id, vin, vehicle, case):
        """Update the open case's row (same id and VIN), else start a new one. Returns its id."""
        now = time.strftime("%Y-%m-%d %H:%M")
        with self._lock, self._db:
            if visit_id and self._db.execute("SELECT 1 FROM visit WHERE id=? AND vin=?", (visit_id, vin)).fetchone():
                self._db.execute("UPDATE visit SET updated=?, vehicle=?, case_json=? WHERE id=?",
                                 (now, vehicle, json.dumps(case), visit_id))
                return int(visit_id)
            return self._db.execute("INSERT INTO visit (vin, started, updated, vehicle, case_json) VALUES (?,?,?,?,?)",
                                    (vin, now, now, vehicle, json.dumps(case))).lastrowid

    def visits(self, vin, exclude=None):
        """[(id, started, vehicle, case dict)] for this VIN, newest first."""
        with self._lock:
            rows = self._db.execute("SELECT id, started, vehicle, case_json FROM visit WHERE vin=? AND id IS NOT ? "
                                    "ORDER BY id DESC", (vin, int(exclude) if exclude else None)).fetchall()
        out = []
        for i, started, vehicle, cj in rows:
            try:
                out.append((i, started, vehicle, json.loads(cj)))
            except ValueError:
                pass
        return out

    def set(self, k, v):
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO session_state VALUES (?,?)", (k, v))

    def get(self, k):
        with self._lock:
            r = self._db.execute("SELECT value FROM session_state WHERE key=?", (k,)).fetchone()
        return r[0] if r else ""

    def log(self, sender, msg):
        with self._lock, self._db:
            self._db.execute("INSERT INTO chat_log (sender, message) VALUES (?,?)", (sender, msg))

    def telemetry_zones(self, budget=900):
        """TELEMETRY (telemetry_ref): each reading of the last READ CAR against this car's healthy zone, out-of-zone
        first with where the manual points. '' when the make DB has no telem_* tables, the live line was typed
        or edited by hand (its reader is unknown), or nothing was read."""
        src, text = self.get("telemetry_source"), self.get("telemetry")
        kb = os.path.expanduser(CFG.get("kb_db") or "")
        if not (src and text and telemetry_ref.has_tables(kb)):
            return ""
        try:
            v = json.loads(self.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        car = {"make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "engine": v.get("engine"),
               "transmission": v.get("trans"), "vin": v.get("vin"), "db": kb}
        readings = telemetry_ref.normalize(telemetry_ref.from_live_text(text, src), src)
        return telemetry_ref.summary(telemetry_ref.judge(readings, car), budget=budget, car=car) if readings else ""

    def opinions(self):
        """The OPINION_FIELDS that are filled in, labelled; '' when there are none."""
        return "\n".join(f"{label}: {self.get(k)}" for k, label in OPINION_FIELDS if (self.get(k) or "").strip())

    def case_summary(self, exclude=()):
        """exclude: keys left out (the blind first pass leaves out OPINION_FIELDS)."""
        drive = self.get(DRIVE_KEY["key"]()) or self.get("drive_log")
        live_label = "Live data (last reading of the test drive below)" if drive else "Live data"
        keys = [("vehicle", "Vehicle"), ("dtcs", "DTCs"), ("telemetry", live_label), ("symptoms", "Symptoms"),
                ("ruled_out", "Verified good / ruled out"), ("suspect", "Suspect / known defects"),
                ("history", "History"), ("earlier_visits", "Earlier visits to this car (deck's records)"),
                ("intake_summary", "Intake note summary"),
                ("maint_selected", "Maintenance the customer approved for this visit")]
        if not self.get("intake_summary"):  # the raw note only when there's no summary of it (prompt room)
            keys.append(("intake_note", "Intake note (raw, as dictated)"))
        if self.get("mode06"):
            keys.insert(3, ("mode06", "Mode 06 on-board tests that failed"))
        if self.get("past_readings"):   # the car's record file: the same values at earlier visits
            keys.insert(3, ("past_readings", "Same readings at this car's earlier visits (deck's records)"))
        keys = [(k, label) for k, label in keys if k not in exclude]
        out = "\n".join(f"{label}: {(live_data.for_prompt(self.get(k)) if k == 'telemetry' else self.get(k)) or '(none)'}"
                        for k, label in keys)
        meaning = live_data.explain(self.get("telemetry"))
        if meaning:  # the models misread raw numbers (12 C coolant became "overheating"); Python says what they mean
            out += "\nWhat the live data means (worked out by the deck, generic ranges):\n" + \
                   "\n".join(f"- {m}" for m in meaning)
        zones = self.telemetry_zones()   # TELEMETRY: only when the make DB has telem_* tables
        if zones:
            out += "\nLive data against this car's manual (zones and status worked out by the deck):\n" + zones
        if self.get("saved_debate") and "saved_debate" not in exclude:
            out += "\nSaved debate (competing explanations weighed, unresolved):\n" + self.get("saved_debate")
        if self.get("saved_wildcard") and "saved_wildcard" not in exclude:
            out += ("\nSaved wildcard ideas (long shots the mechanic wanted kept; unproven, weigh against the "
                    "evidence):\n" + self.get("saved_wildcard"))
        if len(out) > CASE_MAX_CHARS:  # with the manual text and history it must still fit 4096 tokens
            out = out[:CASE_MAX_CHARS] + " [...]"
        if drive:  # after the cut, so a long case never drops the drive
            out += "\nTEST DRIVE LOG (a series of readings taken while driving, not one snapshot):\n" + drive
        notes = self.get("chat_notes")
        if notes:   # after the cut: what the mechanic said must never be the part that gets dropped
            room = 3500 if DRIVE_KEY["key"]() == "drive_log" else 900
            out += ("\nTHE MECHANIC'S OWN NOTES FROM CHAT (their words, newest last; anything they say they replaced, "
                    "checked or tested counts as verified unless the case shows otherwise):\n" + notes[-room:])
        if self.get("chart_reads") and DRIVE_KEY["key"]() == "drive_log":   # PC models only: too long for the deck's
            out += ("\nCHARM DIAGNOSTIC CHARTS for this case's codes (read from the manual's pictures by a vision "
                    "model; follow their steps, but numbers marked unverified need checking on the picture):\n"
                    + self.get("chart_reads")[:CHART_READS_MAX])
        return out


# --------------------------------------------------------------------------- model manager
class ModelManager:
    """Loads FAST/DEEP lazily; keeps both resident only when RAM allows."""

    def __init__(self, cfg):
        self.cfg = cfg
        backend = cfg["backend"]
        if backend == "auto":
            backend = "rkllm" if (platform.machine() == "aarch64" and os.path.isfile(cfg["rkllm_lib"])) else "ollama"
        self.backend = backend
        kb = cfg["keep_both_loaded"]
        self.keep_both = (mem_total_gb() >= 12) if kb == "auto" else bool(kb)
        self.models = {}
        self.home = bool(cfg.get("home"))
        self.on_pc = set()        # roles whose loaded model is the PC's
        self.pc_checked = {}      # role -> time the PC was last found unreachable for it
        self.pc_why = ""
        self._lock = threading.RLock()   # the launch preload and a model request can arrive together

    def label(self, role):
        if self.home and role in self.on_pc:
            return f"PC {self.cfg[f'home_{role}']}"
        if self.home and self.pc_why:
            return f"deck {self._local_label(role)} ({self.pc_why})"
        return self._local_label(role)

    def _local_label(self, role):
        if self.backend in ("claude", "openai"):
            return self.cfg[f"{self.backend}_{role}"] or self.cfg[f"{self.backend}_fast"]
        if self.backend == "rkllm":
            why = self.cfg.get("stand_in", {}).get(role)
            if why:
                return {"deep": f"fast model (4B {why})",
                        "think": f"1.7B thinking (Think model {why})"}.get(role, "fast model")
            return os.path.basename(self.cfg[f"{role}_model"]).replace(".rkllm", "")
        return self.cfg[f"ollama_{role}"]

    def _pc_model(self, role):
        """The home PC's model for this role, or None (reason in self.pc_why) when it can't be used."""
        have = ollama_models(self.cfg["home_url"])
        name = self.cfg[f"home_{role}"]
        if have is None:
            self.pc_why = "PC not reachable"
            return None
        if not any(h == name or h == name + ":latest" for h in have):
            self.pc_why = f"PC is up but hasn't downloaded {name}"
            return None
        self.pc_why = ""
        system = FAST_SYSTEM if role == "fast" else DEEP_SYSTEM
        temp = {"fast": 0.4, "deep": 0.1, "think": 0.6}[role]
        # Qwen's settings for thinking mode: top_p 0.95, top_k 20, and a presence penalty, which Qwen advises
        # against the endless "wait... but..." loops quantized thinking models fall into (11-01 saw them)
        opts = ({"top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": self.cfg.get("think_presence_penalty", 1.5)}
                if role == "think" else {})
        return OllamaModel(name, system_prompt=system, url=self.cfg["home_url"], temperature=temp,
                           enable_thinking=role == "think", num_ctx=self.cfg["home_context_len"], options=opts)

    def pc_failed(self, role):
        """A PC model stopped answering mid-job: drop it so the next request checks again (or uses the deck)."""
        if role in self.on_pc:
            self.on_pc.discard(role)
            self.models.pop(role, None)
            self.pc_why = "PC stopped answering"
            self.pc_checked[role] = time.time()

    def get(self, role):
        with self._lock:
            return self._get(role)

    def _get(self, role):
        if self.home and role not in self.on_pc and time.time() - self.pc_checked.get(role, 0) > 60:
            m = self._pc_model(role)
            if m is not None:
                old = self.models.pop(role, None)
                if old is not None:
                    old.release()
                self.models[role] = m
                self.on_pc.add(role)
                return m
            self.pc_checked[role] = time.time()   # try the PC again in a minute, the deck's model meanwhile
        if role in self.models:
            return self.models[role]
        # loading a model briefly needs about 1.5x its file in free RAM; short of that the deck swaps until it
        # freezes, so the loaded ones go first (they reload in seconds)
        try:
            need = os.path.getsize(self.cfg[f"{role}_model"]) / 2**30 * 1.5 + 1 if self.backend == "rkllm" else 0
        except OSError:
            need = 0
        domain = 0 if role == "fast" else 1
        if self.backend == "rkllm" and role != "fast" and self.cfg[f"{role}_model"] != self.cfg["fast_model"]:
            domain = self._trial(role)
            if domain is None:  # it can't run here: the fast model's file stands in, labelled with the reason
                return self.get(role)
        for other in list(self.models):
            # one 4B at a time (deep and think swap); swap all on small boards;
            # a model that only loads in domain 0 shares it with nothing
            if not self.keep_both or domain == 0 and role != "fast" or {role, other} == {"deep", "think"}:
                self.models.pop(other).release()
        # 11-02: keep the 1.7B and one 4B loaded together. Only when RAM is still short after the other 4B
        # went does the rest go too (short loads made the deck swap until it froze). Checked after the swap: before,
        # the RAM the other 4B was about to free counted as used, so every switch reloaded everything.
        if need and self.models and mem_available_gb() < need:
            print(f"models: {mem_available_gb():.1f} GB free, {need:.1f} GB needed to load {role}: "
                  f"unloading {', '.join(self.models)} first")
            for other in list(self.models):
                self.models.pop(other).release()
        system = FAST_SYSTEM if role == "fast" else DEEP_SYSTEM
        temp = {"fast": 0.4, "deep": 0.1, "think": 0.6}[role]  # 0.6 is Qwen's setting for thinking models
        think = role == "think"
        if self.backend == "rkllm":
            def load():
                return RKLLMModel(self.cfg[f"{role}_model"], self.cfg["rkllm_lib"], system_prompt=system,
                                  max_context_len=self.cfg["max_context_len"], temperature=temp,
                                  enable_thinking=think, domain_id=domain)
            try:
                m = load()
            except RuntimeError as e:
                if role == "fast":
                    raise
                m = None
                if self.models:  # maybe the NPU wouldn't hold both: try this one alone, once
                    for other in list(self.models):
                        self.models.pop(other).release()
                    try:
                        m = load()
                    except RuntimeError as e2:
                        e = e2
                if m is None:
                    if self.cfg[f"{role}_model"] == self.cfg["fast_model"]:
                        raise
                    # remember it and load the fast model's file in this role (its own prompt), labelled so
                    self.cfg.setdefault("stand_in", {})[role] = f"wouldn't load: {str(e).split('(')[0].strip()}"
                    self.cfg[f"{role}_model"] = self.cfg["fast_model"]
                    return self.get(role)
        elif self.backend == "claude":
            m = ClaudeModel(self.cfg[f"claude_{role}"], system_prompt=system,
                            effort=self.cfg["claude_effort"].get(role, "medium"), enable_thinking=think)
        elif self.backend == "openai":
            name = self.cfg[f"openai_{role}"] or self.cfg["openai_fast"]
            if not name:
                raise RuntimeError('backend "openai" needs a model name in openai_fast (config.json)')
            m = OpenAICompatModel(name, system_prompt=system, url=self.cfg["openai_url"],
                                  key_env=self.cfg["openai_key_env"], temperature=temp)
        else:
            m = OllamaModel(self.cfg[f"ollama_{role}"], system_prompt=system, temperature=temp,
                            enable_thinking=think, num_ctx=self.cfg["max_context_len"])
        self.models[role] = m
        return m

    def _trial(self, role):
        """Try a big model in a child process before loading it here (a native crash would close the app).
        Its own NPU domain first, then domain 0 on its own. -> domain to use, or None after setting up the stand-in.
        The verdict is remembered per model file, runtime and kernel, so it's tried once, not every launch."""
        path, lib = self.cfg[f"{role}_model"], self.cfg["rkllm_lib"]
        try:
            key = "|".join([path, str(os.path.getsize(path)), str(int(os.path.getmtime(path))),
                            str(int(os.path.getmtime(lib))), platform.release()])
        except OSError:
            return 1
        cache_path = f"{ROOT}/logs/model_probe.json"
        try:
            with open(cache_path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        if key not in cache:
            # the child needs the RAM and NPU memory this process is holding
            for other in list(self.models):
                self.models.pop(other).release()
            tried = []
            verdict = {"domain": None, "why": ""}
            for domain in (1, 0):
                ok, why, out = probe_rkllm(path, lib, domain)
                tried.append(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M} {os.path.basename(path)} domain {domain}: "
                             f"{'OK' if ok else why}\n{out[-3000:]}")
                if ok:
                    verdict = {"domain": domain, "why": ""}
                    break
                verdict["why"] = why
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(f"{ROOT}/logs/model_probe.log", "a") as f:
                f.write("\n".join(tried) + "\n")
            cache[key] = verdict
            with open(cache_path, "w") as f:
                json.dump(cache, f, indent=1)
        verdict = cache[key]
        if verdict["domain"] is None:
            self.cfg.setdefault("stand_in", {})[role] = f"won't run here: {verdict['why']}; see logs/model_probe.log"
            self.cfg[f"{role}_model"] = self.cfg["fast_model"]
        return verdict["domain"]

    def release_all(self):
        with self._lock:
            for m in self.models.values():
                m.release()
            self.models.clear()


class PreloadWorker(QThread):
    """Loads the deck's fast model at launch, so the first question doesn't wait for it (11-02)."""

    def __init__(self, mgr):
        super().__init__()
        self.mgr = mgr

    def run(self):
        try:
            self.mgr.get("fast")
        except Exception as e:      # the first question tries again and shows the error
            print(f"fast model preload: {e}")


class GenWorker(QThread):
    token = pyqtSignal(str)
    status = pyqtSignal(str)
    done = pyqtSignal(str)

    def __init__(self, mgr, role, prompt, keep_history=False, max_new_tokens=None, temperature=None):
        super().__init__()
        self.mgr, self.role, self.prompt = mgr, role, prompt
        self.keep_history, self.max_new_tokens, self.temperature = keep_history, max_new_tokens, temperature

    def run(self):
        try:
            if self.role not in self.mgr.models:
                self.status.emit(f"loading {self.mgr.label(self.role)}...")
            model = self.mgr.get(self.role)
            self.status.emit(f"{self.mgr.label(self.role)} generating...")
            text = model.generate(self.prompt, self.token.emit, keep_history=self.keep_history,
                                  max_new_tokens=self.max_new_tokens, temperature=self.temperature)
        except Exception as e:  # surface load/run errors in the UI instead of crashing
            text = f"\n[LLM error: {e}]"
            self.token.emit(text)
            self.mgr.pc_failed(self.role)
        self.done.emit(text)


class CaseContextWorker(QThread):
    """The manual lookup for a saved case, off the UI thread: on a big make it can take a minute on the deck."""
    result = pyqtSignal(str)

    def __init__(self, retriever, case, budget, **kw):
        super().__init__()
        self.retriever, self.case, self.budget, self.kw = retriever, case, budget, kw

    def run(self):
        try:
            self.result.emit(case_context(self.retriever, self.case, budget=self.budget, **self.kw))
        except Exception as e:      # a failed lookup only loses the manual text, never the answer
            print(f"manual lookup failed: {e}")
            self.result.emit("")


class WarmWorker(QThread):
    """One throwaway vector search, so the embedding model and the car's vectors are loaded before anyone asks."""

    def __init__(self, retriever):
        super().__init__()
        self.retriever = retriever

    def run(self):
        try:
            self.retriever.search_ids("engine", 1)
        except Exception as e:      # only a speed-up: the first real search loads them instead
            print(f"retriever warm-up: {e}")


class VinWorker(QThread):
    """NHTSA's online VIN decode off the UI thread (a slow hotspot must not freeze the screen)."""
    result = pyqtSignal(str, object)

    def __init__(self, vin):
        super().__init__()
        self.vin = vin

    def run(self):
        self.result.emit(self.vin, nhtsa_decode(self.vin, nhtsa_cache_path()))


class PicWorker(QThread):
    """CHARM page pictures from the home PC, off the UI thread (cache/charm/ keeps them for offline)."""
    done = pyqtSignal(list, str)     # cached files, problem ('' when all came)

    def __init__(self, names):
        super().__init__()
        self.names = names

    def run(self):
        have, why = charm_pics.fetch(CFG["pic_url"], CFG["cache_dir"], self.names, db=CFG.get("kb_db"))
        self.done.emit(have, why)


class ChartWorker(QThread):
    """Asks the PC to read chart pictures (35B vision, queued there behind other GPU jobs) and polls until
    each one is read. Emits each reading as it lands."""
    result = pyqtSignal(str, dict)   # picture name, the PC's answer
    note = pyqtSignal(str)
    POLL_S, GIVE_UP_S = 15, 45 * 60

    def __init__(self, names):
        super().__init__()
        self.names = names
        self.stop_flag = threading.Event()

    def run(self):
        left, t0, said = list(self.names), time.time(), ""
        while left and not self.stop_flag.is_set():
            for n in list(left):
                try:
                    r = charm_pics.reading(CFG["pic_url"], n, db=CFG.get("kb_db"))
                except urllib.error.HTTPError as e:
                    self.note.emit(f"The PC's picture server can't read charts (it answered {e.code}; was it started "
                                   "with --no-read?). The pictures above still show.")
                    return
                except (OSError, ValueError):
                    self.note.emit("Chart reading needs the PC, and it isn't reachable now. The pictures above are "
                                   "still the manual's own; tap CHARTS again once the PC is back.")
                    return
                if r.get("state") in ("done", "skipped", "error"):
                    left.remove(n)
                    self.result.emit(n, r)
                elif r.get("state") == "queued" and n == left[0]:
                    msg = (f"{len(left)} chart pictures waiting on the PC" +
                           (": the GPU is busy with a database build, so they wait for it" if r.get("gpu_busy")
                            else f" ({r.get('ahead', 0)} other pictures ahead)") + ". About a minute each.")
                    if msg != said:
                        self.note.emit(msg)
                        said = msg
            if time.time() - t0 > self.GIVE_UP_S:
                self.note.emit(f"Stopped waiting for {len(left)} chart readings; they keep going on the PC, so tap "
                               "CHARTS later and they'll show at once.")
                return
            self.stop_flag.wait(self.POLL_S if left else 0)


def nhtsa_cache_path():
    return os.path.join(CFG["cache_dir"], "vin_nhtsa.json")


class OBDWorker(QThread):
    """One read-only snapshot. OBDLink EX (main cable): generic OBD-II codes, VIN and every PID the car offers
    (obd_all.py), plus mode 06 results on READ CAR,
    then, unless the VIN says another make, Subaru SSM through the same EX. Bare KKL cable: SSM only.
    result fires only with real data; problems go to note, so an error never lands in the Live data box."""
    result = pyqtSignal(str, str)
    note = pyqtSignal(str)
    # how ("vin" | "ssm" | "hint"), value (VIN | SSM id | maker hint), engine (SSM only), ECU id (ROM/CAL id)
    ident = pyqtSignal(str, str, str, str)
    mode06 = pyqtSignal(str)   # failed on-board tests, for the case ('' when none)
    read_mode06 = True

    def __init__(self, adapter, port, make=""):
        super().__init__()
        self.adapter, self.port = adapter, port or None
        self.make, self.protocol = make, ""   # the Make picked in Intake; the bus the EX found

    def run(self):
        try:
            self._read()
        except Exception as e:  # a missing cable or driver must never close the app
            self.note.emit(f"Read failed: {e}")

    def _ssm_note(self, note):
        self.note.emit(note)
        m = re.match(r"SSM (\S+) \(([^)]*)\) ROM (\S+)", note)
        if m:  # only Subaru ECUs answer the SSM init, so this alone says the make
            self.ident.emit("ssm", m.group(1), m.group(2), f"ROM {m.group(3)}")
            v = re.search(r"\bVIN ([A-HJ-NPR-Z0-9]{17})\b", note)
            if v:  # newer Subaru ECUs keep the VIN; read with 0xA8 like everything else
                self.ident.emit("vin", v.group(1), m.group(2), f"ROM {m.group(3)}")
        return bool(m)

    def _read(self):
        if self.adapter == "kkl":
            from ssm_reader import read_ssm
            codes, live, note = read_ssm(self.port, CFG["session_db"], CFG["cache_dir"])
            self._ssm_note(note)
            self._seen_ssm(note, "KKL", "ecu_init_reply.hex")
            if live or codes:
                CFG["_live"] = ("ssm", live)
                self.result.emit(codes, live)
            return
        codes, live, vin_make = self._read_obd()
        CFG["_live"] = ("obd01", live)
        if not obd_all.may_speak_ssm(vin_make or self.make, self.protocol):
            if not (vin_make or self.make):
                self.note.emit("Type the VIN (windshield or door jamb), or pick the Make.")
            if codes or live:
                self.result.emit(codes, live)
            return
        # No VIN, or a Subaru VIN: ask for SSM through the same EX (more codes and live values on a Subaru).
        if CFG.get("_ex_ssm_failed"):  # it already got no SSM answer this run: don't spend the time again
            self.note.emit("Skipping Subaru SSM through the EX (no answer earlier). For SSM codes and live data "
                           "on a Subaru, pick the KKL cable.")
            if codes or live:
                self.result.emit(codes, live)
            return
        from ssm_reader import read_ssm_ex
        self.note.emit("Asking for Subaru SSM through the EX...")
        s_codes, s_live, note, variant = read_ssm_ex(self.port, CFG["session_db"], CFG["cache_dir"],
                                                     CFG.get("ex_ssm_variant", ""))
        ok = self._ssm_note(note)
        if not ok and (codes or live):  # the EX itself works on this car, only SSM through it doesn't
            CFG["_ex_ssm_failed"] = True
        if not ok:
            if not vin_make:
                self.note.emit("Type the VIN (windshield or door jamb), or pick the Make.")
        elif variant != CFG.get("ex_ssm_variant"):
            CFG["ex_ssm_variant"] = variant
            save_config_key("ex_ssm_variant", variant)
        codes = ", ".join(dict.fromkeys(c for c in (codes + ", " + s_codes).split(", ") if c))
        if s_live and s_live != "(no live data)":
            ssm_labels = {x.strip().split(" ")[0] for x in s_live.split("|")}
            extra = [x.strip() for x in live.split("|") if x.strip() and x.strip().split(" ")[0] not in ssm_labels]
            live = " | ".join([s_live] + extra)   # SSM first; generic values SSM doesn't have stay in the line
            CFG["_live"] = ("ssm", live)
            self._seen_ssm(note, "EX", "ecu_init_reply_ex.hex")
        if codes or live:
            self.result.emit(codes, live)

    def _read_obd(self):
        """Generic OBD-II over the EX. Returns (codes, live, make from the VIN or '')."""
        try:
            import obd
        except ImportError:
            self.note.emit("python-obd not installed (pip install obd); trying SSM only")
            return "", "", ""
        from cables import ex_port
        conn, why = obd_all.connect(ex_port(self.port or ""))
        if conn is None:
            self.note.emit(f"No generic OBD-II answer ({why}). Ignition on? Adapter plugged in?")
            return "", "", ""
        self.protocol = conn.protocol_name() or ""
        vin_make = self._identify(conn)
        dtcs = conn.query(obd.commands.GET_DTC).value or []
        codes = ", ".join(c for c, _ in dtcs)
        got = obd_all.read_all(conn)
        self._seen_obd(conn, got)
        self.note.emit(f"EX: car offers {len(got['supported'])} PIDs: {' '.join(got['supported'])}"
                       + (f"; no answer from {' '.join(got['silent'])}" if got["silent"] else ""))
        if self.read_mode06:
            self._mode06(conn)
        conn.close()
        return codes, got["line"], vin_make

    def _mode06(self, conn):
        """On-board test results: all of them to cache/mode06_last.txt, failures to the case. Never stops a read."""
        try:
            lines, failed, kind = obd_all.mode06(conn)
            with open(os.path.join(CFG["cache_dir"], "mode06_last.txt"), "w") as f:
                f.write("\n".join(lines) + "\n")
            if kind == "decoded":
                self.note.emit(f"Mode 06: {len(lines)} tests, {len(failed)} failed (cache/mode06_last.txt)"
                               + "".join(f"\n  {x}" for x in failed))
            elif lines:
                self.note.emit(f"Mode 06: {len(lines)} raw frames saved to cache/mode06_last.txt (on pre-CAN cars "
                               "their meaning is the maker's own, so the deck doesn't judge them)")
            self.mode06.emit("; ".join(failed))
        except Exception as e:
            print(f"mode 06: {e}")

    # TELEMETRY: what this car answered, kept per device (cache/telemetry_seen.db) for telemetry_ref.capabilities().
    # Reads only what the connection already learned; never sends anything extra. A failure never stops a read.
    def _seen_obd(self, conn, got):
        try:
            car = getattr(self, "seen_car", None)
            if not car:
                return
            telemetry_ref.record_seen(car, "EX", conn.protocol_name() or "", "obd01", got["supported"],
                                      sorted(set(got["asked"]) - set(got["supported"])))
        except Exception as e:
            print(f"telemetry seen (OBD): {e}")

    def _seen_ssm(self, note, cable, hex_name):
        try:
            m = re.search(r"\bROM (\S+)", note or "")
            kb = os.path.expanduser(CFG.get("kb_db") or "")
            path = os.path.join(CFG["cache_dir"], hex_name)
            if not (m and os.path.isfile(path) and telemetry_ref.has_tables(kb)):
                return
            init = bytes.fromhex(open(path).read().replace(" ", "").strip())
            yes, no = telemetry_ref.ssm_support(init, kb)
            telemetry_ref.record_seen({"ecu_id": m.group(1)}, cable, "SSM2", "ssm", yes, no)
        except Exception as e:
            print(f"telemetry seen (SSM): {e}")

    def _identify(self, conn):
        """Ask every car for its VIN (mode 09 PID 02: required from 2005, many older cars answer too).
        No VIN: report the protocol and OBD standard as a maker/market hint. Read-only requests only."""
        import obd

        def ask(name, force=False):
            cmd = getattr(obd.commands, name, None)
            if cmd is None or not (force or conn.supports(cmd)):
                return ""
            r = conn.query(cmd, force=force)  # force: some libraries don't read the 0900 bitmap; VIN is a read
            if r.is_null():
                return ""
            v = r.value
            return (v.decode(errors="ignore") if isinstance(v, (bytes, bytearray)) else str(v)).strip(" \x00")
        cal = ask("CALIBRATION_ID")
        key = f"CAL {cal}" if cal else ""
        vin = ask("VIN", force=True)
        self.seen_car = {"vin": vin} if vin else {"ecu_id": key} if key else None
        if vin:
            self.ident.emit("vin", vin, "", key)
            return decode_vin(vin, CFG.get("vin_db"))["make"]
        proto = conn.protocol_name() or ""
        hint = {"J1850 PWM": "J1850 PWM: usually Ford or Mazda", "J1850 VPW": "J1850 VPW: usually GM or Chrysler"}
        text = next((h for k, h in hint.items() if k in proto), proto)
        std = ask("OBD_COMPLIANCE")
        self.ident.emit("hint", "; ".join(x for x in (text, std) if x), "", key)
        return ""


class DriveWorker(OBDWorker):
    """TEST DRIVE: the same read-only READ CAR snapshot, repeated every `every` seconds until stopped.
    Holds the cable for the whole drive, so READ CAR, TEST MODE and other programs wait."""
    sample = pyqtSignal(float, str, str)   # time.time(), codes, live
    read_mode06 = False                    # slow and doesn't change while driving; READ CAR has it

    def __init__(self, adapter, port, every, make=""):
        super().__init__(adapter, port, make)
        self.every = every
        self.stop_flag = threading.Event()
        self.result.connect(lambda codes, live: self.sample.emit(time.time(), codes, live))

    def run(self):
        while not self.stop_flag.is_set():
            t0 = time.time()
            try:
                self._read()
            except Exception as e:  # one bad read (a bump, a loose plug) mustn't end the drive log
                self.note.emit(f"Read failed: {e}")
            self.stop_flag.wait(max(1.0, self.every - (time.time() - t0)))


class TestWorker(QThread):
    """TEST MODE: holds the cable while the switch is on and runs only what a confirmed button asked for.
    Subaru actuator tests and clear memory go through ssm/ssm_tests.py (FreeSSM's checks: test-mode
    connector joined, engine off); clear codes for any car is OBD-II mode 04 through python-obd."""
    msg = pyqtSignal(str, str)      # text, colour
    tests = pyqtSignal(list)        # actuator tests this ECU advertises ([] = none, or not connected)
    running = pyqtSignal(str)       # test running now, '' when none

    def __init__(self, port):
        super().__init__()
        # 10-19: Subaru tests and clears always go over the KKL cable (the EX got NO DATA over SSM);
        # the EX is only for mode 04 on other cars. cables.py tells the two apart when both are plugged in.
        self.adapter, self.port = "kkl", port or None
        self.q = queue.Queue()
        self.t = None
        self._last = None
        self.quitting = False

    def do(self, op, arg=None):
        if op == "quit":
            self.quitting = True  # a CONNECT in progress gives up before trying the next cable
        self.q.put((op, arg))

    def emergency_stop(self):
        """STOP ALL from the GUI thread: doesn't wait in the queue behind a slow job. Writes 0x00 to every
        actuator byte (the only writes it can make) on its own thread, so the screen never freezes."""
        t = self.t
        if t is None:
            self.msg.emit("[!] Not connected to an ECU, so there's nothing the deck can switch off. "
                          "Key OFF to be sure.", T.AMBER)
            return

        def go():
            try:
                ok = t.stop()
            except Exception as e:
                ok = False
                self.msg.emit(f"[!] Stop failed ({e}).", T.RED)
            self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                          "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)
        threading.Thread(target=go, daemon=True).start()

    def run(self):
        from ssm_tests import Refused
        while True:
            try:
                op, arg = self.q.get(timeout=0.25)
            except queue.Empty:
                self._report()
                continue
            if op == "quit":
                break
            try:
                getattr(self, "_" + op)(arg)
            except Refused as e:
                self.msg.emit(f"[!] {e}", T.AMBER)
            except Exception as e:
                self.msg.emit(f"[!] {op.replace('_', ' ')} failed: {e}", T.RED)
            self._report()
        self._release()

    def _report(self):
        cur = self.t.running if self.t is not None else None
        if cur != self._last:
            if cur is None and self._last and self.t is not None and self.t.ended:
                if self.t.off_confirmed:
                    self.msg.emit(f"[✓] {self._last} {self.t.ended}; all actuators switched off.", T.OK)
                else:
                    self.msg.emit(f"[!] {self._last} {self.t.ended}, but the ECU didn't confirm the switch-off. "
                                  "Key OFF to be sure.", T.RED)
            self._last = cur
            self.running.emit(cur or "")

    def _release(self):
        if self.t is not None:
            try:
                self.t.close()  # stops a running test and writes 0x00 to every actuator byte first
            except Exception as e:
                self.msg.emit(f"[!] Couldn't confirm the actuators stopped ({e}). Key OFF to be sure.", T.RED)
            self.t = None
            self._last = None
            self.running.emit("")
            self.tests.emit([])

    def _connect(self, _):
        from ssm_tests import open_subaru
        self._release()
        from ssm_tests import NoLink
        self.msg.emit("[*] Connecting to the Subaru engine ECU...", T.MUTED)
        # Each job uses whichever cable answers: the KKL cable first for SSM, then the EX.
        try:
            self.t, variant = open_subaru("kkl", self.port, "", CFG["cache_dir"])
        except NoLink as e:
            if self.quitting:
                raise
            self.msg.emit(f"[*] {e} Trying the OBDLink EX...", T.MUTED)
            try:
                self.t, variant = open_subaru("obdlink", None, CFG.get("ex_ssm_variant", ""), CFG["cache_dir"])
            except NoLink as e2:
                raise NoLink(f"Neither cable got an SSM answer. KKL: {e} EX: {e2}")
        if variant and variant != CFG.get("ex_ssm_variant"):
            CFG["ex_ssm_variant"] = variant
            save_config_key("ex_ssm_variant", variant)
        caps = self.t.caps
        self.msg.emit(f"[✓] ECU {caps.sys_id.hex().upper()} ({caps.engine}), ROM {caps.rom_id.hex().upper()}.", T.OK)
        why = self.t.why_no_tests()
        if why:
            self.msg.emit(f"[!] {why} Clear memory still works.", T.AMBER)
            self.tests.emit([])
            return
        tm, rpm = self.t.test_mode_on(), self.t.engine_rpm()
        self.msg.emit(f"[car] Test-mode connector: {'joined' if tm else 'NOT joined' if tm is not None else 'unknown'}"
                      f" | engine: {'off' if rpm is not None and rpm <= 100 else 'RUNNING' if rpm else 'unknown'}",
                      T.OK if tm and rpm is not None and rpm <= 100 else T.AMBER)
        self.tests.emit(self.t.titles())

    def _need_ecu(self):
        from ssm_tests import Refused
        if self.t is None:
            raise Refused("Connect to the Subaru ECU first.")
        return self.t

    def _start(self, index):
        title = self._need_ecu().start(index)
        self.msg.emit(f"[TEST] {title} is running. STOP ALL ends it; it stops by itself after "
                      f"{__import__('ssm_tests').MAX_TEST_S} s.", T.AMBER)

    def _stop(self, _):
        if self.t is None:
            return
        ok = self.t.stop()
        self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                      "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)

    def _stop_all(self, _):
        ok = self._need_ecu().stop_all()
        self.msg.emit("[✓] All actuators off (ECU confirmed)." if ok else
                      "[!] The ECU didn't confirm the stop. Key OFF to be sure.", T.OK if ok else T.RED)

    def _clear_memory(self, _):
        ok = self._need_ecu().clear_memory()
        self.msg.emit("[✓] ECU memory cleared (ECU confirmed). Key OFF for 10 s, then READ CAR to check." if ok else
                      "[!] The ECU didn't confirm the clear. Read the codes again to see.", T.OK if ok else T.RED)

    def _clear_codes(self, _):
        """OBD-II mode 04 through the OBDLink EX; if that cable doesn't answer, the Subaru SSM memory clear
        through the KKL cable. A safety refusal (engine running) never falls through to the other cable."""
        from ssm_tests import NoLink
        try:
            self._mode04()
        except NoLink as e:
            self.msg.emit(f"[*] {e} Trying the SSM memory clear through the KKL cable...", T.MUTED)
            if self.t is None:
                self._connect(None)
            self._clear_memory(None)

    def _mode04(self):
        from ssm_tests import NoLink, Refused
        from cables import ex_port
        self._release()  # python-obd needs the port to itself
        try:
            import obd
        except ImportError:
            raise NoLink("python-obd isn't installed (pip install obd).")
        conn, why = obd_all.connect(ex_port(CFG["obd_port"] if CFG.get("obd_adapter") == "obdlink" else ""))
        if conn is None:
            raise NoLink(f"No OBD-II answer through the OBDLink EX ({why}).")
        try:
            r = conn.query(obd.commands.RPM)
            rpm = None if r.is_null() else float(getattr(r.value, "magnitude", r.value))
            if rpm is None:
                raise Refused("Couldn't read engine speed, so nothing was sent.")
            if rpm > 100:
                raise Refused(f"Engine is running ({rpm:.0f} rpm). Key ON, engine OFF.")
            conn.query(obd.commands.CLEAR_DTC)
            left = conn.query(obd.commands.GET_DTC).value or []
            if left:
                self.msg.emit(f"[!] Clear sent, but codes are still there: {', '.join(c for c, _ in left)}. "
                              "A fault that's still present comes straight back.", T.AMBER)
            else:
                self.msg.emit("[✓] Codes cleared (mode 04); the car now reports none. Emissions readiness "
                              "monitors are reset too and need a drive cycle.", T.OK)
        finally:
            conn.close()


class ErrorBus(QObject):
    """Unhandled errors (any thread) end up on screen instead of PyQt6 closing the whole app."""
    error = pyqtSignal(str)


_CONFIG_LOCK = threading.Lock()


def save_config_key(key, value):
    """Remember a choice (mic, cable) in config.json without touching the rest of it."""
    with _CONFIG_LOCK:  # the GUI and the cable workers can both save a choice
        data = {}
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        except OSError:
            pass
        except ValueError:
            return  # never replace a file we couldn't read with a one-key file
        data[key] = value
        tmp = CONFIG_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, CONFIG_PATH)  # a power cut leaves the old file or the new one, never half of one
        except OSError:
            pass


# --------------------------------------------------------------------------- intake bubbles
MODELS = ["Forester", "Impreza", "Legacy", "Outback", "WRX", "WRX STI", "Baja", "Crosstrek", "Tribeca", "SVX",
          "Ascent", "BRZ", "XV Crosstrek"]
TRANS = ["", "Manual", "Automatic"]
REPAIRS = sorted(["exhaust manifold gasket", "spark plugs", "ignition coil", "spark plug wires", "timing belt",
                  "head gasket", "valve cover gasket", "front O2 sensor", "rear O2 sensor", "PCV valve", "thermostat",
                  "water pump", "intake manifold gasket", "fuel injector", "knock sensor", "crankshaft position sensor",
                  "camshaft position sensor"], key=str.lower)  # alphabetical: list order must not favor a repair


VERDICT_KEYWORDS = [("exhaust manifold", "exhaust manifold gasket"), ("coil", "ignition coil"), ("plug wire", "spark plug wires"),
                    ("spark plug", "spark plugs"), ("timing belt", "timing belt"), ("head gasket", "head gasket"),
                    ("injector", "fuel injector"), ("thermostat", "thermostat"), ("o2", "front O2 sensor"),
                    ("oxygen sensor", "front O2 sensor"), ("knock", "knock sensor"), ("pcv", "PCV valve")]


def repair_from_verdict(verdict):
    """The repair named in the verdict's root cause (section 1 only, so ruled-out parts don't count)."""
    # section 2 starts at a line like "2.", "**2.**", "## 2)" or "2. WHAT RULES OUT"
    root = re.split(r"\n[\s#*_>-]*2\s*[.):]", verdict, maxsplit=1)[0].lower()

    def named(word):
        """Where the root cause names word as the problem: not 'not the O2 sensor' / 'O2 sensor reads normal'."""
        for m in re.finditer(re.escape(word), root):
            before = root[max(0, m.start() - 25):m.start()]
            after = root[m.end():m.end() + 30]
            if re.search(r"\b(not|no|isn't|rule[sd]? out|ruled out)\b[^.]*$", before) or \
                    re.search(r"^[^.]*\b(normal|good|fine|ok|okay|within spec|ruled out)\b", after):
                continue
            return m.start()
        return -1

    # the repair the verdict names first wins, not the one listed first here
    hits = [(i, r) for r in REPAIRS if (i := named(r.lower())) >= 0]
    hits = hits or [(i, r) for k, r in VERDICT_KEYWORDS if (i := named(k)) >= 0]
    return min(hits)[1] if hits else None


class Bubbles(QWidget):
    """Tap-to-toggle bubbles plus a typing box for anything not listed. text() joins them with '; '.
    sections: [(heading or None, [labels])]. single=True makes it pick-one."""
    changed = pyqtSignal()

    def __init__(self, sections, cols=6, other_hint="other (type)", single=False, tips=None):
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        self.buttons = []
        self.group = QButtonGroup(self) if single else None
        if self.group:
            self.group.setExclusive(False)  # exclusive by hand, so a second tap can clear it
        row = 0
        for head, labels in sections:
            col0 = 0
            if head:
                lbl = QLabel(head)
                lbl.setStyleSheet(f"color: {T.MUTED};")
                grid.addWidget(lbl, row, 0)
                col0 = 1
            for i, t in enumerate(labels):
                b = QPushButton(t)
                b.setObjectName("bubble")
                b.setCheckable(True)
                if tips and t in tips:
                    b.setToolTip(tips[t])
                b.clicked.connect(lambda on, b=b: self._clicked(b, on))
                self.buttons.append(b)
                grid.addWidget(b, row + i // cols, col0 + i % cols)
            row += (len(labels) + cols - 1) // cols
        self.other = QLineEdit()
        self.other.setPlaceholderText(other_hint)
        self.other.textChanged.connect(self._typed)
        grid.addWidget(self.other, row, 0, 1, cols + (1 if any(h for h, _ in sections) else 0))

    def _typed(self, t):
        if self.group is not None and t.strip():
            for o in self.buttons:
                o.setChecked(False)
        self.changed.emit()

    def _clicked(self, b, on):
        if self.group is not None and on:
            for o in self.buttons:
                if o is not b:
                    o.setChecked(False)
            self.other.clear()
        self.changed.emit()

    def text(self):
        parts = [b.text() for b in self.buttons if b.isChecked()]
        o = self.other.text().strip()
        return "; ".join(parts + ([o] if o else []))

    def set_text(self, s):
        by_label = {b.text().lower(): b for b in self.buttons}
        rest = []
        for b in self.buttons:
            b.setChecked(False)
        for item in re.split(r"[;,]\s*", s or ""):
            item = item.strip()
            if not item:
                continue
            if item.lower() in by_label and not (self.group is not None and any(b.isChecked() for b in self.buttons)):
                by_label[item.lower()].setChecked(True)
            else:
                rest.append(item)
        self.other.setText("; ".join(rest))


class PopupCombo(QComboBox):
    """A combo box that refreshes its list each time it opens."""
    def __init__(self, on_open):
        super().__init__()
        self._on_open = on_open

    def showPopup(self):
        self._on_open()
        super().showPopup()


class Field(QLineEdit):
    """A typed line with the Bubbles text()/set_text() interface (codes and checks are typed, not tapped:
    a fixed chip list ends up being one car's history)."""
    def set_text(self, s):
        self.setText(s or "")


def vehicle_table(retriever):
    """{(model, year): {engine codes}} from the kb's vehicle table (every car the manuals cover)."""
    out = {}
    db = getattr(retriever, "db", None)
    if db is not None:
        try:
            for model, year, engine in db.execute("SELECT model, year, engine FROM vehicle"):
                out.setdefault((model, year), set()).add(engine)
        except Exception as e:
            log_caught("vehicle table", e)
    return out


# --------------------------------------------------------------------------- GUI
STYLE = T.qss()
IDLE, BUSY, ERR = T.fg(T.MUTED), T.fg(T.AMBER, bold=True), T.fg(T.RED, bold=True)


class CyberdeckApp(QMainWindow):
    def __init__(self, bus=None):
        super().__init__()
        self.session = Session(CFG["session_db"])
        try:  # manual lookups start on the car picked last time, if any
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        if v.get("model"):
            CFG["kb_model"], CFG["kb_year"] = v["model"], int(v["year"]) if str(v.get("year", "")).isdigit() else None
        if "make" not in v and v.get("model"):  # cases saved before makes existed were all Subaru
            v["make"] = "Subaru"
        self.kb_msg = use_make(v.get("make", ""))
        try:  # a kb mid-copy or a missing kb_retriever.py must not stop the window opening
            self.retriever = make_retriever(CFG)
            self._warm_retriever()
        except Exception as e:
            self.retriever = NullRetriever()
            self.kb_msg = f"manual database failed to open: {e}"
        self.mgr = ModelManager(CFG)
        self.preload = None
        if self.mgr.backend == "rkllm" and not self.mgr.home:
            self.preload = PreloadWorker(self.mgr)
            self.preload.start()
        DRIVE_KEY["key"] = lambda: "drive_log" if self.mgr.home and not self.mgr.pc_why else "drive_log_short"
        self.worker = None
        self.obd_worker = None
        self.test_worker = None  # TEST MODE; always off at start
        self.chat_started = False
        self.chat_hist = []
        self.last_case = self.session.case_summary()
        self.last_debate = ""  # held in memory; only SAVE DEBATE (/save) writes it to the session DB
        self.dictate_target = None
        self.dictate_widget = None   # the text box that had the cursor when dictation started
        self.last_edit = None        # the last text box the cursor was in, on any page
        self.voice = None

        self.setWindowTitle("CYBERDECK HOME // PC MODELS" if CFG.get("home") else "CYBERDECK // DIAGNOSTIC OS")
        avail = QApplication.primaryScreen().availableGeometry() if QApplication.primaryScreen() else None
        self.resize(min(1180, avail.width()) if avail else 1180, min(720, avail.height()) if avail else 720)
        self.setStyleSheet(STYLE)

        root = FitRoot()   # scrolls only when the screen is below the minimum, never at 150% scaling
        lay = QVBoxLayout(root)
        top = QHBoxLayout()
        self.lbl_title = title = QLabel(self._title_text())
        title.setStyleSheet(f"color: {T.FOREST_TEXT}; font-weight: bold;")
        self.lbl_llm = QLabel("")
        self._llm_idle()
        for lbl in (title, self.lbl_llm):
            lbl.setWordWrap(True)
            lbl.setMinimumWidth(120)
        self.lbl_llm.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.lbl_llm.setObjectName("chip")   # the Home/RKLLM mode label (theme.qss)
        top.addWidget(title, 1)
        top.addWidget(self.lbl_llm, 1)
        lay.addLayout(top)

        nav = ReflowRow()   # wraps onto two rows on a portrait screen
        # self.nav is in page (stack) order; Maintenance was added last but sits second on screen
        self.nav = [QPushButton(t) for t in ("1. INTAKE && SCAN", "3. BRAINSTORM / CHAT", "4. FINAL DIAGNOSIS",
                                             "5. REPAIR GUIDE (SOP)", "6. PARTS", "7. TESTS", "2. MAINTENANCE")]
        for i, b in enumerate(self.nav):
            b.setCheckable(True)
            b.clicked.connect(lambda _, i=i: self.switch(i))
        for i in (0, MAINT_PAGE, 1, 2, 3, 4, 5):
            nav.add(self.nav[i])
        lay.addWidget(nav)

        self.stack = QStackedWidget()
        self._intake_view()
        self._chat_view()
        self._verdict_view()
        self._sop_view()
        self._parts_view()
        self._tests_view()
        self._maint_view()
        lay.addWidget(self.stack, 1)

        # bottom bar: voice + stop, on every screen
        bar = QHBoxLayout()
        self.b_mic = QPushButton("🎤 MIC")
        self.b_mic.setToolTip("Or press F2, or say 'hey deck'. Talk, then stay quiet for 2 s. The words go "
                              "into the box the cursor is in (Chat and Parts then run it); with no box picked: "
                              "Intake notes, Chat question, Repair job, Parts search.")
        # pressing MIC must not take the cursor out of the box the words are meant for
        self.b_mic.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        QApplication.instance().focusChanged.connect(self._track_focus)
        QShortcut(QKeySequence("F2"), self, activated=self.act_mic)
        self.b_mic.clicked.connect(self.act_mic)
        self.cmb_mic = PopupCombo(lambda: self._fill_mics(refresh=True))  # earbuds that connect later show up
        self.cmb_mic.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cmb_mic.setMinimumContentsLength(12)  # long PortAudio names mustn't widen the window
        self._fill_mics()
        self.cmb_mic.currentIndexChanged.connect(self.act_mic_changed)
        b_test = QPushButton("🔊 TEST")
        b_test.setToolTip("Say a test phrase through the speaker/headphones")
        b_test.clicked.connect(self.act_speaker_test)
        self.lbl_voice = QLabel("")
        self.lbl_voice.setStyleSheet(IDLE)
        self.b_stop = QPushButton("■ STOP")
        self.b_stop.setToolTip("Stop the answer being written")
        self.b_stop.clicked.connect(self.act_stop)
        for wdg in (self.b_mic, self.cmb_mic, b_test):
            bar.addWidget(wdg)
        self.lbl_voice.setWordWrap(True)
        self.lbl_voice.setMinimumWidth(100)
        bar.addWidget(self.lbl_voice, 1)
        bar.addWidget(self.b_stop)
        lay.addLayout(bar)

        # The whole window scrolls when the screen is smaller than the layout's minimum (desktop scaled to
        # 160% leaves ~1200x675 logical px), so nothing is ever placed off screen.
        root.setObjectName("page")
        frame = QScrollArea()
        frame.setWidgetResizable(True)
        frame.setFrameShape(QScrollArea.Shape.NoFrame)
        frame.setWidget(root)
        self.setCentralWidget(frame)
        self.switch(0)
        if bus is not None:
            bus.error.connect(self.on_error)
        self._start_voice()

    # ------------------------------------------------------------------ plumbing
    def on_error(self, msg):
        self.lbl_llm.setText(f"error: {msg[:120]}")
        self.lbl_llm.setStyleSheet(ERR)
        out = [self.txt_intake, self.txt_chat, self.txt_verdict, self.txt_sop, self.txt_parts,
               self.txt_tests, self.txt_maint][self.stack.currentIndex()]
        self._say(out, f"[error] {msg}", T.RED)

    def _llm_idle(self):
        self.lbl_llm.setText(f"[{self.mgr.backend}] fast: {self.mgr.label('fast')} | deep: {self.mgr.label('deep')} (idle)")
        self.lbl_llm.setStyleSheet(IDLE)

    def _say(self, out, text, color=None, who=None, raw=False):
        """Append a line; who= adds a coloured 'NAME >' label like the old app. raw=True: text is already HTML."""
        body = text if raw else html.escape(text).replace("\n", "<br>")
        if who:
            body = f"<span style='color:{color or T.FOREST_TEXT};font-weight:bold'>{who} &gt;</span> {body}"
        elif color:
            body = f"<span style='color:{color}'>{body}</span>"
        out.append(body)
        out.moveCursor(QTextCursor.MoveOperation.End)

    def _fig_html(self, out, path):
        """<img> for one figure file, '' when it's missing or unreadable. A CHARM picture name means its
        cached copy (see _charm_figs for fetching it)."""
        if charm_pics.is_charm(path):
            path = charm_pics.cache_path(CFG["cache_dir"], path)
        if not os.path.isfile(path):
            return ""
        url = QUrl.fromLocalFile(path)
        doc = out.document()
        if doc.resource(QTextDocument.ResourceType.ImageResource, url) is None:
            pm = load_figure(path)
            if pm.isNull():
                return ""
            doc.addResource(QTextDocument.ResourceType.ImageResource, url, pm)
        return f'<br><img src="{url.toString()}"><br>'

    def _charm_figs(self, out, label, names):
        """A CHARM page's pictures: the cached ones now, the rest fetched from the PC and drawn when they
        arrive (with the page's tag, since other text may have been added below by then)."""
        names = names[:int(CFG.get("charm_pics_max") or 8)]
        now = "".join(self._fig_html(out, n) for n in names)
        missing = [n for n in names if not os.path.isfile(charm_pics.cache_path(CFG["cache_dir"], n))]
        if missing:
            w = PicWorker(missing)
            self._pic_workers = [x for x in getattr(self, "_pic_workers", []) if x.isRunning()] + [w]

            def arrived(have, why, out=out, label=label, n=len(missing)):
                gray = f"<span style='color:{T.MUTED}'>{{}}</span>"
                imgs = "".join(self._fig_html(out, p) for p in have)
                if imgs:
                    out.append(gray.format("Pictures for " + html.escape(label)) + imgs)
                if len(have) < n:
                    k = n - len(have)
                    out.append(gray.format(f"{k} CHARM picture{'s' * (k > 1)} for {html.escape(label)} not shown"
                                           f" ({html.escape(why or 'unreadable')})" + (
                                               ". They show once the PC is reachable." if why == "PC not reachable"
                                               else ".")))
                out.moveCursor(QTextCursor.MoveOperation.End)
            w.done.connect(arrived)
            w.start()
        return now

    def _say_hits(self, out, hits, color=None, sep="<br><br>"):
        """Manual sections with their figures drawn where the text says [fig]. CHARM pages have no [fig]
        marks: their pictures follow the text."""
        blocks, later = [], []
        for text, figs in hits:
            charm = [f for f in figs if charm_pics.is_charm(f)]
            figs = [f for f in figs if not charm_pics.is_charm(f)]
            parts = text.split("[fig]")
            body = html.escape(parts[0])
            for i, p in enumerate(parts[1:]):
                body += (self._fig_html(out, figs[i]) if i < len(figs) else "") + html.escape(p)
            body = body.replace("\n", "<br>")
            if charm:
                later.append((text.split("\n", 1)[0], charm))
            blocks.append(body)
        body = sep.join(blocks)
        if color:
            body = f"<span style='color:{color}'>{body}</span>"
        out.append(body)
        for label, charm in later:
            imgs = self._charm_figs(out, label, charm)
            if imgs:
                out.append(imgs)
        out.moveCursor(QTextCursor.MoveOperation.End)

    def _say_figs(self, out, figs, limit=6, label="this page"):
        """A section's figures on their own (the SOP sources)."""
        charm = [f for f in figs if charm_pics.is_charm(f)]
        imgs = [h for h in (self._fig_html(out, f) for f in figs[:limit] if not charm_pics.is_charm(f)) if h]
        if charm:
            imgs.append(self._charm_figs(out, label, charm))
        imgs = [h for h in imgs if h]
        if imgs:
            out.append("".join(imgs))
            out.moveCursor(QTextCursor.MoveOperation.End)

    def switch(self, i):
        if self.stack.currentIndex() == 0 and i != 0:
            self._store_case()  # no SAVE needed: whatever is on Intake follows you to the other screens
        if self.stack.currentIndex() == 5 and i != 5 and self.test_worker is not None \
                and self.lbl_testmode.text().startswith("RUNNING"):
            self.test_worker.emergency_stop()  # STOP ALL lives on this screen: no test keeps running out of sight
        if i == 2 and hasattr(self, "txt_notes"):
            self.txt_notes.setPlainText(self.session.get("chat_notes"))
            verdict = self.session.get("final_verdict")
            if verdict and self.tabs_verdict.count() == 1 and not self.txt_verdict.toPlainText().strip():
                self._say(self.txt_verdict, verdict)     # a loaded visit's verdict, back in its tabs
                self._build_verdict_tabs(verdict)
        if i == MAINT_PAGE:
            self._maint_fill()
        self.stack.setCurrentIndex(i)
        for j, b in enumerate(self.nav):
            b.setChecked(i == j)

    def _warm_retriever(self):
        """Load the embedding model and this car's vectors on a thread right after the car is picked. The first
        vector search did that on whatever called it: 11 s on the PC for chevrolet.db, far longer on the deck,
        and /fsm, maintenance and the repair guide still search on the UI thread."""
        r = self.retriever
        if not hasattr(r, "search_ids"):
            return
        w = WarmWorker(r)
        self.warmers = [x for x in getattr(self, "warmers", []) if x.isRunning()] + [w]
        w.start()

    def _lookup(self, text, budget, then, **kw):
        """case_context off the UI thread, then then(ctx) back on it. The first lookup on a big make loads the
        embedding model and the car's vectors: seconds on the PC, a minute or more on the deck, and the window
        looked crashed while it waited (11-02)."""
        w = CaseContextWorker(self.retriever, text, budget, **kw)
        self.lookups = [x for x in getattr(self, "lookups", []) if x.isRunning()] + [w]
        self.lbl_llm.setText("LOOKING UP THE MANUAL...")
        self.lbl_llm.setStyleSheet(BUSY)
        w.result.connect(then)
        w.start()

    def _busy(self):
        if any(w.isRunning() for w in getattr(self, "lookups", [])):
            self.lbl_llm.setText("busy: looking up the manual for the last request")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        if self.test_worker is not None and self.lbl_testmode.text().startswith("RUNNING"):
            # a model crash would take the running test's repeat thread with it before the switch-off
            self.lbl_llm.setText("a test is running: STOP it first (TESTS screen)")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        if self.worker is not None and self.worker.isRunning():
            self.lbl_llm.setText("busy: wait for the current answer, or press STOP")
            self.lbl_llm.setStyleSheet(BUSY)
            return True
        return False

    def answer_room(self, prompt):
        """Tokens the answer may use: whatever the model's context has left after the prompt. Answers end where
        the model ends them (the prompt says how long); this only stops one that would overflow the context.
        11-01: fixed caps per mode (700 for wildcard, 300 for chat...) cut answers off mid-sentence."""
        on_pc = bool(getattr(self.mgr, "home", False)) and not getattr(self.mgr, "pc_why", None)
        ctx = CFG["home_context_len"] if on_pc else CFG["max_context_len"]
        return max(256, ctx - int(len(prompt) / 3.0) - 128)    # ~3 chars a token, rounded toward safe

    def _run(self, role, prompt, out: QTextEdit, on_done, keep_history=False, max_new_tokens=None, temperature=None):
        # a caller's number is ignored on purpose: only the context limit applies (see answer_room)
        max_new_tokens = self.answer_room(prompt)
        self.worker = GenWorker(self.mgr, role, prompt, keep_history, max_new_tokens, temperature)
        plain = QTextCharFormat()
        plain.setForeground(QColor(T.TEXT))

        def tok(t):
            c = out.textCursor()
            c.movePosition(QTextCursor.MoveOperation.End)
            c.insertText(t, plain)  # plain text, so the coloured label before it doesn't bleed into the answer
            out.setTextCursor(c)
            out.ensureCursorVisible()

        def status(s):
            self.lbl_llm.setText(s.upper())
            self.lbl_llm.setStyleSheet(BUSY)

        def done(text):
            self._llm_idle()
            if "[LLM error" in text:
                self.lbl_llm.setText("model error: see the answer box")
                self.lbl_llm.setStyleSheet(ERR)
            on_done(text)

        self.worker.token.connect(tok)
        self.worker.status.connect(status)
        self.worker.done.connect(done)
        self.worker.start()

    def act_stop(self):
        if self.worker is not None and self.worker.isRunning():
            model = self.mgr.models.get(self.worker.role)
            if model:
                model.abort()

    # ------------------------------------------------------------------ voice
    def _start_voice(self):
        listen, dictate, why = voice.voice_status(CFG) if CFG["voice_enabled"] else (False, False, "voice off in config")
        self.lbl_voice.setText(why)
        self.b_mic.setEnabled(dictate)
        if listen or dictate:
            self.voice = voice.VoiceClient(CFG, self._voice_vocabulary() if listen else [])
            self.voice.command.connect(self.on_voice)
            self.voice.note.connect(self.on_note)
            self.voice.status.connect(self.on_voice_status)
            self.voice.start()

    def _stop_voice(self):
        if self.voice:
            self.voice.stop()
            self.voice.wait(1500)
            self.voice = None

    def _fill_mics(self, refresh=False):
        self.cmb_mic.blockSignals(True)
        keep = self.cmb_mic.currentData() if self.cmb_mic.count() else CFG.get("mic_device", "")
        self.cmb_mic.clear()
        self.cmb_mic.addItem("Mic: system default", "")
        for _, name in voice.input_devices(refresh=refresh):
            self.cmb_mic.addItem(f"Mic: {name}", name)
        if keep:
            i = self.cmb_mic.findData(keep)
            if i < 0:
                self.cmb_mic.addItem(f"Mic: {keep}", keep)
                i = self.cmb_mic.count() - 1
            self.cmb_mic.setCurrentIndex(i)
        self.cmb_mic.blockSignals(False)

    def act_mic_changed(self):
        CFG["mic_device"] = self.cmb_mic.currentData() or ""
        save_config_key("mic_device", CFG["mic_device"])
        self._stop_voice()
        self._start_voice()

    def _speak(self, text):
        """Say text with the listener paused, so the mic doesn't hear the deck (it once typed
        "CyberVecordio Check" into Notes)."""
        if self.voice is not None:
            self.voice.send("pause")
            ms = int(1500 + len(text) * 1000 / (CFG.get("tts_rate", 145) * 5.5 / 60))  # ~5.5 chars per word
            QTimer.singleShot(ms, lambda: self.voice is not None and self.voice.send("resume"))
        return voice.speak(CFG, text)

    def act_speaker_test(self):
        if not self._speak("Cyberdeck audio check."):
            self.lbl_voice.setText("no speech engine: sudo apt install espeak-ng")

    def act_mic(self):
        if self.voice is not None and not self.voice.isRunning():  # the voice process died: start it again
            self._stop_voice()
            self._start_voice()
        if self.voice is None:
            self.lbl_voice.setText("dictation needs whisper.cpp set up (see README)")
            return
        if self.b_mic.text().startswith("●"):  # already listening: this press means "done talking"
            self.voice.send("dictate")
            return
        self._start_dictation(self.stack.currentIndex())
        self.voice.send("dictate")

    def _track_focus(self, old, new):
        if isinstance(new, (QLineEdit, QPlainTextEdit, QTextEdit)) and not new.isReadOnly():
            self.last_edit = new

    def _edit_target(self):
        """The text box dictation should type into: the last one the cursor was in, if it's on screen now."""
        w = self.last_edit
        try:
            return w if w is not None and w.isVisible() and w.isEnabled() and not w.isReadOnly() else None
        except RuntimeError:   # the box was deleted (a page rebuilt)
            return None

    def _start_dictation(self, page):
        self.dictate_target = page
        self.dictate_widget = self._edit_target()
        self.b_mic.setText("● DONE")
        self.b_mic.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT};")

    @staticmethod
    def _insert_text(w, text):
        """Type text at the cursor, with a space before it when it would run into a word."""
        if isinstance(w, QLineEdit):
            pos, cur = w.cursorPosition(), w.text()
            w.insert((" " if pos and not cur[pos - 1].isspace() else "") + text)
        else:
            c = w.textCursor()
            before = w.document().characterAt(c.position() - 1) if c.position() else ""
            c.insertText((" " if before and not before.isspace() else "") + text)
            w.setTextCursor(c)
        w.setFocus()

    def on_voice_status(self, s):
        self.lbl_voice.setText(s)
        if s.startswith(("dictation", "note transcribed", "whisper error")):
            self.b_mic.setText("🎤 MIC")
            self.b_mic.setStyleSheet("")

    def _voice_vocabulary(self):
        """Part names from the kb, so Vosk's closed phrase list can still hear 'torque for caliper bracket bolt'."""
        words = set()
        try:
            db = getattr(self.retriever, "db", None) or self.retriever.kb
            sql = "SELECT component FROM spec" if hasattr(self.retriever, "db") else "SELECT component FROM fast_specs"
            for (c,) in db.execute(sql):
                words |= {w for w in re.findall(r"[a-z]{3,}", (c or "").lower())}
        except Exception as e:
            log_caught("voice vocabulary", e)
        return sorted(words)[:3000]

    def on_voice(self, cmd, arg):
        if cmd == "read_car":
            self.switch(0)
            self.act_read_obd()
        elif cmd == "save_case":
            self.act_save_case()
        elif cmd == "new_case":
            self.act_new_case()
        elif cmd == "verdict":  # shown on screen, never spoken
            self.switch(2)
            self.act_verdict()
        elif cmd == "torque":
            rows = focus_specs(arg, self.retriever.specs(arg, limit=30))[:8] or self.retriever.specs(arg)
            self.switch(1)
            self._say(self.txt_chat, f"/torque {arg}", who="You (voice)")
            self._say(self.txt_chat, "\n".join("  " + r for r in rows) or "  no match", T.OK)
            self._speak(re.sub(r"^\[[^\]]*\]\s*", "", rows[0]).replace("N·m", "newton meters")
                        .replace("ft-lb", "foot pounds") if rows else f"No torque spec found for {arg}")
        elif cmd == "fluid":
            hits = fsm_hits(self.retriever, f"{arg} fluid capacity specification", k=1, chars=700)
            self.switch(1)
            self._say(self.txt_chat, f"fluid {arg}", who="You (voice)")
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no FSM match")
            self._speak("There's no fluid table yet. The closest manual section is on screen.")
        elif cmd == "next_step":
            self._speak(self._next_step(arg))
        elif cmd == "goto":
            self.switch({"intake": 0, "chat": 1, "verdict": 2, "repair": 3, "procedure": 3, "parts": 4,
                         "maintenance": MAINT_PAGE}[arg])
        elif cmd == "stop":
            self.act_stop()
        elif cmd == "dictating":  # "hey deck" started whisper: the text goes where MIC would put it
            if arg == "intake":
                self.switch(0)
                self.last_edit = self.txt_note
            self._start_dictation(self.stack.currentIndex())

    def _next_step(self, said):
        """Stateless: match what was said against the open procedure's numbered steps, return the next one."""
        steps = re.findall(r"^\s*(\d+)[.)]\s+(.+)$", self.txt_sop.toPlainText(), flags=re.M)
        if not steps:
            return "No repair procedure is open."
        if not said:
            return f"Step {steps[0][0]}. {steps[0][1]}"
        said_words = set(re.findall(r"[a-z]{3,}", said.lower()))
        scores = [len(said_words & set(re.findall(r"[a-z]{3,}", t.lower()))) for _, t in steps]
        best = max(range(len(steps)), key=lambda i: scores[i])
        if scores[best] == 0:
            return "I couldn't match that to a step."
        if best + 1 >= len(steps):
            return "That was the last step."
        n, text = steps[best + 1]
        return f"Step {n}. {text}"

    def on_note(self, text):
        """whisper.cpp text goes into the box the cursor was in when dictation started (any page). With no box
        picked: MIC on Chat asks it, on Repair sets the job, on Parts searches, otherwise it's an intake note."""
        target, self.dictate_target = self.dictate_target, None
        w, self.dictate_widget = self.dictate_widget, None
        self.b_mic.setText("🎤 MIC")
        self.b_mic.setStyleSheet("")
        if not text:
            return
        try:
            alive = w is not None and w.isEnabled() and not w.isReadOnly()
        except RuntimeError:
            alive = False
        if alive and w is not self.txt_note:
            self._insert_text(w, text)
            if w is self.in_chat:
                if self._busy():
                    self.lbl_voice.setText("model busy: your words are in the chat box, press Enter when it's free")
                else:
                    self.act_chat()
            elif w is self.in_part:
                self.act_find_parts()
            return
        if alive:   # the intake notes box: typed in where the cursor was, then summarized below
            self.switch(0)
            self._insert_text(w, text)
            self._summarize_note()
            return
        if target == 1:
            self.in_chat.setText(text)
            self.act_chat()
            return
        if target == 3:
            self.bub_repair.set_text(text)
            return
        if target == 4:
            self.in_part.setText(text)
            self.act_find_parts()
            return
        self.switch(0)
        self.txt_note.appendPlainText(text)
        self._summarize_note()

    def _summarize_note(self):
        self._say(self.txt_intake, "[🎤] Note added. Summarizing...", T.MUTED)
        if self._busy():
            self._say(self.txt_intake, "    (model busy: summary skipped; the raw note is kept)", T.MUTED)
            return
        prompt = ("Customer/mechanic intake note, transcribed from speech:\n" + self.txt_note.toPlainText() +
                  "\n\nPull out, as three short lines, only what the note says:\nSYMPTOMS: ...\n"
                  "PARTS ALREADY REPLACED OR CHECKED: ...\nWHEN IT HAPPENS: ...\nWrite 'not mentioned' where it says nothing.")
        self._say(self.txt_intake, "", who="Summary")
        self._run("fast", prompt, self.txt_intake, self._note_summary_done, keep_history=False, max_new_tokens=250)

    def _note_summary_done(self, text):
        self.session.set("intake_summary", "" if "[LLM error" in text else text.strip())

    # ------------------------------------------------------------------ 1. intake
    def _intake_view(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setObjectName("page")
        lay = QVBoxLayout(inner)

        scan = QGroupBox("SCAN THE CAR (or fill in by hand below)")
        row = QHBoxLayout(scan)
        self.cmb_adapter = QComboBox()
        self.cmb_adapter.addItem("OBDLink EX: OBD-II + VIN + Subaru SSM", "obdlink")
        self.cmb_adapter.addItem("Bare KKL cable (fallback): Subaru SSM", "kkl")
        self.cmb_adapter.setCurrentIndex(max(0, self.cmb_adapter.findData(CFG["obd_adapter"])))
        self.cmb_adapter.currentIndexChanged.connect(
            lambda: (CFG.__setitem__("obd_adapter", self.cmb_adapter.currentData()),
                     save_config_key("obd_adapter", self.cmb_adapter.currentData())))
        self.b_obd = QPushButton("READ CAR (codes + live data)")
        self.b_obd.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        self.b_obd.clicked.connect(self.act_read_obd)
        self.b_drive = QPushButton(f"TEST DRIVE (every {CFG.get('drive_every_s', 30)} s)")
        self.b_drive.setToolTip("Start before driving, stop after: reads the car the whole drive so the models see "
                                "idle, cruise and load, not one snapshot. Set it up parked; let a passenger work it.")
        self.b_drive.clicked.connect(self.act_test_drive)
        self.drive_worker = None
        self.drive_path = ""
        self.b_load_drive = QPushButton("LOAD DRIVE FILE")
        self.b_load_drive.setToolTip("Bring a saved test drive back into this case. Every drive is written to drives/ "
                                     "as it records, so one survives a crash.")
        self.b_load_drive.clicked.connect(self.act_load_drive)
        row.addWidget(self.cmb_adapter)
        row.addWidget(self.b_obd, 1)
        row.addWidget(self.b_drive)
        row.addWidget(self.b_load_drive)
        lay.addWidget(scan)

        veh = QGroupBox("VEHICLE")
        vbox = QVBoxLayout(veh)
        vin_row = QHBoxLayout()
        self.in_vin = QLineEdit()
        self.in_vin.setPlaceholderText("VIN (17 characters): filled by READ CAR when the car reports it, or type it")
        self.in_vin.setMaxLength(20)
        self.in_vin.editingFinished.connect(lambda: self._apply_vin(self.in_vin.text()))
        self.lbl_kb = QLabel("")
        self.lbl_kb.setWordWrap(True)
        vin_row.addWidget(QLabel("VIN"))
        vin_row.addWidget(self.in_vin, 2)
        vin_row.addWidget(self.lbl_kb, 3)
        self.b_tool = QPushButton("")
        self.b_tool.clicked.connect(self.act_open_tool)
        self.b_tool.hide()
        self.tool_proc = None
        vin_row.addWidget(self.b_tool)
        vbox.addLayout(vin_row)
        vr = QHBoxLayout()
        vbox.addLayout(vr)
        self.vehicles = vehicle_table(self.retriever)  # {(model, year): {engines}} from the manuals
        self.cmb_year, self.cmb_make, self.cmb_model, self.cmb_trans = QComboBox(), QComboBox(), QComboBox(), QComboBox()
        self._fill_years(self.cmb_year)
        self.cmb_make.addItems([""] + MAKES)  # each make loads its own kb/<make>.db
        self.cmb_make.currentTextChanged.connect(self._switch_make)
        self.cmb_trans.addItems(TRANS)
        self.in_miles = QLineEdit()
        self.in_miles.setPlaceholderText("miles")
        self.in_miles.setMaximumWidth(130)
        self.in_customer = QLineEdit()
        self.in_customer.setPlaceholderText("customer / plate")
        self.in_customer.setToolTip("Files the visit under this name when the car has no VIN on the deck")
        self.in_customer.setMaximumWidth(200)
        self.lbl_engine = QLabel("")
        self.lbl_engine.setStyleSheet(f"color: {T.MUTED};")
        self.cmb_year.currentTextChanged.connect(self._fill_models)
        self.cmb_model.currentTextChanged.connect(self._show_engine)
        for label, wdg in (("Year", self.cmb_year), ("Make", self.cmb_make), ("Model", self.cmb_model),
                           ("Trans", self.cmb_trans), ("Miles", self.in_miles), ("Customer", self.in_customer)):
            vr.addWidget(QLabel(label))
            vr.addWidget(wdg)
        vr.addWidget(self.lbl_engine, 1)
        lay.addWidget(veh)

        case = QGroupBox("WHAT'S WRONG")
        mf = QFormLayout(case)
        self.bub_dtcs = Field()
        self.bub_dtcs.setPlaceholderText("filled by READ CAR, or type them (e.g. P0420, P0171)")
        self.in_live = QLineEdit()
        self.in_live.setPlaceholderText("filled by READ CAR, or type readings (e.g. LTFT +12%, idle 650)")
        self.bub_symptoms = Field()  # typed or dictated; a button list can't cover what cars do
        self.bub_symptoms.setPlaceholderText("what it does, when (e.g. ticks when cold, stalls at lights)")
        self.bub_good = Field()
        self.bub_good.setPlaceholderText("already checked and fine (optional)")
        self.in_suspect = QLineEdit()
        self.in_suspect.setPlaceholderText("already suspected (optional)")
        self.in_history = QLineEdit()
        self.in_history.setPlaceholderText("recent work on the car (optional)")
        self.txt_note = QPlainTextEdit()
        self.txt_note.setPlaceholderText("Customer story. Press 🎤 MIC below, or say 'hey deck, intake note'")
        self.txt_note.setMaximumHeight(70)
        mf.addRow("Codes:", self.bub_dtcs)
        mf.addRow("Live data:", self.in_live)
        self.lbl_drive = QLabel("")
        self.lbl_drive.setStyleSheet(f"color: {T.MUTED};")
        self.lbl_drive.setWordWrap(True)
        mf.addRow("", self.lbl_drive)
        mf.addRow("Symptoms:", self.bub_symptoms)
        mf.addRow("Checked OK:", self.bub_good)
        mf.addRow("Suspect:", self.in_suspect)
        mf.addRow("History:", self.in_history)
        mf.addRow("Notes:", self.txt_note)
        lay.addWidget(case)

        btns = ReflowRow()   # wraps on a narrow (portrait) window
        b_save = QPushButton("SAVE CASE + SHOW WHAT THE MANUAL HAS")
        b_save.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b_save.clicked.connect(self.act_save_case)
        b_new = QPushButton("NEW CASE")
        b_new.clicked.connect(self.act_new_case)
        b_last = QPushButton("LOAD LAST CASE")
        b_last.clicked.connect(self.act_load_last)
        self.b_visits = QPushButton("PAST VISITS")
        self.b_visits.setToolTip("Earlier cases saved for this VIN")
        self.b_visits.setEnabled(False)
        self.b_visits.clicked.connect(self.act_past_visits)
        for b in (b_save, b_new, b_last, self.b_visits):
            btns.add(b)
        lay.addWidget(btns)

        self.txt_intake = QTextEdit()
        self.txt_intake.setReadOnly(True)
        self.txt_intake.setMinimumHeight(160)
        lay.addWidget(self.txt_intake)

        scroll.setWidget(inner)
        outer.addWidget(scroll)
        self.stack.addWidget(page)
        self._clear_case()  # 10-19: every start is a fresh case; LOAD LAST CASE brings the old one back
        self._load_case()
        self._say(self.txt_intake, "[*] New case. LOAD LAST CASE brings back the previous one.", T.MUTED)

    def _fill_years(self, cmb):
        keep = cmb.currentText()
        years = sorted({y for _, y in self.vehicles}, reverse=True) or list(range(2026, 1980, -1))
        cmb.blockSignals(True)
        cmb.clear()
        cmb.addItems([""] + [str(y) for y in years])
        cmb.blockSignals(False)
        cmb.setCurrentText(keep)

    def _model_list(self, year):
        """Models the make's manuals cover for that year; the built-in list only for Subaru without a kb."""
        have = sorted({m for m, y in self.vehicles if str(y) == year}) if self.vehicles else []
        return have or (MODELS if CFG["make"] == "Subaru" else [])

    def _fill_models(self, year):
        keep = self.cmb_model.currentText()
        names = self._model_list(year)
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        self.cmb_model.addItems([""] + names)
        self.cmb_model.setEditable(not names)  # no manuals for this make: type the model
        self.cmb_model.blockSignals(False)
        self.cmb_model.setCurrentText(keep if (keep in names or not names) else "")
        self._show_engine()

    def _launcher(self, make=None):
        """(name, argv) of the program configured for this make, or None."""
        entry = (CFG.get("launchers") or {}).get(make if make is not None else CFG["make"])
        if not entry:
            return None
        if isinstance(entry, str):
            entry = {"cmd": entry}
        cmd = entry.get("cmd") or ""
        argv = [os.path.expanduser(a) for a in (cmd if isinstance(cmd, list) else shlex.split(cmd))]
        if not argv:
            return None
        return entry.get("name") or os.path.splitext(os.path.basename(argv[-1]))[0], argv

    def _show_tool(self):
        tool = self._launcher()
        busy = self.tool_proc is not None
        self.b_tool.setVisible(bool(tool) or busy)
        if tool and not busy:
            self.b_tool.setText(f"OPEN {tool[0].upper()}")
            self.b_tool.setToolTip(f"Opens {tool[0]} for {CFG['make']} and lends it the cable until you close it")

    def act_open_tool(self):
        """Open the make's own program (e.g. FORScan under Wine). Only one program can use the cable,
        so READ CAR waits until that program is closed."""
        tool = self._launcher()
        if not tool or self.tool_proc is not None:
            return
        if self.obd_worker is not None and self.obd_worker.isRunning():
            self._say(self.txt_intake, "[!] Wait for READ CAR to finish, then open it.", T.AMBER)
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        name, argv = tool
        proc = QProcess(self)
        proc.finished.connect(lambda *_: self._tool_closed(name))
        proc.errorOccurred.connect(lambda e: self._tool_closed(name, f"could not start ({argv[0]}: {e.name})"))
        self.tool_proc = proc
        self.b_obd.setEnabled(False)
        self.b_obd.setText(f"CABLE LENT TO {name.upper()}")
        self.b_tool.setEnabled(False)
        self.b_tool.setText(f"{name.upper()} OPEN")
        self._say(self.txt_intake, f"[*] Opening {name}. The cable is its until you close it.", T.MUTED)
        proc.start(argv[0], argv[1:])

    def _tool_closed(self, name, error=""):
        if self.tool_proc is None:
            return
        self.tool_proc = None
        self.b_obd.setEnabled(True)
        self.b_obd.setText("READ CAR (codes + live data)")
        self.b_tool.setEnabled(True)
        self._show_tool()
        if error:
            self._say(self.txt_intake, f"[!] {name} {error}. Check \"launchers\" in config.json.", T.RED)
        else:
            self._say(self.txt_intake, f"[✓] {name} closed; the cable is back for READ CAR.", T.OK)

    def _show_kb(self):
        ok = os.path.isfile(CFG["kb_db"] or "") or CFG["make"] == "Subaru" and isinstance(
            self.retriever, LegacyRetriever)
        search = ""
        if os.path.isfile(CFG["kb_db"] or ""):
            search = (" · vector search on" if getattr(self.retriever, "embed", None) is not None
                      else " · keyword search only")
        self.lbl_kb.setText(self.kb_msg + search)
        self._show_tool()
        self.lbl_kb.setStyleSheet(f"color: {T.OK};" if ok else f"color: {T.AMBER};")

    def _switch_make(self, make):
        """Load the make's knowledge DB (startup, Make dropdown, VIN, or the SSM reply)."""
        if make == CFG["make"] and hasattr(self, "lbl_kb") and self.lbl_kb.text():
            return
        self.kb_msg = use_make(make)
        try:
            self.retriever = make_retriever(CFG)
            self._warm_retriever()
        except Exception as e:
            self.retriever = NullRetriever()
            self.kb_msg = f"{make} database failed to open: {e}"
        self.vehicles = vehicle_table(self.retriever)
        self.lbl_engine.setText("")  # another make's engine must not carry over
        self._fill_years(self.cmb_year)
        self._fill_models(self.cmb_year.currentText())
        if hasattr(self, "cmb_p_make"):
            self.cmb_p_make.setCurrentText(make)
            self._fill_years(self.cmb_p_year)
            self._fill_p_models(self.cmb_p_year.currentText())
        self._show_kb()

    def _apply_vin(self, text):
        """Typed or read VIN -> make (loads its DB), year, and model where the VIN says it plainly."""
        if not text.strip():
            return
        d = decode_vin(text, CFG.get("vin_db"), nhtsa_cache_path())
        if d["note"] and not d["make"]:
            self._say(self.txt_intake, f"[VIN] {d['note']}", T.AMBER)
            return
        self.in_vin.setText(d["vin"])
        if getattr(self, "last_ecu", "") and d.get("vin"):  # typed after a read: remember it for this ECU now
            remember_ecu_vin(self.last_ecu, d["vin"])
        if d["make"]:
            self.cmb_make.setCurrentText(d["make"])
        if d["year"]:
            self.cmb_year.setCurrentText(str(d["year"]))
        if d["model"]:
            self._fill_models(self.cmb_year.currentText())
            if self.cmb_model.findText(d["model"]) < 0 and self.cmb_model.isEditable():
                self.cmb_model.addItem(d["model"])
            self.cmb_model.setCurrentText(d["model"])
        if d.get("engine") and not self.lbl_engine.text():
            self.lbl_engine.setText(f"Engine: {d['engine']}")
        self._set_trans(d.get("trans", ""))
        if not d.get("engine") and not getattr(self, "vin_worker", None):
            # nothing saved for this car yet: ask NHTSA (free) if there's internet; the answer is kept offline
            self.vin_worker = VinWorker(d["vin"])
            self.vin_worker.result.connect(self._nhtsa_done)
            self.vin_worker.finished.connect(lambda: setattr(self, "vin_worker", None))
            self.vin_worker.start()
        found = " ".join(str(x) for x in (d["year"], d["make"], d["model"]) if x)
        self._say(self.txt_intake, f"[VIN] {found or 'not decoded'}" + (f" ({d['note']})" if d["note"] else "")
                  + ("" if d["model"] else ". Pick the Model below."), T.OK)
        self._show_past_visits(d["vin"])

    def _set_trans(self, trans):
        t = (trans or "").lower()
        pick = "Manual" if "manual" in t else "Automatic" if re.search(r"auto|cvt|continuously", t) else ""
        if pick:
            self.cmb_trans.setCurrentText(pick)

    def _nhtsa_done(self, vin, d):
        if vin != self.in_vin.text().strip().upper():
            return   # the VIN was changed while NHTSA answered
        if not d:
            self._say(self.txt_intake, "[VIN] No internet (or NHTSA doesn't know it): engine and transmission "
                      "come from the manuals once you pick the model.", T.MUTED)
            return
        if d.get("make") and d["make"] != self.cmb_make.currentText():
            self.cmb_make.setCurrentText(d["make"])
        if d.get("year"):
            self.cmb_year.setCurrentText(str(d["year"]))
        if d.get("model"):
            self._fill_models(self.cmb_year.currentText())
            hit = next((self.cmb_model.itemText(i) for i in range(self.cmb_model.count())
                        if self.cmb_model.itemText(i) and d["model"].lower().startswith(
                            self.cmb_model.itemText(i).lower())), "")
            if not hit and self.cmb_model.isEditable():
                self.cmb_model.addItem(d["model"])
                hit = d["model"]
            if hit:
                self.cmb_model.setCurrentText(hit)
        if d.get("engine"):
            self.lbl_engine.setText(f"Engine: {d['engine']}")
        self._set_trans(d.get("trans"))
        desc = ", ".join(x for x in (" ".join(str(y) for y in (d.get("year"), d.get("make"), d.get("model"),
                                                                d.get("trim")) if y),
                                     d.get("engine"), d.get("trans"), d.get("drive")) if x)
        self._say(self.txt_intake, f"[VIN] NHTSA: {desc}. Saved on the deck for next time.", T.OK)

    def _apply_ident(self, how, value, engine, ecu=""):
        """What the car itself said: a VIN over OBD-II, an SSM reply (always a Subaru), or only a hint.
        A VIN typed in once is remembered against the ECU's ROM/calibration id, so the next read fills it."""
        self.last_ecu = ecu
        # a VIN already on screen may be the previous car's (NEW CASE keeps the car): only one typed after
        # this read gets tied to this ECU
        self.vin_at_read = self.in_vin.text().strip().upper()
        known = ecu_vins().get(ecu, "") if ecu else ""
        if how == "vin":
            self._apply_vin(value)
            return
        if known and not self.in_vin.text().strip():
            self._say(self.txt_intake, f"[car] Known ECU ({ecu}): using the VIN saved for it.", T.OK)
            self.in_vin.setText(known)
            self._apply_vin(known)
        if how == "hint":
            if not known:
                self._say(self.txt_intake, f"[car] No VIN from this car ({value or 'no hint'}).", T.AMBER)
            return
        make = make_from_ssm(value)
        if make and self.cmb_make.currentText() != make:
            self.cmb_make.setCurrentText(make)
        if engine and not self.lbl_engine.text():
            self.lbl_engine.setText(f"Engine: {engine}")
        car = None if known else car_from_rom(ecu.replace("ROM ", ""), CFG.get("rom_db"))
        if car and car["year"]:
            self.cmb_year.setCurrentText(str(car["year"]))
            self._fill_models(self.cmb_year.currentText())
            # the definition's model name must match one the manuals use ('Forester', not 'Forester XT 5MT')
            hit = next((self.cmb_model.itemText(i) for i in range(self.cmb_model.count())
                        if self.cmb_model.itemText(i) and car["model"].lower().startswith(
                            self.cmb_model.itemText(i).lower())), "")
            if hit:
                self.cmb_model.setCurrentText(hit)
            desc = " ".join(x for x in (str(car["year"]), make, car["model"], car["submodel"], car["transmission"],
                                        car["market"]) if x)
            self._say(self.txt_intake, f"[car] ECU {ecu}: built for a {desc} (RomRaider ECU list)."
                      + ("" if hit else " Pick the Model below.") + " Type the VIN once to be sure: the deck "
                      "remembers it for this ECU.", T.OK)
            return
        if not known:
            self._say(self.txt_intake, f"[car] ECU answered SSM (ID {value}): {make}, {engine or 'engine unknown'}. "
                      "This car doesn't report its VIN (most cars before 2005 don't, over any cable). Type it "
                      "once (windshield or door jamb): the deck remembers it for this ECU and fills it in "
                      "on every read after.", T.OK)

    def _show_engine(self, *_):
        """The engine comes from the manuals for that model/year; no need to know it."""
        try:
            year = int(self.cmb_year.currentText())
        except ValueError:
            year = None
        engines = sorted(e for e in self.vehicles.get((self.cmb_model.currentText(), year), ()) if e)
        if engines or self.vehicles:  # keep an engine the SSM reply gave when there are no manuals
            self.lbl_engine.setText(f"Engine: {' or '.join(engines)}" if engines else "")

    def _title_text(self):
        return f"SYSTEM: {(self.session.get('vehicle') or CFG['vehicle'] or 'no vehicle picked (Intake)').upper()}"

    def _load_case(self):
        v = {}
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            pass
        if "make" not in v and v.get("model"):  # cases saved before makes existed were all Subaru
            v["make"] = "Subaru"
        self.cmb_make.blockSignals(True)
        self.cmb_make.setCurrentText(v.get("make", ""))
        self.cmb_make.blockSignals(False)
        if v.get("make", "") != CFG["make"]:
            self._switch_make(v.get("make", ""))
        self._show_kb()
        self.in_vin.setText(v.get("vin", ""))
        self.cmb_year.setCurrentText(v.get("year", ""))
        self._fill_models(self.cmb_year.currentText())
        self.cmb_model.setCurrentText(v.get("model", ""))
        self.cmb_trans.setCurrentText(v.get("trans", ""))
        self.in_miles.setText(v.get("miles", ""))
        self.in_customer.setText(v.get("customer", ""))
        self.bub_dtcs.set_text(self.session.get("dtcs"))
        self.in_live.setText(self.session.get("telemetry"))
        self._show_drive_line()
        self.bub_symptoms.set_text(self.session.get("symptoms"))
        self.bub_good.set_text(self.session.get("ruled_out"))
        self.in_suspect.setText(self.session.get("suspect"))
        self.in_history.setText(self.session.get("history"))
        self.txt_note.setPlainText(self.session.get("intake_note"))

    def _store_case(self):
        """Write Intake to the session. If the case changed, the chat starts over with the new case."""
        v = {"year": self.cmb_year.currentText(), "make": self.cmb_make.currentText(),
             "model": self.cmb_model.currentText(), "vin": self.in_vin.text().strip().upper(),
             "trans": self.cmb_trans.currentText(), "miles": self.in_miles.text().strip(),
             "engine": self.lbl_engine.text().replace("Engine: ", ""), "customer": self.in_customer.text().strip()}
        self.session.set("vehicle_info", json.dumps(v))
        key = self._file_key(v)
        if key.startswith("NOVIN|") and key != getattr(self, "shown_key", ""):
            self.shown_key = key
            self._show_past_visits(key)
        if v["vin"] and getattr(self, "last_ecu", "") and v["vin"] != getattr(self, "vin_at_read", ""):
            remember_ecu_vin(self.last_ecu, v["vin"])
        desc = " ".join(x for x in (v["year"], v["make"], v["model"]) if x)
        extra = ", ".join(x for x in (v["engine"], v["trans"], f"{v['miles']} mi" if v["miles"] else "") if x)
        self.session.set("vehicle", desc + (f" ({extra})" if extra else ""))
        self.lbl_title.setText(self._title_text())
        # "(pending)" / "(history)" etc. stay with their code, so the models see the status the scan tool gave
        codes = [f"{c} ({st})" if st else c for c, st in sanity_check.parse_codes(self.bub_dtcs.text())
                 if re.fullmatch(r"[PBCU]\d{4}", c)]
        self.session.set("dtcs", ", ".join(dict.fromkeys(codes)))
        self.session.set("telemetry", self.in_live.text().strip())
        # TELEMETRY: which reader wrote the live line (ssm / obd01); unknown once it's typed or edited by hand
        read = CFG.get("_live") or ("", "")
        self.session.set("telemetry_source", read[0] if self.in_live.text().strip() == read[1].strip() else "")
        self.session.set("symptoms", self.bub_symptoms.text())
        self.session.set("ruled_out", self.bub_good.text())
        self.session.set("suspect", self.in_suspect.text().strip())
        self.session.set("history", self.in_history.text().strip())
        note = self.txt_note.toPlainText().strip()
        self.session.set("intake_note", note)
        if not note:
            self.session.set("intake_summary", "")
        # follow the picked car in the manual lookups
        model = v["model"] or None
        year = int(v["year"]) if v["year"].isdigit() else None
        if (model, year) != (CFG.get("kb_model"), CFG.get("kb_year")):
            CFG["kb_model"], CFG["kb_year"] = model, year
            if hasattr(self, "cmb_p_year"):
                self._sync_parts_car()
            try:
                self.retriever = make_retriever(CFG)
                self._warm_retriever()
            except Exception as e:
                self._say(self.txt_intake, f"[!] manual lookup not reloaded: {e}", T.AMBER)
        if not key:
            self.session.set("earlier_visits", "")
        self._past_readings(key, v)
        case = self.session.case_summary()
        if case != self.last_case:
            self.last_case = case
            self.session.set("final_verdict", "")
            self.chat_started = False
        self._file_visit()
        return case

    # ------------------------------------------------------------------ work history per car (VIN)
    def _file_visit(self):
        """File the open case under its car's VIN (one row per case, updated as it changes)."""
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        vin = self._file_key(v)
        case = {k: self.session.get(k) for k in self.CASE_KEYS}
        if not vin or not any(case.values()):
            return
        vid = self.session.save_visit(self.session.get("visit_id"), vin, self.session.get("vehicle"), case)
        self.session.set("visit_id", str(vid))
        if CFG.get("car_records", True):
            try:
                rec = car_records.CarRecords(vin)
                rec.update_car(v)
                rec.save_visit(vid, self.session.get("vehicle"), case, v.get("miles", ""))
            except Exception as e:
                self._records_warn(e)
            self._flush_records()

    # ------------------------------------------------------------------ per-car record file (records/<VIN>.db)
    # Every reading (READ CAR and test drive), the chat and each visit's case go into the car's own SQLite file.
    # Until the car has a VIN or customer key they wait in the session (records_pending), then get filed.
    def _rec_car(self, v):
        return {"make": v.get("make"), "model": v.get("model"), "year": v.get("year"), "engine": v.get("engine"),
                "transmission": v.get("trans"), "vin": v.get("vin"), "db": os.path.expanduser(CFG.get("kb_db") or "")}

    def _records_warn(self, e):
        if not getattr(self, "_rec_warned", False):   # once per run, not every reading
            self._rec_warned = True
            self._say(self.txt_intake, f"[!] Car record file not written (kept to try again): {e}", T.AMBER)

    def _record(self, kind, **kw):
        if not CFG.get("car_records", True):
            return
        item = {"kind": kind, "t": time.time(), "visit_ref": self.session.get("visit_id"), **kw}
        try:
            pend = json.loads(self.session.get("records_pending") or "[]")
        except ValueError:
            pend = []
        pend.append(item)
        self.session.set("records_pending", json.dumps(pend[-3000:]))
        self._flush_records()

    def _flush_records(self):
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
            pend = json.loads(self.session.get("records_pending") or "[]")
        except ValueError:
            return
        key = self._file_key(v)
        if not key or not pend:
            return
        try:
            rec, car, vid = car_records.CarRecords(key), self._rec_car(v), self.session.get("visit_id")
            for x in pend:
                if x["kind"] == "chat":
                    rec.add_chat(x["t"], x.get("sender", ""), x.get("message", ""), x.get("visit_ref") or vid)
                else:
                    rec.add_reading(x["t"], x["kind"], x.get("codes", ""), x.get("live", ""), x.get("source", ""),
                                    x.get("visit_ref") or vid, x.get("drive_id", ""), v.get("miles", ""), car)
            self.session.set("records_pending", "")
        except Exception as e:
            self._records_warn(e)

    def _past_readings(self, key, v):
        """The values read now next to the same values at the car's earlier visits (Python, from the record file)."""
        live = self.session.get("telemetry")
        text = ""
        if key and live and CFG.get("car_records", True):
            try:
                text = car_records.CarRecords(key).compare_text(live, self.session.get("telemetry_source"),
                                                                self._rec_car(v), self.session.get("visit_id"))
            except Exception as e:
                self._records_warn(e)
        self.session.set("past_readings", text)

    @staticmethod
    def _live_source(live):
        read = CFG.get("_live") or ("", "")
        return read[0] if live and live.strip() == (read[1] or "").strip() else ""

    @staticmethod
    def _file_key(v):
        """Where this car's visits are filed: its VIN, else customer/plate + year make model (a car the deck
        couldn't read a VIN from still gets a history). '' when there's nothing to file it under."""
        vin = (v.get("vin") or "").upper()
        if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
            return vin
        cust = re.sub(r"\s+", " ", (v.get("customer") or "").strip().lower())
        car = " ".join(str(v.get(k) or "") for k in ("year", "make", "model")).strip().lower()
        return f"NOVIN|{cust}|{car}" if cust and v.get("model") else ""

    @staticmethod
    def _visit_line(started, case):
        verdict = re.sub(r"\s+", " ", re.sub(r"<think>.*?</think>", "", case.get("final_verdict") or "", flags=re.S))
        bits = [started[:10], case.get("dtcs") or "no codes", case.get("symptoms") or ""]
        if verdict.strip():
            bits.append("verdict: " + verdict.strip()[:90])
        return " | ".join(b for b in bits if b)

    def _show_past_visits(self, vin):
        rows = self.session.visits(vin, exclude=self.session.get("visit_id") or None) if vin else []
        self.past_visits = rows
        self.b_visits.setEnabled(bool(rows))
        self.b_visits.setText(f"PAST VISITS ({len(rows)})" if rows else "PAST VISITS")
        lines = [self._visit_line(started, case) for _, started, _, case in rows]
        # the models see a short version too, so "it came back" is part of the case
        self.session.set("earlier_visits", "; ".join(lines[:3])[:600])
        if rows:
            self._say(self.txt_intake, f"[history] {len(rows)} earlier visit(s) for this car (PAST VISITS opens one):\n"
                      + "\n".join("    " + l for l in lines[:5]), T.PURPLE_TEXT)

    def act_past_visits(self):
        rows = getattr(self, "past_visits", [])
        if not rows:
            return
        labels = [self._visit_line(started, case)[:110] for _, started, _, case in rows]
        pick, ok = QInputDialog.getItem(self, "Past visits", "Open which visit? (the case on screen is kept "
                                        "for LOAD LAST CASE)", labels, 0, False)
        if not ok:
            return
        vid, started, _, case = rows[labels.index(pick)]
        self._clear_case()
        for k in self.CASE_KEYS:
            self.session.set(k, case.get(k, ""))
        self.session.set("visit_id", str(vid))
        self._load_case()
        self.chat_started = False
        self.txt_intake.clear()
        self._say(self.txt_intake, f"[*] Visit from {started} loaded. Changes are saved to that visit.", T.MUTED)
        self.last_case = self._store_case()
        self._show_past_visits(self._file_key(json.loads(self.session.get("vehicle_info") or "{}")))

    def act_read_obd(self):
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        self._say(self.txt_intake, f"[*] Reading the car ({self.cmb_adapter.currentText()})...", T.MUTED)
        self.b_obd.setEnabled(False)
        self.b_obd.setText("READING...")
        self.obd_worker = OBDWorker(self.cmb_adapter.currentData(), CFG["obd_port"], self.cmb_make.currentText())
        self.obd_worker.result.connect(self._obd_done)
        self.obd_worker.ident.connect(self._apply_ident)
        self.obd_worker.note.connect(lambda n: self._say(self.txt_intake, f"[car] {n}", T.AMBER))
        self.obd_worker.mode06.connect(lambda t: self.session.set("mode06", t))
        self.obd_worker.finished.connect(lambda: (self.b_obd.setEnabled(True),
                                                  self.b_obd.setText("READ CAR (codes + live data)")))
        self.obd_worker.start()

    def _obd_done(self, codes, live):
        """A scan replaces the codes with what the car has now (10-24: appending repeated them every
        read). Codes typed by hand are replaced too: the car's answer is the current state."""
        old = self.bub_dtcs.text().strip()
        self.bub_dtcs.set_text(codes)
        if old and old != codes:
            self._say(self.txt_intake, f"[*] Codes before this scan: {old}", T.MUTED)
        if live:
            self.in_live.setText(live)
        self._say(self.txt_intake, f"[✓] Codes: {codes or 'none stored'} | {live}", T.AQUA)  # live values: aqua
        self._record("read", codes=codes, live=live, source=self._live_source(live))

    # ------------------------------------------------------------------ TEST DRIVE
    def act_test_drive(self):
        if self.drive_worker is not None:   # second tap: stop
            self.drive_worker.stop_flag.set()
            self.b_drive.setEnabled(False)
            self.b_drive.setText("STOPPING...")
            return
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            self._say(self.txt_intake, "[!] Wait for READ CAR (or the other program) to finish first.", T.AMBER)
            return
        if self.test_worker is not None:
            self._say(self.txt_intake, "[!] TEST MODE has the cable. Switch it off on the TESTS screen first.", T.AMBER)
            return
        every = max(10, int(CFG.get("drive_every_s", 30)))
        self.drive_id = str(time.time())
        self.drive_n = 0
        self.drive_notes = set()
        self.session.set("drive_id", self.drive_id)
        self.session.set("drive_log", "")
        self.session.set("drive_log_short", "")
        self._show_drive_line()
        self.drive_path = self._drive_file_start(self.drive_id)
        self._say(self.txt_intake, f"[*] Test drive started: reading the car every {every} s. Drive through idle, "
                  "cruise and some harder acceleration, then tap STOP TEST DRIVE. Eyes on the road.", T.FOREST_TEXT)
        w = DriveWorker(self.cmb_adapter.currentData(), CFG["obd_port"], every, self.cmb_make.currentText())
        self.drive_worker = self.obd_worker = w   # READ CAR, TEST MODE and other programs see the cable is busy
        w.sample.connect(self._drive_sample)
        first = {"done": False}

        def ident(*a):   # who the car is: once per drive is enough
            if not first["done"]:
                first["done"] = True
                self._apply_ident(*a)
        w.ident.connect(ident)
        w.note.connect(self._drive_note)
        w.finished.connect(self._drive_finished)
        self.b_drive.setText("STOP TEST DRIVE (0 readings)")
        self.b_drive.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT};")
        self.b_obd.setEnabled(False)
        w.start()

    def _drive_note(self, n):
        if n not in self.drive_notes:   # every 30 s the same note would bury the screen
            self.drive_notes.add(n)
            self._say(self.txt_intake, f"[car] {n}", T.AMBER)

    def _drive_file_start(self, drive_id):
        car = " ".join(x for x in (self.cmb_year.currentText(), self.cmb_make.currentText(),
                                   self.cmb_model.currentText()) if x)
        try:
            return drive_file.start(drive_id, car)
        except (OSError, ValueError) as e:     # the drive still goes on, into the session DB
            self._say(self.txt_intake, f"[!] Couldn't start a drive file: {e}", T.AMBER)
            return ""

    def act_load_drive(self):
        if self.drive_worker is not None:
            self._say(self.txt_intake, "[!] Stop the test drive first.", T.AMBER)
            return
        self._recover_drives()
        path = QFileDialog.getOpenFileName(self, "Load test drive", drive_file.DIR, "Test drives (*.jsonl)")[0]
        if not path:
            return
        try:
            head, rows = drive_file.read(path)
        except OSError as e:
            self._say(self.txt_intake, f"[!] Couldn't read that file: {e}", T.AMBER)
            return
        if not rows:
            self._say(self.txt_intake, "[!] That drive has no readings in it.", T.AMBER)
            return
        self.drive_id = str(head.get("drive_id") or rows[0][0])
        if not self.session.drive_samples(self.drive_id):
            for row in rows:
                self.session.add_drive_sample(self.drive_id, *row)
        self.session.set("drive_id", self.drive_id)
        self.drive_n = len(rows)
        self._build_drive_log()
        self._drive_codes(rows[-1][1], rows[-1][2])
        self._store_case()
        self._say(self.txt_intake, f"[✓] Loaded {len(rows)} readings from {os.path.basename(path)}"
                  + (f" ({head['car']})" if head.get("car") else "") + ". The models now see this drive.", T.OK)

    def _recover_drives(self):
        """Drives from before files existed, or a crashed one: their readings are in the session DB, so give each a file."""
        for drive_id in self.session.drive_ids():
            try:
                if drive_file.has(drive_id):
                    continue
                path = drive_file.start(drive_id)
                for row in self.session.drive_samples(drive_id):
                    drive_file.add(path, *row)
            except (OSError, ValueError) as e:
                self._say(self.txt_intake, f"[!] Couldn't save drive {drive_id} to a file: {e}", T.AMBER)

    def _drive_sample(self, t, codes, live):
        self.session.add_drive_sample(self.drive_id, t, codes, live)
        self._record("drive", t=t, codes=codes, live=live, drive_id=self.drive_id, source=self._live_source(live))
        try:
            if self.drive_path:
                drive_file.add(self.drive_path, t, codes, live)
        except OSError as e:
            self._drive_note(f"Drive file not written: {e}")
        self.drive_n += 1
        self._obd_done(codes, live) if self.drive_n == 1 else self._drive_codes(codes, live)
        self.b_drive.setText(f"STOP TEST DRIVE ({self.drive_n} readings)")
        self._build_drive_log()

    def _drive_codes(self, codes, live):
        """Later readings: same replace rule as READ CAR, but only a changed code list is worth a line."""
        old = self.bub_dtcs.text().strip()
        if codes != old:
            self.bub_dtcs.set_text(codes)
            self._say(self.txt_intake, f"[car] Codes now: {codes or 'none'} (were: {old or 'none'})", T.AMBER)
        if live:
            self.in_live.setText(live)

    def _build_drive_log(self):
        rows = self.session.drive_samples(getattr(self, "drive_id", "") or self.session.get("drive_id"))
        if not rows:
            return
        self.session.set("drive_log", live_data.drive_summary(rows, max_rows=18, max_chars=2800))
        self.session.set("drive_log_short", live_data.drive_summary(rows, max_rows=6, max_chars=1100))
        self._show_drive_line()

    def _show_drive_line(self):
        log = self.session.get("drive_log")
        self.lbl_drive.setText(("Test drive: " + log.split("\n")[0].split(".")[0] + ". "
                                + (log.split("\n")[1] if "\n" in log else "")) if log else "")

    def _drive_finished(self):
        self.drive_worker = None
        self.b_drive.setEnabled(True)
        self.b_drive.setText(f"TEST DRIVE (every {CFG.get('drive_every_s', 30)} s)")
        self.b_drive.setStyleSheet("")
        self.b_obd.setEnabled(True)
        n = getattr(self, "drive_n", 0)
        if n:
            self._build_drive_log()
            self._store_case()
            self._say(self.txt_intake, f"[✓] Test drive saved: {n} readings. Brainstorm, debate, wildcard and the "
                      "verdict all see the whole drive.", T.OK)
        else:
            self._say(self.txt_intake, "[!] Test drive stopped with no readings (no answer from the car).", T.AMBER)

    def act_save_case(self):
        case = self._store_case()
        self._say(self.txt_intake, "[✓] Case saved.", T.OK)
        w = getattr(self, "case_worker", None)
        if w is not None and w.isRunning():
            return
        self._say(self.txt_intake, "[*] Looking up what the manual has for it...", T.MUTED)
        # this goes on the screen, not into a model, so it gets the whole procedure (config intake_show_chars)
        self.case_worker = CaseContextWorker(self.retriever, case, CFG.get("intake_show_chars", 16000))
        self.case_worker.result.connect(lambda ctx: self._say(self.txt_intake, ctx or "(nothing matched)"))
        self.case_worker.start()

    # One case's fields: cleared by a new case, saved with each visit, restored by LOAD LAST CASE / PAST VISITS.
    # Anything Session.case_summary() reads must be here or in CAR_KEYS (kb/tests/test_gui_pure.py checks).
    CASE_KEYS = ("dtcs", "telemetry", "telemetry_source", "mode06", "past_readings", "symptoms", "ruled_out",
                 "suspect", "history", "intake_note", "intake_summary", "final_verdict", "saved_debate",
                 "saved_wildcard", "drive_log", "drive_log_short", "chart_reads", "chat_notes", "drive_id",
                 "maint_selected", "maint_status")
    CAR_KEYS = ("vehicle", "vehicle_info", "earlier_visits")   # the car: kept across its cases

    def _clear_case(self):
        """Empty the case fields (the car stays). The old case is kept for LOAD LAST CASE."""
        old = {k: self.session.get(k) for k in self.CASE_KEYS}
        if any(old.values()):
            self.session.set("prev_case", json.dumps(dict(old, visit_id=self.session.get("visit_id"))))
        for k in self.CASE_KEYS:
            self.session.set(k, "")
        self.session.set("visit_id", "")   # the next case gets its own history row

    def act_load_last(self):
        try:
            old = json.loads(self.session.get("prev_case") or "{}")
        except ValueError:
            old = {}
        if not old:
            self._say(self.txt_intake, "[!] No earlier case saved.", T.AMBER)
            return
        self._clear_case()  # what's on screen now becomes the "last case"
        for k in self.CASE_KEYS:
            self.session.set(k, old.get(k, ""))
        self.session.set("visit_id", old.get("visit_id", ""))
        self._load_case()
        self.chat_started = False
        self.txt_intake.clear()
        self._say(self.txt_intake, "[*] Last case loaded.", T.MUTED)
        self.last_case = self._store_case()

    def act_new_case(self):
        self._clear_case()
        self._load_case()
        self.last_debate = ""
        self.last_wildcard = ""
        self.chat_started = False
        for t in (self.txt_chat, self.txt_verdict, self.txt_sop, self.txt_parts):
            t.clear()
        self._reset_verdict_tabs()
        self.txt_intake.clear()
        self._say(self.txt_intake, "[*] New case.", T.MUTED)
        self.last_case = self._store_case()

    # ------------------------------------------------------------------ 2. chat (fast model)
    def _chat_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt_chat = QTextEdit()
        self.txt_chat.setReadOnly(True)
        lay.addWidget(self.txt_chat)
        row = QHBoxLayout()
        self.in_chat = QLineEdit()
        self.in_chat.setPlaceholderText("Ask about the case (it already knows the Intake). /torque <part> and /fsm <words> "
                                        "still work typed")
        self.in_chat.returnPressed.connect(self.act_chat)
        b = QPushButton("SEND")
        b.clicked.connect(self.act_chat)
        row.addWidget(self.in_chat)
        row.addWidget(b)
        lay.addLayout(row)
        acts = ReflowRow()
        for label, tip, fn in [   # 10-24: TORQUE and MANUAL buttons dropped (typed /torque, /fsm still work)
            ("COMMON ISSUES", "What this model is known for and at what mileage (NHTSA, recalls, TSBs, forums). "
             "Good for a pre-purchase call", lambda: self._chat_cmd("/common")),
            ("CHARTS", "The manual's diagnostic chart pictures for the case's codes (or codes typed above), read "
             "step by step by the PC's vision model", lambda: self._chat_cmd("/chart")),
            ("DEBATE", "Weighs the competing explanations for the case, or for the question typed above (or end your "
             "text with /debate): evidence for and against each, and the test that settles it",
             lambda: self._chat_cmd("/debate")),
            ("SAVE DEBATE", "Add the last debate to the case, so the verdict sees it", lambda: self._chat_cmd("/save")),
            ("WILDCARD", "Dr. House: 5 unlikely but possible causes", lambda: self._chat_cmd("/wildcard")),
            ("SAVE WILDCARD", "Add the last wildcard ideas to the case, marked as long shots",
             lambda: self._chat_cmd("/savewild")),
            ("CLEAR", "Clear the chat and start it over with the case", self.act_chat_clear),
        ]:
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.clicked.connect(fn)
            acts.add(btn)
        lay.addWidget(acts)
        self.stack.addWidget(w)

    def _chat_cmd(self, cmd):
        arg = self.in_chat.text().strip()
        if cmd in ("/torque", "/fsm") and not arg:
            self._say(self.txt_chat, f"Type a part or words first, then tap {'TORQUE' if cmd == '/torque' else 'MANUAL'}.",
                      T.AMBER)
            return
        self.in_chat.setText(f"{cmd} {arg}".strip())
        self.act_chat()

    CHAT_NOTES_MAX = 4000

    def _note_chat(self, msg):
        """Everything the mechanic types in chat goes on the case's notes list (filed with the visit), so Final
        Diagnosis sees what he replaced, checked and suspects. He can edit the list on the Final Diagnosis screen."""
        line = " ".join(msg.split())
        if not line:
            return
        notes = (self.session.get("chat_notes") or "") + "\n- " + line
        self.session.set("chat_notes", notes.strip()[-self.CHAT_NOTES_MAX:])

    def act_chat_clear(self, keep_screen=False):
        if not keep_screen:
            self.txt_chat.clear()
        self.chat_started = False
        self.chat_hist = []

    def act_chat(self):
        msg = self.in_chat.text().strip()
        if not msg or self._busy():
            return
        self.in_chat.clear()
        self._say(self.txt_chat, msg, T.FOREST_TEXT, who="You")

        # Slash commands never write to the session DB; only /save and /savewild do, on purpose.
        low = msg.lower()
        m = re.match(r"(?s)(.*\S)\s+(/debate|/wildcard)$", msg.strip(), re.I)
        if m and not m.group(1).startswith("/"):   # "is it the coil or the injector? /debate": the text is the topic
            self._note_chat(m.group(1))
            msg = f"{m.group(2).lower()} {m.group(1)}"
            low = msg.lower()
        bare = {"/torque": "/torque <part>", "/spec": "/spec <part>", "/fsm": "/fsm <words>",
                "/part": "/part <name or OEM number>", "/q": "/q <question>", "/question": "/question <question>",
                "/fluids": "/fluids <system, e.g. engine oil>", "/fluid": "/fluid <system>"}
        if low in bare:
            self._say(self.txt_chat, f"  usage: {bare[low]}", T.AMBER)
            return
        if low.startswith(("/fluids ", "/fluid ")):  # no fluids table yet: closest manual sections, never the model
            hits = fsm_hits(self.retriever, msg.split(" ", 1)[1] + " fluid capacity specification", k=2, chars=700)
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no manual match")
            return
        if low.startswith(("/torque ", "/spec ")):
            term = re.sub(r"\b(bolts?|nuts?|torque|specs?|for|the)\b", " ", msg.split(" ", 1)[1], flags=re.I)
            term = term.strip() or msg.split(" ", 1)[1]
            rows = focus_specs(term, self.retriever.specs(term, limit=30))[:10] or self.retriever.specs(term)
            self._say(self.txt_chat, "\n".join("  • " + r for r in rows) or "  no spec found", T.OK)
            return
        if low.startswith("/fsm "):
            hits = fsm_hits(self.retriever, msg.split(" ", 1)[1], k=3, chars=700)
            if hits:
                self._say_hits(self.txt_chat, hits)
            else:
                self._say(self.txt_chat, "  no manual match")
            return
        if low.startswith("/part "):
            self._say(self.txt_chat, self.part_text(msg.split(" ", 1)[1].strip()), T.OK)
            return
        if low == "/debate" or low.startswith("/debate "):
            self.act_debate(msg[len("/debate"):].strip())
            return
        if low == "/save":
            if not self.last_debate:
                self._say(self.txt_chat, "  nothing to save: run DEBATE first", T.AMBER)
            else:
                self.session.set("saved_debate", self.last_debate)
                self._say(self.txt_chat, "  [✓] debate saved to the case; the verdict will see it", T.OK)
            return
        if low == "/savewild":
            if not getattr(self, "last_wildcard", ""):
                self._say(self.txt_chat, "  nothing to save: run WILDCARD first", T.AMBER)
            else:
                self.session.set("saved_wildcard", self.last_wildcard)
                self._say(self.txt_chat, "  [✓] wildcard ideas saved to the case as long shots; the verdict will "
                          "weigh them", T.OK)
            return
        if low in ("/chart", "/charts") or low.startswith(("/chart ", "/charts ")):
            self.act_charts(msg.split(" ", 1)[1] if " " in msg else "")
            return
        if low.startswith(("/maintenance", "/maint", "/addmaint")):   # 10-25: a screen, not chat commands
            if "severe" in low:
                self.cmb_maint_kind.setCurrentIndex(1)
            self.switch(MAINT_PAGE)
            return
        if low == "/common" or low.startswith("/common "):
            self.act_common()
            return
        if low == "/wildcard" or low.startswith("/wildcard "):
            self.act_wildcard(msg[len("/wildcard"):].strip())
            return
        if low.startswith(("/question ", "/q ")):
            q = msg.split(" ", 1)[1]
            self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
            self._run("fast", f"Answer briefly and factually. No speculation.\n\nQUESTION: {q}", self.txt_chat,
                      lambda t: None, keep_history=False, max_new_tokens=400)
            return
        if low.startswith("/"):
            self._say(self.txt_chat, "  commands: /maintenance (opens the MAINTENANCE screen), /chart [codes], /common, /torque <part>, /fsm <words>, /fluids <system>, /part <name or number>, /debate [q] (or end your text with /debate), /save, /wildcard [q], /savewild, /q <text>",
                      T.AMBER)
            return

        self.session.log("user", msg)
        self._record("chat", sender="user", message=msg)
        self._note_chat(msg)
        if not self.chat_started:  # a new case or CLEAR started the chat over
            self.chat_hist = []
            self.chat_started = True
        # Every turn is a fresh prompt: the case, manual text found for the case AND the question, and the
        # last few exchanges. The model's own memory isn't used, so it never fills up and resets, and a vague
        # follow-up ("explain deeper") can't pull in unrelated manual pages (it once answered about bumpers).
        anchor = " ".join(x for x in (self.session.get("dtcs"), self.session.get("symptoms")) if x)
        recent = " ".join(u for u, _ in self.chat_hist[-2:])
        # the codes in the query pull in the manual's diagnostic procedure first; then only owner-confirmed fixes
        # (11-01: manual-biased, plus verified fix threads; unchecked threads stay out of chat)

        def go(ctx):
            hist, used = [], 0
            for u, d in reversed(self.chat_hist):
                turn = f"Mechanic: {u}\nYou: {d}"
                if used + len(turn) > 1600:
                    break
                hist.insert(0, turn)
                used += len(turn)
            dc = self._data_check()
            if dc and dc != getattr(self, "_shown_dc", ""):     # shown once per change, not every turn
                self._shown_dc = dc
                self._say(self.txt_chat, dc, T.AMBER)
            prompt = ((f"{dc}\n\n" if dc else "") +
                      f"CASE:\n{self.session.case_summary()}\n\nFSM DATA (the [MANUAL DIAGNOSTIC PROCEDURE] and [DTC] "
                      "entries are the manual's diagnostic tree for this car's codes: use it as your guide to where the "
                      "mechanic is and what to check next, from what's already been checked. Manual text comes first "
                      "and wins; [REPORTED CAUSES] and [COMMUNITY REPORTS] are fixes owners confirmed on real cars, "
                      "a secondary hint, never a spec or a step):\n"
                      f"{ctx or '(none)'}\n\n"
                      + ("CONVERSATION SO FAR:\n" + "\n".join(hist) + "\n\n" if hist else "")
                      + f"Mechanic: {msg}\n"
                      "(If the mechanic puts forward a cause of their own, weigh it on the evidence: what in the case "
                      "fits it, what doesn't, and the quickest test that would confirm or clear it, from the manual's "
                      "pages for that part when there are any. Not being on the diagnostic tree is no reason to set it "
                      "aside: the tree only covers what the computer can detect. Don't agree just because they said it "
                      "either.)\n" + CHAT_STYLE)
            # 10-19 18:07Z: chat on the fast model, fed only manual text; config "chat_model": "deep" for the 4B
            role = "deep" if CFG.get("chat_model", "fast") == "deep" and "deep" not in CFG.get("stand_in", {}) else "fast"
            if self.mgr.home and not self.mgr.pc_why:
                role = "fast"   # on the PC the fast model is already big; the deep one is kept for verdicts/repairs

            def done(t):
                self.session.log(role, t)
                self._record("chat", sender=role, message=t)
                if "[LLM error" not in t:
                    self.chat_hist.append((msg, t.strip()[:600]))

            self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
            self._run(role, prompt, self.txt_chat, done, keep_history=False, max_new_tokens=300)

        self._lookup(f"{anchor} {recent} {msg}".strip(), 5000, go, scraped=True, verified_only=True)

    def act_debate(self, question):
        """The competing explanations weighed against the same evidence (no role-played sides; 10-25),
        on the fast model. One-off: not added to chat memory."""
        case = self.session.case_summary()
        asked = bool(question)
        question = question or "What is causing the symptoms in this case?"

        def go(ctx):
            prompt = (f"CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n" +
                      (f"THE MECHANIC'S QUESTION (debate exactly this, using the case as evidence): {question}\n\n"
                       if asked else f"QUESTION: {question}\n\n") +
                      "Weigh the competing explanations against each other. If the question names ideas, those are "
                      "the explanations; otherwise use the 2 or 3 the evidence supports best. No characters or "
                      "personas: argue the ideas themselves, using only the case, live data and manual text above, "
                      "and quote the actual numbers.\n"
                      "For each explanation: IDEA (one line), FOR (the evidence that fits it), AGAINST (evidence that "
                      "doesn't fit, including anything listed as verified good), COST (rough effort or parts if it's "
                      "right).\n"
                      "Then WHAT SETTLES IT: the one test that tells them apart, and what each result means.\n"
                      "Do not pick a winner. Keep each explanation under 100 words.")
            self._say(self.txt_chat, "", T.AMBER, who="Debate")
            self._run("fast", prompt, self.txt_chat, self._debate_done, keep_history=False, max_new_tokens=900)

        self._lookup(case + " " + question, 4500, go)

    def _rejected_causes(self):
        """Causes the baked card's source check threw out, for this case's codes and car (and why).
        Read fresh from the kb every time: a rebake replaces the cards. Only /wildcard uses these."""
        codes = list(dict.fromkeys(re.findall(r"\b[PBCU]\d{4}\b", self.session.get("dtcs") or "")))
        kb = CFG.get("kb_db") or ""
        if not codes or not os.path.isfile(kb):
            return []
        sql = "SELECT code, dropped_json FROM dtc_card WHERE code = ?"
        args_car = []
        if CFG.get("kb_model"):
            sql += " AND model = ?"
            args_car.append(CFG["kb_model"])
        if CFG.get("kb_year"):
            sql += " AND ? BETWEEN year_min AND year_max"
            args_car.append(CFG["kb_year"])
        out = []
        try:
            with sqlite3.connect(f"file:{kb}?mode=ro", uri=True) as db:
                for code in dict.fromkeys(codes):
                    for c, dropped in db.execute(sql, [code] + args_car):
                        try:
                            items = json.loads(dropped or "[]")
                        except ValueError:
                            continue
                        for d in items:
                            if isinstance(d, dict) and d.get("cause"):
                                line = f"{c}: {d['cause']} (rejected: {d.get('reason') or 'no reason given'})"
                                if line not in out:
                                    out.append(line)
        except sqlite3.Error:  # no card table (kb built before cards), or mid-copy
            return []
        return out[:15]

    def act_charts(self, arg=""):
        """CHARM's diagnostic pages for the codes: text and pictures now, then the PC's reading of each picture
        as it lands (kept in the case for the PC models). Python picks the pages; the model only reads them."""
        codes = list(dict.fromkeys(re.findall(r"\b[PBCU]\d{4}\b", (arg or self.bub_dtcs.text()).upper())))[:3]
        r, db = self.retriever, getattr(self.retriever, "db", None)
        if not codes:
            self._say(self.txt_chat, "  No codes: read the car or type one, e.g. /chart P0301.", T.AMBER)
            return
        if db is None or not hasattr(r, "_veh_filter") or not getattr(r, "vehicle_ids", None):
            self._say(self.txt_chat, "  Pick the car (model and year) on Intake first, so the charts are this car's.",
                      T.AMBER)
            return
        vf, vargs = r._veh_filter("s.id")
        names, done = [], 0
        for code in codes:
            try:
                rows = db.execute(
                    "SELECT s.path, s.body, s.images FROM dtc_procedure p JOIN section s ON s.id=p.section_id "
                    f"JOIN doc d ON d.id=s.doc_id WHERE d.kind='charm' AND p.code=?{vf} GROUP BY s.id ORDER BY s.id "
                    "LIMIT 2", [code] + vargs).fetchall()
            except sqlite3.Error:
                rows = []
            if not rows:
                self._say(self.txt_chat, f"  {code}: no CHARM diagnostic page for this car.", T.AMBER)
                continue
            for path, body, imgs in rows:
                try:
                    figs = [n for n in json.loads(imgs or "[]") if charm_pics.is_charm(n)]
                except ValueError:
                    figs = []
                figs = figs[:int(CFG.get("charm_pics_max") or 8)]
                self._say_hits(self.txt_chat, [(squeeze(r._fmt(path, None, body, 1200, "charm")), figs)])
                names += [(code, path, n) for n in figs if n not in [x[2] for x in names]]
                done += 1
        if not names:
            if done:
                self._say(self.txt_chat, "  These pages have no pictures to read.", T.MUTED)
            return
        self.chart_meta = {n: (code, path, i + 1) for i, (code, path, n) in enumerate(names)}
        if getattr(self, "chart_worker", None) is not None and self.chart_worker.isRunning():
            self.chart_worker.stop_flag.set()
        self.session.set("chart_reads", "")
        self._say(self.txt_chat, f"  Asking the PC to read {len(names)} chart pictures. Readings show here as they "
                  "come; you can keep working.", T.MUTED)
        w = ChartWorker([n for *_, n in names])
        w.result.connect(lambda n, res, w=w: self._chart_read(n, res) if not w.stop_flag.is_set() else None)
        w.note.connect(lambda t, w=w: self._say(self.txt_chat, "  " + t, T.MUTED) if not w.stop_flag.is_set()
                       else None)
        self.chart_worker = w
        w.start()

    def _chart_read(self, name, res):
        code, path, i = self.chart_meta.get(name, ("", "", 0))
        label = f"{code} picture {i} ({path.split(' > ')[-1] if path else 'CHARM'})"
        state = res.get("state")
        if state == "skipped":
            return   # icons and arrows: nothing to say
        if state == "error":
            self._say(self.txt_chat, f"  {label}: the PC couldn't read it ({res.get('why', '')[:120]}). Tap CHARTS "
                      "to try again.", T.AMBER)
            return
        text = (res.get("text") or "").strip()
        if text.upper().startswith("NOT A CHART"):
            self._say(self.txt_chat, f"  {label}: {text.splitlines()[0][len('NOT A CHART:'):].strip()}", T.MUTED)
            return
        self._say(self.txt_chat, "", T.PURPLE_TEXT, who=f"Chart reading, {label}")
        self._say(self.txt_chat, text)
        bad = res.get("unverified")
        if bad:
            warn = f"Check these numbers on the picture (plain OCR didn't see them): {', '.join(bad[:15])}"
        elif bad is None:
            warn = "Numbers not cross-checked (no plain OCR on the PC for this picture)."
        else:
            warn = "Every number matched plain OCR of the picture."
        self._say(self.txt_chat, "  " + warn + " The picture above is the source.", T.AMBER if bad or bad is None
                  else T.OK)
        reads = self.session.get("chart_reads") or ""
        entry = f"[{code} chart, {path}, picture {i}]\n{text}" + (f"\n(unverified numbers: {', '.join(bad)})" if bad else "")
        self.session.set("chart_reads", (reads + "\n\n" + entry).strip())

    # ------------------------------------------------------------------ maintenance screen
    MAINT_STATES = ("", "done before", "doing today", "declined")
    MAINT_GROUPS = {"past": "SHOULD HAVE BEEN DONE in the last {lb:,} mi (ask the customer)",
                    "next": "DUE NOW: next service at {next:,} mi",
                    "longer": "LONG-INTERVAL JOBS last due before that (ask when they were really done)",
                    "later": "COMING UP at {later}"}

    def _maint_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lbl_maint = QLabel("Factory schedule for the car on Intake (needs model, year and miles).")
        self.lbl_maint.setStyleSheet(f"color: {T.AMBER}; font-weight: bold;")
        self.lbl_maint.setWordWrap(True)
        self.cmb_maint_kind = QComboBox()
        self.cmb_maint_kind.addItems(["Normal service", "Severe service"])
        self.cmb_maint_kind.setToolTip("Severe: towing, dusty roads, short trips, extreme heat or cold")
        self.cmb_maint_kind.currentIndexChanged.connect(lambda _: self._maint_fill())
        b_man = QPushButton("MANUAL FOR THIS")
        b_man.setToolTip("The manual's procedure for the picked row")
        b_man.clicked.connect(self.act_maint_manual)
        top.addWidget(self.lbl_maint, 1)
        top.addWidget(self.cmb_maint_kind)
        top.addWidget(b_man)
        if PS is not None:
            b_parts = QPushButton("PARTS + PRICES FOR THIS")
            b_parts.clicked.connect(self.act_maint_parts)
            top.addWidget(b_parts)
        lay.addLayout(top)
        self.tree_maint = QTreeWidget()
        self.tree_maint.setHeaderLabels(["Item", "Due (mi)", "Status", "Done at (mi, date)"])
        self.tree_maint.setColumnWidth(0, 300)
        self.tree_maint.setColumnWidth(1, 320)
        self.tree_maint.setColumnWidth(2, 150)
        self.tree_maint.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)  # only "Done at", by tapping it
        self.tree_maint.itemClicked.connect(self._maint_clicked)
        self.tree_maint.itemChanged.connect(self._maint_edited)
        lay.addWidget(self.tree_maint, 3)
        self.txt_maint = QTextEdit()
        self.txt_maint.setReadOnly(True)
        lay.addWidget(self.txt_maint, 2)
        self.maint_rows = {}
        self.stack.addWidget(w)

    def _maint_status(self):
        try:
            return json.loads(self.session.get("maint_status") or "{}")
        except ValueError:
            return {}

    def _maint_past_done(self):
        """{item key: 'mi, date' text} for items done before on this car, from its earlier visits (newest wins)."""
        try:
            key = self._file_key(json.loads(self.session.get("vehicle_info") or "{}"))
        except ValueError:
            key = ""
        out = {}
        for _, started, _, case in (self.session.visits(key, exclude=self.session.get("visit_id") or None)
                                    if key else []):
            try:
                st = json.loads(case.get("maint_status") or "{}")
            except ValueError:
                continue
            for k, v in st.items():
                if k not in out and v.get("s") in ("done before", "doing today"):
                    out[k] = v.get("at") or started[:10]
        return out

    def _maint_fill(self):
        """Rebuild the checklist from the factory schedule, the miles on Intake and what's been ticked."""
        self.tree_maint.blockSignals(True)
        self.tree_maint.clear()
        self.maint_rows = {}
        self.tree_maint.blockSignals(False)
        self.txt_maint.clear()
        miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0)
        r, db = self.retriever, getattr(self.retriever, "db", None)
        if db is None or not getattr(r, "vehicle_ids", None):
            self.lbl_maint.setText("Pick the car (model and year) on Intake first.")
            return
        if not miles:
            self.lbl_maint.setText("Type the car's miles on Intake first.")
            return
        severe = self.cmb_maint_kind.currentIndex() == 1
        try:
            sched, _ = maintenance.schedule(db, r.vehicle_ids, severe)
        except sqlite3.Error as e:
            sched = {}
            self._say(self.txt_maint, f"[!] schedule lookup failed: {e}", T.AMBER)
        p = maintenance.plan(sched, miles)
        rows = maintenance.checklist(p)
        if not rows:
            self.lbl_maint.setText("No CHARM service schedule for this car. The manual's own schedule pages are below.")
            hits = fsm_hits(r, "periodic maintenance schedule service intervals", k=2, chars=900)
            if hits:
                self._say_hits(self.txt_maint, hits)
            return
        status = self._maint_status()
        past = self._maint_past_done()
        for k, at in past.items():     # done on an earlier visit: already ticked, with when
            if k not in status:
                status[k] = {"s": "done before", "at": at, "earlier": True}
        self.session.set("maint_status", json.dumps(status) if status else "")
        kind = "Severe" if severe else "Normal"
        self.lbl_maint.setText(f"{kind} Service schedule (CHARM) at {miles:,} mi. Tap Status to tick each item; "
                               "tap Done at to type when it was done." + (
                                   f" (Schedule pages stop at {p['last_mi']:,} mi; later points repeat each item's "
                                   "spacing.)" if p["extrapolated"] else ""))
        self.tree_maint.blockSignals(True)
        heads = {}
        for g, e in rows:
            if g not in heads:
                title = self.MAINT_GROUPS[g].format(lb=maintenance.LOOKBACK, next=p["next_mi"],
                                                    later=" and ".join(f"{m:,}" for m in p["later_mi"]) + " mi")
                h = QTreeWidgetItem([title])
                h.setFirstColumnSpanned(True)
                h.setFlags(Qt.ItemFlag.ItemIsEnabled)
                h.setForeground(0, QColor(T.TEXT))
                self.tree_maint.addTopLevelItem(h)
                h.setFirstColumnSpanned(True)
                heads[g] = h
            st = status.get(e["key"], {})
            it = QTreeWidgetItem([f"{e['action']} {e['item']}", e["due_text"], "", st.get("at", "")])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            it.setData(0, Qt.ItemDataRole.UserRole, e["key"])
            if e["note"]:
                it.setToolTip(0, e["note"])
            heads[g].addChild(it)
            cmb = QComboBox()
            cmb.addItems(["—"] + list(self.MAINT_STATES[1:]))
            cmb.setCurrentIndex(self.MAINT_STATES.index(st.get("s", "")) if st.get("s", "") in self.MAINT_STATES else 0)
            cmb.currentIndexChanged.connect(lambda i, k=e["key"]: self._maint_set(k, self.MAINT_STATES[i]))
            self.tree_maint.setItemWidget(it, 2, cmb)
            self.maint_rows[e["key"]] = (it, e)
            self._maint_paint(e["key"])
        self.tree_maint.expandAll()
        self.tree_maint.blockSignals(False)
        self._maint_rollup()

    def _maint_paint(self, key):
        """Colour a row by its status, and show when it comes due again after a typed 'done at' mileage."""
        it, e = self.maint_rows[key]
        st = self._maint_status().get(key, {})
        colour = {"done before": T.OK, "doing today": T.AMBER, "declined": T.MUTED}.get(st.get("s"), T.TEXT)
        due = e["due_text"]
        done_mi = self._maint_miles(st.get("at", ""))
        if st.get("s") in ("done before", "doing today") and done_mi:
            again = maintenance.next_after(e, done_mi)
            if again:
                due = f"done {done_mi:,}; again at {again:,}"
        self.tree_maint.blockSignals(True)    # colours and texts set here are not the mechanic's edits
        for c in (0, 1, 3):
            it.setForeground(c, QColor(colour))
        it.setText(1, due)
        it.setText(3, st.get("at", ""))
        self.tree_maint.blockSignals(False)

    @staticmethod
    def _maint_miles(text):
        """The mileage in a typed 'done at' ('98,000 mi, 2019', '98000', '98k'), never a year; None if there's none."""
        m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", text or "", re.I)
        if m:
            return int(float(m.group(1)) * 1000)
        m = re.search(r"\b(\d{1,3}(?:,\d{3})+|\d{5,7})\b", text or "")
        return int(m.group(1).replace(",", "")) if m else None

    def _maint_clicked(self, it, col):
        if col == 3 and it.data(0, Qt.ItemDataRole.UserRole):
            self.tree_maint.editItem(it, 3)
        key = it.data(0, Qt.ItemDataRole.UserRole)
        if key and key in self.maint_rows and self.maint_rows[key][1]["note"]:
            self.lbl_voice.setText(self.maint_rows[key][1]["note"][:160])

    def _maint_edited(self, it, col):
        key = it.data(0, Qt.ItemDataRole.UserRole)
        if col != 3 or not key:
            return
        status = self._maint_status()
        st = status.setdefault(key, {"s": ""})
        st["at"] = it.text(3).strip()
        st.pop("earlier", None)
        if st["at"] and not st["s"]:
            st["s"] = "done before"
            w = self.tree_maint.itemWidget(it, 2)
            w.blockSignals(True)
            w.setCurrentIndex(self.MAINT_STATES.index("done before"))
            w.blockSignals(False)
        self._maint_save(status)
        self._maint_paint(key)

    def _maint_set(self, key, state):
        status = self._maint_status()
        st = status.setdefault(key, {})
        st["s"] = state
        st.pop("earlier", None)
        if state == "doing today":
            miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0)
            st["at"] = f"{miles:,} mi, {time.strftime('%Y-%m-%d')}" if miles else time.strftime("%Y-%m-%d")
        elif not state:
            st.pop("at", None)
        if not state and not st.get("at"):
            status.pop(key, None)
        self._maint_save(status)
        self._maint_paint(key)
        self._maint_rollup()

    def _maint_save(self, status):
        """Keep the ticks with the visit; today's jobs go to Final Diagnosis (as before with /addmaint)."""
        self.session.set("maint_status", json.dumps(status) if status else "")
        today = [f"{self.maint_rows[k][1]['action']} {self.maint_rows[k][1]['item']} (factory schedule, "
                 f"{self.maint_rows[k][1]['due_text']})" for k, v in status.items()
                 if v.get("s") == "doing today" and k in self.maint_rows]
        self.session.set("maint_selected", "\n".join(today))
        self._file_visit()

    def _maint_rollup(self):
        """Today's jobs with the CHARM book hours and part numbers for this car."""
        status = self._maint_status()
        today = [self.maint_rows[k][1] for k, v in status.items() if v.get("s") == "doing today" and k in self.maint_rows]
        self.txt_maint.clear()
        if not today:
            self._say(self.txt_maint, "Jobs marked 'doing today' are listed here with book hours and part numbers, "
                      "and go into Final Diagnosis.", T.MUTED)
            return
        cache = getattr(self, "maint_cache", {})
        self.maint_cache = cache
        cfg = None
        if PS is not None:
            try:
                cfg = self._ps_cfg()
                cfg = cfg if cfg["kb_ok"] else None
            except Exception as e:
                log_caught("maintenance parts settings", e)
                cfg = None
        lines, total = [], 0.0
        for e in today:
            name = e["item"]
            ck = (CFG.get("kb_model"), CFG.get("kb_year"), name)
            if ck not in cache and cfg is not None:
                hours = parts = None
                try:
                    from parts_sourcing import junk_quote
                    hours = junk_quote.labor_hours(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])
                except Exception as e:
                    log_caught(f"book hours for {name!r}", e)
                try:
                    parts = PS.parts_for_job(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])[:3]
                except Exception as e:
                    log_caught(f"part numbers for {name!r}", e)
                cache[ck] = (hours, parts or [])
            hours, parts = cache.get(ck, (None, []))
            if hours and hours.get("hours"):
                total += float(hours["hours"])
            lines.append(f"{e['action']} {name}: " + (f"{float(hours['hours']):g} h book time" if hours and
                                                      hours.get("hours") else "no book time found")
                         + ("\n      parts: " + "; ".join(f"{pn} {op.split(' > ')[-1]}" + (f" ({price})" if price else "")
                                                          for pn, op, price in parts) if parts else ""))
        self._say(self.txt_maint, f"DOING TODAY ({len(today)} job{'s' if len(today) != 1 else ''}, {total:g} h book time found):\n"
                  + "\n".join("  " + l for l in lines)
                  + "\n(Book hours and part numbers are CHARM's for this car; check each part number before ordering.)",
                  T.AMBER)

    def _maint_picked(self):
        it = self.tree_maint.currentItem()
        key = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        return self.maint_rows.get(key, (None, None))[1] if key else None

    def act_maint_manual(self):
        e = self._maint_picked()
        if e is None:
            self.lbl_voice.setText("Tap a maintenance row first")
            return
        hits = fsm_hits(self.retriever, f"{e['action']} {e['item']} procedure", k=2, chars=1200)
        self.txt_maint.clear()
        self._say(self.txt_maint, f"{e['action']} {e['item']}" + (f": {e['note']}" if e["note"] else ""), T.TEXT)
        if hits:
            self._say_hits(self.txt_maint, hits)
        else:
            self._say(self.txt_maint, "No manual match for this item.", T.AMBER)

    def act_maint_parts(self):
        e = self._maint_picked()
        if e is None:
            self.lbl_voice.setText("Tap a maintenance row first")
            return
        self.in_part.setText(e["item"])
        self.act_find_parts()
        self.switch(4)

    def act_common(self):
        """Top known problems for the car on Intake, and at what mileage. Python counts; the model only words it."""
        model, year = CFG.get("kb_model"), CFG.get("kb_year")
        miles = int(re.sub(r"\D", "", self.in_miles.text()) or 0) or None
        db = getattr(self.retriever, "db", None)
        path = os.path.expanduser(CFG.get("kb_db") or "")
        if not os.path.isfile(path) or db is None:
            self._say(self.txt_chat, "  This car's database isn't loaded, so there's nothing to count.", T.AMBER)
            return
        aliases = getattr(sys.modules.get(type(self.retriever).__module__), "MODEL_ALIASES", {})
        r = common_issues.gather(path, model, year, miles, aliases)
        if r.get("error"):
            self._say(self.txt_chat, "  " + r["error"], T.AMBER)
            return
        facts = "\n".join(r["lines"])
        self._say(self.txt_chat, f"Counts for {r['scope']} (from the database):\n{facts}", T.OK, who="Deck")
        car = " ".join(x for x in (str(year or ""), CFG.get("make") or "", model) if x)
        prompt = common_issues.PROMPT.format(car=car, mi=f" with {miles:,} miles" if miles else "",
                                             scope=r["scope"], facts=facts)
        role = "fast"
        self._say(self.txt_chat, "", T.FOREST_TEXT, who="Deck")
        self._run(role, prompt, self.txt_chat, lambda t: None, keep_history=False, max_new_tokens=600)

    def act_wildcard(self, question):
        """Dr. House mode: 5 unlikely-but-possible causes, high temperature. SAVE WILDCARD keeps them as long shots."""
        case = self.session.case_summary()
        q = question or "What else could this be?"
        forum = getattr(self.retriever, "knowledge", lambda *a, **k: [])(case + " " + q, k=3)
        rejected = self._rejected_causes()
        prompt = (f"CASE:\n{case}\n\nFORUM FIXES (other owners, unverified):\n" + ("\n\n".join(forum) or "(none)") +
                  ("\n\nCAUSES REJECTED BY THE CARD'S SOURCE CHECK (and why; unproven, may still be worth a "
                   "thought):\n" + "\n".join(rejected) if rejected else "") +
                  f"\n\nQUESTION: {q}\n\n"
                  "You are a brilliant, contrarian diagnostician in the style of Dr. House. Ignore the obvious answer. "
                  "Give exactly 5 unlikely but physically possible causes that fit these symptoms and numbers, "
                  "including odd interactions and things a tired mechanic would miss. For each: the cause in one "
                  "line, the clue in the case that points to it, and a cheap test to rule it in or out. "
                  "Never contradict anything listed as verified good.")
        self._say(self.txt_chat, "", T.PURPLE_TEXT, who="Wildcard (speculative)")
        # Runs on the deep model: the small one drifts into nonsense at high temperature.
        def done(t):
            if "[LLM error" not in t:
                self.last_wildcard = t.strip()
            self._say(self.txt_chat, "  (not saved: tap SAVE WILDCARD to add these to the case as long shots)",
                      T.MUTED)
        self._run("deep", prompt, self.txt_chat, done, keep_history=False, max_new_tokens=700, temperature=0.85)

    def _debate_done(self, text):
        self.last_debate = text
        self._say(self.txt_chat, "  (not saved: tap SAVE DEBATE to add it to the case)", T.MUTED)

    # ------------------------------------------------------------------ 3. verdict (deep model)
    def _verdict_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = ReflowRow()   # wraps on a narrow (portrait) window
        b = QPushButton("SYNTHESIZE ALL EVIDENCE && BUILD VERDICT")
        b.setStyleSheet(f"background-color: {T.AMBER}; color: {T.BG};")
        b.clicked.connect(self.act_verdict)
        row.add(b)
        self.chk_think = QCheckBox("THINK HARDER (slower)")
        if self.mgr.backend == "rkllm" and "think" in CFG.get("stand_in", {}):
            # no Thinking 4B: the 1.7B thinks instead (it gave usable verdicts on the deck); say so
            self.chk_think.setText("THINK HARDER (1.7B thinking)")
            self.chk_think.setToolTip("The Qwen3-4B-Thinking .rkllm isn't in models/rkllm/ yet, so the 1.7B thinks instead")
        row.add(self.chk_think)
        self.b_to_sop = QPushButton("REPAIR GUIDE →")
        self.b_to_sop.setToolTip("Write the repair procedure for this verdict")
        self.b_to_sop.clicked.connect(lambda: (self.switch(3), self.act_sop()))
        row.add(self.b_to_sop)
        b_parts = QPushButton("PARTS →")
        b_parts.setToolTip("Part numbers, prices and QR codes for the repair in this verdict")
        b_parts.clicked.connect(lambda: self.switch(4))
        row.add(b_parts)
        lay.addWidget(row)
        notes_lbl = QLabel("YOUR NOTES FROM CHAT (sent to Final Diagnosis; delete any line that's wrong):")
        notes_lbl.setWordWrap(True)
        lay.addWidget(notes_lbl)
        self.txt_notes = QPlainTextEdit()
        self.txt_notes.setMaximumHeight(110)
        self.txt_notes.setPlaceholderText("What you type in chat shows up here: parts you replaced, things you checked, "
                                          "your theories. You can also type here.")
        lay.addWidget(self.txt_notes)
        # 10-25: one tab per issue (up to 5), cheapest/likeliest first, each with its own test, parts and
        # manual pages; the first tab is the summary with every part to order, the last the whole answer as written
        self.tabs_verdict = QTabWidget()
        self.txt_verdict = QTextEdit()
        self.txt_verdict.setReadOnly(True)
        self.tabs_verdict.addTab(self.txt_verdict, "WORKING")
        lay.addWidget(self.tabs_verdict, 1)
        self.stack.addWidget(w)

    VERDICT_FIELDS = ("WHY HERE", "FOR", "AGAINST", "TEST", "REPAIR", "PARTS")

    def _data_check(self):
        """The DATA CHECK block (app/sanity_check.py) for the top of the prompts; '' when the case data shows no
        problem. Never stops a request: a failure here just means no block."""
        try:
            case = {k: self.session.get(k) for k in ("dtcs", "telemetry", "symptoms", "history", "ruled_out",
                                                     "suspect", "intake_note")}
            case["dtcs_raw"] = self.bub_dtcs.text()
            ref = sanity_check.find_ref_db(CFG.get("kb_db"), CFG.get("make"))
            return sanity_check.block(sanity_check.check(case, self.retriever, ref))
        except Exception as e:
            print(f"data check failed: {type(e).__name__}: {e}", file=sys.stderr)
            return ""

    def act_verdict(self):
        if self._busy():
            return
        self.session.set("chat_notes", self.txt_notes.toPlainText().strip())   # their edits to the list win
        case = self.session.case_summary()
        on_pc = self.mgr.home and not self.mgr.pc_why
        most = 5 if on_pc else 3            # the deck's 4096-token models can't write 5 full issues
        maint = self.session.get("maint_selected")
        dc = self._data_check()
        top = f"{dc}\n\n" if dc else ""
        # Blind first pass (config "verdict_blind_first", default on): pass 1 sees the evidence only, without the
        # OPINION_FIELDS; pass 2 gets its ranking plus the opinions and says whether the evidence supports them.
        opinions = self.session.opinions()
        blind = bool(CFG.get("verdict_blind_first", True) and opinions)
        case1 = self.session.case_summary(exclude=[k for k, _ in OPINION_FIELDS]) if blind else case

        def go(ctx):
            prompt = (f"{top}CASE:\n{case1}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"
                      "Write a ranked work plan for this car. Cover every code in the case.\n"
                      f"- Up to {most} ISSUES, only as many as the evidence supports; one is right when one cause "
                      "explains everything. Separate faults (or a second part that also has to be bought) are separate "
                      "issues.\n"
                      "- Order: the likeliest and cheapest/easiest to confirm first. Say why each one sits where it does.\n"
                      "- Every idea the mechanic states in their notes gets its own issue with the evidence for and "
                      "against it, even if you rank it low.\n"
                      "- A part he says he replaced or checked is never issue 1. List it last, as a recheck, only if "
                      "the evidence still points at it, and say why.\n"
                      "- Use only the case, live data and manual text above, and quote the real numbers. Never write "
                      "part numbers or prices: the deck adds them from its database.\n\n"
                      "Write exactly this format:\n"
                      "SUMMARY: <2-3 sentences: how many problems, and the order to work in>\n"
                      "=== ISSUE 1: <short name> ===\n"
                      "WHY HERE: <why it's ranked here>\n"
                      "FOR: <evidence that fits>\n"
                      "AGAINST: <evidence that doesn't fit, or none>\n"
                      "TEST: <the test that confirms or clears it, and what each result means>\n"
                      "REPAIR: <the job if confirmed, in a few words, e.g. replace front brake pads>\n"
                      "PARTS: <part names only, comma separated, e.g. exhaust manifold gasket, spark plugs; "
                      "no actions or steps; or none>\n"
                      "=== ISSUE 2: <short name> ===\n(same fields)\n"
                      + ("=== ALSO DO: maintenance ===\nREPAIR: <the approved maintenance jobs>\nPARTS: <part names>\n"
                         if maint else "") +
                      "Keep each issue under 120 words.")
            fmt = prompt[prompt.index("Write exactly this format:"):]
            # pass 2: the same case with the opinions, the first-pass plan, a short job (no re-ranking on opinion)
            full = f"{top}CASE:\n{case}\n\nFSM DATA:\n{ctx or '(none)'}\n\n"

            def pass2_prompt(plan):
                return (full + "YOUR FIRST-PASS PLAN (written from the evidence only, before the opinions below were "
                        "shown):\n" + plan.strip() + "\n\nOPINIONS LEFT OUT OF THE FIRST PASS (guesses and earlier "
                        "diagnoses, not test results):\n" + opinions + "\n\n"
                        "For each opinion, say whether the evidence in the CASE and FSM DATA supports it, and the test "
                        "that would settle it. Keep the first-pass ranking. Move an issue only when evidence (a code, a "
                        "reading, a test result or a manual page) justifies it, never because someone suspects it, and "
                        "name that evidence. An opinion the plan doesn't cover gets its own issue, ranked by its "
                        "evidence. Then write the final plan.\n"
                        "Start with one line per opinion:\nOPINION CHECK: <opinion> - <supported / not supported / "
                        "untested>: <the evidence, and the test>\nthen " + fmt[0].lower() + fmt[1:])

            def pass1_done(plan):
                if "[LLM error" in plan or not plan.strip():
                    return
                self._blind_pass1 = plan.strip()
                self.txt_verdict.clear()
                self._say(self.txt_verdict, "[*] Pass 1 (evidence only) done. Pass 2: weighing the left-out opinions "
                          "against the evidence...", T.MUTED)
                w = self.worker
                go2 = lambda: self._run("deep", pass2_prompt(plan), self.txt_verdict, self._verdict_done,
                                        max_new_tokens=2200 if on_pc else 900)
                if w is not None and w.isRunning():
                    w.finished.connect(go2)
                else:
                    go2()
            self._reset_verdict_tabs()
            self.txt_verdict.clear()
            if dc:
                self._say(self.txt_verdict, dc, T.AMBER)
            self._say(self.txt_verdict, "[*] Synthesizing all evidence..." + (
                " Pass 1 of 2: evidence only, the Suspect field and other opinions left out." if blind else ""), T.MUTED)
            self._blind_pass1 = ""
            if self.chk_think.isChecked():
                # 11-01: the thinking model writing the plan from scratch did worse than the plain one and
                # looped on doubts. Now THINK HARDER = the plain model writes the plan, then the thinking model
                # reviews it against the case and fixes only what the evidence says is wrong.
                def draft_done(draft):
                    if "[LLM error" in draft or not draft.strip():
                        return
                    self.txt_verdict.clear()
                    self._say(self.txt_verdict, "[*] Draft written. Thinking model is now checking it against the "
                              "evidence (its reasoning streams below; only its final plan is kept)...", T.MUTED)
                    review = ((full + prompt[prompt.index("Write a ranked work plan"):] if blind else prompt)
                              + "\n\nDRAFT PLAN (written from the same data by another technician"
                              + (", before it saw the Suspect field and other opinions" if blind else "")
                              + "):\n" + draft.strip()
                              + "\n\nCheck the draft against the CASE and FSM DATA above. Keep every part of it that the "
                              "evidence supports. Change the ranking, a test or an issue only where the case or the "
                              "manual shows it is wrong or missing something, and say what evidence made you change it. "
                              "Decide once per point and move on; don't reopen a point you have settled. Then write "
                              "the final plan in exactly the format above.")
                    self._draft_verdict = draft.strip()      # kept if the review fails or runs out of room
                    w = self.worker                          # the draft's thread is still finishing: start after it
                    go = lambda: self._run("think", review, self.txt_verdict, self._think_done)
                    if w is not None and w.isRunning():
                        w.finished.connect(go)
                    else:
                        go()
                self._draft_verdict = ""
                self._run("deep", prompt, self.txt_verdict, draft_done)
            else:
                self._run("deep", prompt, self.txt_verdict, pass1_done if blind else self._verdict_done,
                          max_new_tokens=2200 if on_pc else 900)

        self._lookup(case1, 6000 if on_pc else 3500, go)

    def _reset_verdict_tabs(self):
        """Back to the one streaming tab (the issue tabs and summary of the last verdict go)."""
        while self.tabs_verdict.count() > 1:
            self.tabs_verdict.removeTab(1 if self.tabs_verdict.widget(0) is self.txt_verdict else 0)
        self.tabs_verdict.setTabText(0, "WORKING")
        self.tabs_verdict.setCurrentIndex(0)

    @classmethod
    def parse_verdict(cls, text):
        """(summary, [issue dicts]) from the plan format; [] when the model ignored the format."""
        text = re.sub(r"(?s)<think>.*?</think>", "", text)
        blocks = re.split(r"(?m)^[\s#*_]*=+\s*\**\s*((?:ISSUE\s*\d+|ALSO DO)\b[^\n=]*?)\s*\**\s*=+[\s*_]*$", text)
        m = re.search(r"(?is)SUMMARY\s*\**\s*:\s*(.*)", blocks[0])
        summary = (m.group(1) if m else blocks[0]).strip()
        issues = []
        for head, body in zip(blocks[1::2], blocks[2::2]):
            kind, _, name = head.partition(":")
            d = {"head": head.strip(), "name": name.strip(" *") or kind.strip(), "also": kind.upper().startswith("ALSO"),
                 "body": body.strip()}
            rx = r"(?im)^[\s*_-]*(" + "|".join(cls.VERDICT_FIELDS) + r")\s*\**\s*:\s*"
            parts = re.split(rx, body)
            for key, val in zip(parts[1::2], parts[2::2]):
                d[key.upper()] = val.strip()
            issues.append(d)
        return summary, issues

    def _part_rows(self, name):
        """[(part number, description, price)] from the car's database for one part name, [] when none."""
        if PS is None or not name:
            return []
        try:
            cfg = self._ps_cfg()
            if not cfg["kb_ok"]:
                return []
            return PS.parts_for_job(cfg["kb_db"], name, cfg["car"]["model"], cfg["car"]["year"])[:3]
        except Exception as e:
            log_caught(f"part rows for {name!r}", e)
            return []

    def _issue_parts(self, d):
        """Part names for one issue: the PARTS field (any action words the model added are dropped), else the
        part its REPAIR line names."""
        names = [n for p in re.split(r"[,;\n]", d.get("PARTS", "")) if p.strip(" .")
                 and p.strip(" .").lower() not in ("none", "n/a", "-", "none needed") for n in parts_from_job(p.strip(" ."))]
        if not names and d.get("REPAIR"):
            names = parts_from_job(d["REPAIR"])
        return list(dict.fromkeys(n for n in names if n))

    def _build_verdict_tabs(self, text):
        summary, issues = self.parse_verdict(text)
        self.tabs_verdict.setTabText(0, "FULL TEXT")
        if not issues:
            return
        all_parts = []
        for n, d in enumerate(issues, 1):
            t = QTextEdit()
            t.setReadOnly(True)
            box = QWidget()
            bl = QVBoxLayout(box)
            row = QHBoxLayout()
            job = d.get("REPAIR") or d["name"]
            b1 = QPushButton("REPAIR GUIDE FOR THIS")
            b1.clicked.connect(lambda _=False, j=job: (self.bub_repair.set_text(j), self.switch(3), self.act_sop()))
            row.addWidget(b1)
            if PS is not None:
                b2 = QPushButton("PARTS + PRICES FOR THIS")
                pq = (self._issue_parts(d) or [part_from_job(job)])[0]   # a part name, not the repair sentence
                b2.clicked.connect(lambda _=False, j=pq: (self.in_part.setText(j), self.act_find_parts(),
                                                          self.switch(4)))
                row.addWidget(b2)
            row.addStretch(1)
            bl.addLayout(row)
            bl.addWidget(t, 1)
            title = ("ALSO: " if d["also"] else f"{n}. ") + d["name"][:28]
            self.tabs_verdict.insertTab(self.tabs_verdict.count() - 1, box, title)
            for key in self.VERDICT_FIELDS[:-1]:
                if d.get(key):
                    self._say(t, f"<b>{key}:</b> " + html.escape(d[key]).replace("\n", "<br>"), raw=True)
            if not any(d.get(k) for k in self.VERDICT_FIELDS):
                self._say(t, d["body"])
            self._say(t, "=== PARTS (numbers from the database, not the model) ===", T.TEXT)
            for name in self._issue_parts(d) or ([part_from_job(job)] if d["also"] else []):
                rows = self._part_rows(name)
                all_parts.append((n, name, rows))
                self._say(t, f"{name}: " + ("; ".join(f"{pn} {op} {price or ''}".strip() for pn, op, price in rows)
                                            if rows else "no part number in the database (look it up on PARTS)"),
                          T.OK if rows else T.AMBER)
            specs = focus_specs(job, self.retriever.specs(job, limit=30))[:6]
            if specs:
                self._say(t, "=== SPECS (database) ===", T.TEXT)
                self._say(t, "\n".join(" • " + x for x in specs), T.OK)
            hits = fsm_hits(self.retriever, job + " removal installation" if not d["also"] else job, k=2, chars=700)
            if hits:
                self._say(t, "=== MANUAL ===", T.TEXT)
                self._say_hits(t, hits)
            t.moveCursor(QTextCursor.MoveOperation.Start)
        # the summary tab: the order to work in and everything to order
        t = QTextEdit()
        t.setReadOnly(True)
        self.tabs_verdict.insertTab(0, t, "SUMMARY")
        self._say(t, summary or "(no summary written)")
        self._say(t, "=== WORK ORDER ===", T.TEXT)
        self._say(t, "\n".join((" • also: " if d["also"] else f" {n}. ") + d["name"] +
                                (f": {d['REPAIR']}" if d.get("REPAIR") else "") for n, d in enumerate(issues, 1)))
        self._say(t, "=== ALL PARTS (numbers from the database) ===", T.TEXT)
        self._say(t, "\n".join(f" [{'also' if issues[n - 1]['also'] else n}] {name}: " +
                                ("; ".join(f"{pn} {price or ''}".strip() for pn, _, price in rows) if rows
                                 else "no number in the database") for n, name, rows in all_parts) or "(none named)")
        self._say(t, "Only buy a later issue's parts if the earlier test doesn't settle it.", T.MUTED)
        self.tabs_verdict.setCurrentIndex(0)

    def _think_done(self, text):
        """The reasoning streamed while it worked; once the answer is in, show only the answer."""
        if "[LLM error" in text:
            if getattr(self, "_draft_verdict", ""):
                self._say(self.txt_verdict, "[!] The review failed, so the plain model's plan is kept, unchecked.", T.AMBER)
                self._verdict_done(self._draft_verdict)
            return
        answer = re.sub(r"(?s)^.*</think>", "", text).strip()
        if "</think>" not in text or not answer:  # ran out of room mid-thought: keep the draft it was checking
            draft = getattr(self, "_draft_verdict", "")
            if draft:
                self.txt_verdict.clear()
                self._say(self.txt_verdict, "[!] The thinking model didn't finish its review, so this is the plain "
                          "model's plan, unchecked:", T.AMBER)
                self._say(self.txt_verdict, draft)
                self._verdict_done(draft)
            else:
                self._say(self.txt_verdict, "[!] The model ran out of room before it finished thinking, so no "
                          "verdict was saved. Run it again without THINK HARDER.", T.AMBER)
            return
        self.txt_verdict.clear()
        self._say(self.txt_verdict, f"[✓] Verdict ({self.mgr.label('think')}; its reasoning is hidden):", T.OK)
        self._say(self.txt_verdict, answer)
        self._verdict_done(answer)

    def _verdict_done(self, text):
        """Save the verdict and load its repair into the Parts tab, so part numbers are waiting there."""
        if "[LLM error" in text:
            return
        self.session.set("final_verdict", text)
        self._file_visit()
        self._build_verdict_tabs(text)
        _, issues = self.parse_verdict(text)
        first = next((d for d in issues if not d["also"]), None)
        job = (first.get("REPAIR") if first else None) or repair_from_verdict(text)
        if not job:
            self._say(self.txt_verdict, "(no repair named in the verdict, so the Parts tab wasn't filled)", T.MUTED)
            return
        self.bub_repair.set_text(job)
        if PS is not None:
            part = (self._issue_parts(first) if first else [])[:1] or [part_from_job(job)]
            self.in_part.setText(part[0])
            self.act_find_parts()
            self._say(self.txt_verdict, f"[✓] Parts tab ready for: {part[0]}. Tap PARTS →", T.OK)

    # ------------------------------------------------------------------ 4. SOP (deep model)
    def _sop_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("REPAIR (type or say it, or leave empty to use the verdict)")
        bl = QVBoxLayout(box)
        # typed, not a chip grid: no fixed repair list (and the 6-wide grid made the window wider than a
        # 160%-scaled screen)
        self.bub_repair = Field()
        self.bub_repair.setPlaceholderText("e.g. the repair the verdict named, or any job")
        bl.addWidget(self.bub_repair)
        b = QPushButton("PULL OEM INSTRUCTIONS && TORQUE SPECS")
        b.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b.clicked.connect(self.act_sop)
        bl.addWidget(b)
        lay.addWidget(box)
        self.txt_sop = QTextEdit()
        self.txt_sop.setReadOnly(True)
        lay.addWidget(self.txt_sop, 1)
        self.stack.addWidget(w)

    def act_sop(self):
        if self._busy():
            return
        verdict = self.session.get("final_verdict")
        job = self.bub_repair.text() or repair_from_verdict(verdict) or verdict[:300]
        if not job:
            self.txt_sop.clear()
            self._say(self.txt_sop, "Tap a repair above, or build a verdict first.", T.AMBER)
            return
        hits = fsm_hits(self.retriever, job + " removal installation", k=3, chars=900)
        fsm = [h for h, _ in hits]
        specs = focus_specs(job, self.retriever.specs(job, limit=40))[:10]
        for term in [] if specs else [t for t in job.replace(",", " ").split() if len(t) > 4][:4]:
            specs += [s for s in self.retriever.specs(term, limit=4) if s not in specs]
        self.txt_sop.clear()
        if not self.bub_repair.text() and len(job) < 60:
            self._say(self.txt_sop, f"REPAIR (from the verdict): {job}", T.AMBER)
        self._say(self.txt_sop, "=== SPECS FROM DATABASE (deterministic) ===", T.TEXT)
        self._say(self.txt_sop, "\n".join(" • " + s for s in specs) or "(none matched: check the FSM)", T.OK)
        self._say(self.txt_sop, "=== FSM SOURCES ===", T.TEXT)
        for h, figs in hits:
            self._say(self.txt_sop, h.split("\n", 1)[0], T.PURPLE_TEXT)   # sources read as reference
            self._say_figs(self.txt_sop, figs, label=h.split("\n", 1)[0])
        if not hits:
            self._say(self.txt_sop, "(none)")
        self._say(self.txt_sop, "=== PROCEDURE (deep model, from the sources above) ===", T.TEXT)
        prompt = (f"REPAIR: {job}\n\n" + (f"VERDICT:\n{verdict}\n\n" if verdict and not self.bub_repair.text() else "") +
                  "FSM EXCERPTS:\n" + "\n\n".join(fsm) + "\n\nSPECS:\n" + "\n".join(specs) +
                  "\n\nWrite a numbered shop procedure: tools, removal, cleaning/inspection, installation, "
                  "torque sequence, verification. Only use torque values listed under SPECS; where one is "
                  "missing write 'see FSM'. Mark any UNVERIFIED spec as such.")
        self._say(self.txt_sop, "")
        self._run("deep", prompt, self.txt_sop, lambda t: None, max_new_tokens=900)

    # ------------------------------------------------------------------ 5. parts (no model)
    def _ps_cfg(self, parts_row=False):
        """parts_sourcing settings for a car: the Parts tab's own Year/Model row, else the Intake car."""
        cfg = PS.settings()
        make = self.cmb_p_make.currentText() if parts_row else CFG["make"]
        cfg["kb_db"], cfg["kb_ok"], cfg["kb_msg"] = kb_status(make)
        try:
            v = json.loads(self.session.get("vehicle_info") or "{}")
        except ValueError:
            v = {}
        if parts_row:
            v = {"year": self.cmb_p_year.currentText(), "model": self.cmb_p_model.currentText()}
        year = int(v["year"]) if str(v.get("year", "")).isdigit() else cfg["car"].get("year")
        cfg["car"] = {"make": make, "model": v.get("model") or cfg["car"].get("model"), "year": year}
        return cfg

    def _sync_parts_car(self):
        """The Parts search row follows the Intake car; change it there to look up another car."""
        self.cmb_p_make.setCurrentText(self.cmb_make.currentText())
        self.cmb_p_year.setCurrentText(self.cmb_year.currentText())
        self._fill_p_models(self.cmb_p_year.currentText())
        self.cmb_p_model.setCurrentText(self.cmb_model.currentText())

    def _fill_p_models(self, year):
        keep = self.cmb_p_model.currentText()
        names = self._model_list(year)
        self.cmb_p_model.clear()
        self.cmb_p_model.addItems([""] + names)
        self.cmb_p_model.setEditable(not names)
        self.cmb_p_model.setCurrentText(keep if (keep in names or not names) else "")

    def part_text(self, arg):
        """Same lookup as the Parts tab, as text (for /part in Chat). Never saved."""
        if PS is None:
            return f"Parts finder not found in {PS_DIR}"
        try:
            return self._part_text(arg)
        except sqlite3.Error as e:  # e.g. a subaru.db built before the CHARM labor table, or mid-copy
            return f"Parts lookup failed: {e}. Rebuild/re-copy kb/subaru.db."

    def _part_text(self, arg):
        cfg = self._ps_cfg()
        if not cfg["kb_ok"]:
            return cfg["kb_msg"]
        if is_part_number(arg.strip()):
            return PS.sourcing(arg, cfg=cfg)["text"]
        rows = PS.parts_for_job(cfg["kb_db"], arg, cfg["car"]["model"], cfg["car"]["year"])
        if not rows:
            from parts_sourcing import junk_quote
            return junk_quote.quote_job(arg, cfg) or f"No CHARM part number for '{arg}' on this car."
        head = "\n".join(f"{pn}  {op}  {price or ''}" for pn, op, price in rows[:6])
        return head + "\n\n" + PS.sourcing(rows[0][0], cfg=cfg)["text"]

    def _parts_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.lbl_part = QLabel("Parts for the verdict's repair show up here. Or search by hand.")
        self.lbl_part.setStyleSheet(f"color: {T.AMBER}; font-weight: bold;")
        self.lbl_part.setWordWrap(True)
        self.b_manual = QPushButton("MANUAL SEARCH")
        self.b_manual.setCheckable(True)
        self.b_manual.setToolTip("Look up any part on any year/model by hand")
        self.b_sheet = QPushButton("PRINT SHEET")
        self.b_sheet.setToolTip("Save a printable page with the QR codes and open it in the browser")
        self.b_sheet.clicked.connect(self.act_parts_sheet)
        top.addWidget(self.lbl_part, 1)
        top.addWidget(self.b_manual)
        top.addWidget(self.b_sheet)
        lay.addLayout(top)
        box = QGroupBox("MANUAL PART SEARCH")
        self.b_manual.toggled.connect(box.setVisible)
        bl = QVBoxLayout(box)
        car = QHBoxLayout()
        self.cmb_p_year, self.cmb_p_make, self.cmb_p_model = QComboBox(), QComboBox(), QComboBox()
        self._fill_years(self.cmb_p_year)
        self.cmb_p_make.addItems([""] + MAKES)
        self.cmb_p_make.setEnabled(False)  # follows the Intake car: one make's DB is loaded at a time
        self.cmb_p_year.currentTextChanged.connect(self._fill_p_models)
        self.in_part = QLineEdit()
        self.in_part.setPlaceholderText("part name or OEM part number")
        self.in_part.returnPressed.connect(self.act_find_parts)
        for label, wdg in (("Year", self.cmb_p_year), ("Make", self.cmb_p_make), ("Model", self.cmb_p_model),
                           ("Part", self.in_part)):
            car.addWidget(QLabel(label))
            car.addWidget(wdg, 3 if wdg is self.in_part else 0)
        bl.addLayout(car)
        row = QHBoxLayout()
        b = QPushButton("SEARCH PART NUMBERS && PRICES")
        b.setStyleSheet(f"background-color: {T.FOREST}; color: {T.TEXT};")
        b.clicked.connect(self.act_find_parts)
        row.addWidget(b, 1)
        bl.addLayout(row)
        box.hide()  # hidden until MANUAL SEARCH is tapped
        lay.addWidget(box)
        self.pn_box = QGroupBox("PART NUMBERS FOR THIS CAR (tap one)")
        self.pn_lay = QVBoxLayout(self.pn_box)
        self.bub_pn = None
        self.pn_box.hide()
        lay.addWidget(self.pn_box)
        mid = QHBoxLayout()
        self.txt_parts = QTextEdit()
        self.txt_parts.setReadOnly(True)
        self.txt_parts.setStyleSheet("font-size: 18px;")  # 10-19: bigger, read at arm's length
        mid.addWidget(self.txt_parts, 3)
        self.qr_box = QWidget()
        self.qr_lay = QGridLayout(self.qr_box)
        self.qr_lay.setContentsMargins(0, 0, 0, 0)
        qr_scroll = QScrollArea()
        qr_scroll.setWidgetResizable(True)
        qr_scroll.setWidget(self.qr_box)
        qr_scroll.setMinimumWidth(240)
        mid.addWidget(qr_scroll, 1)
        lay.addLayout(mid, 1)
        self.parts_shown = []  # sourcing() results on screen, for PRINT SHEET
        self.stack.addWidget(w)
        self._sync_parts_car()
        if PS is None:
            self._say(self.txt_parts, f"Parts finder not found in {PS_DIR}. Copy the parts_sourcing folder there.",
                      T.AMBER)

    def act_find_parts(self):
        if PS is None:
            return
        try:
            self._find_parts()
        except sqlite3.Error as e:
            self.txt_parts.clear()
            self._say(self.txt_parts, f"Parts lookup failed: {e}. Rebuild/re-copy kb/subaru.db.", T.RED)

    def _find_parts(self):
        job = self.in_part.text().strip()
        if not job:
            self.txt_parts.clear()
            self._say(self.txt_parts, "Type a part name or number, then SEARCH.", T.AMBER)
            return
        cfg = self._ps_cfg(parts_row=True)
        self.txt_parts.clear()
        if not cfg["kb_ok"]:
            self._say(self.txt_parts, cfg["kb_msg"], T.AMBER)
            return
        if self.bub_pn is not None:
            self.bub_pn.deleteLater()
            self.bub_pn = None
        self.pn_box.hide()
        if is_part_number(job.strip()):
            self.lbl_part.setText(f"{job.strip().upper()} · {cfg['car']['year']} {cfg['car']['make']} {cfg['car']['model']}")
            self._show_part(job.strip())
            return
        rows = PS.parts_for_job(cfg["kb_db"], job, cfg["car"]["model"], cfg["car"]["year"])
        self.lbl_part.setText(f"{job} · {cfg['car']['year']} {cfg['car']['make']} {cfg['car']['model']}")
        if not rows:
            from parts_sourcing import junk_quote
            self._say(self.txt_parts, spaced(junk_quote.quote_job(job, cfg)) or
                      "No CHARM part number for this job on this car. Try other words, or type the OEM number.")
            self._clear_qr()
            return
        labels = [f"{pn}  {op.split(' > ')[-1]}" for pn, op, _ in rows[:12]]
        self.bub_pn = Bubbles([(None, labels)], cols=3, other_hint="", single=True)
        self.bub_pn.other.hide()
        for b in self.bub_pn.buttons:
            b.clicked.connect(lambda _, t=b.text(): self._show_part(t.split()[0]))
        self.pn_lay.addWidget(self.bub_pn)
        self.pn_box.show()
        self.bub_pn.buttons[0].setChecked(True)
        self._show_part(rows[0][0])

    def _clear_qr(self):
        while self.qr_lay.count():
            self.qr_lay.takeAt(0).widget().deleteLater()

    def _show_part(self, pn):
        res = PS.sourcing(pn, cfg=self._ps_cfg(parts_row=True))
        self.parts_shown = [res]
        self.txt_parts.clear()
        self._say(self.txt_parts, spaced(res["text"]))
        self.txt_parts.moveCursor(QTextCursor.MoveOperation.Start)
        self._clear_qr()
        if not PSQR.available():
            self.qr_lay.addWidget(QLabel("QR codes need: pip install segno"), 0, 0)
            return
        qdir = os.path.join(CFG["cache_dir"], "qr")
        os.makedirs(qdir, exist_ok=True)
        for i, ln in enumerate(res["links"]):  # sites.json order: O'Reilly, AutoZone, RockAuto, Row52
            path = os.path.join(qdir, hashlib.sha1(ln.url.encode()).hexdigest()[:16] + ".png")
            if not os.path.isfile(path):
                PSQR.qr_png(ln.url, path, scale=4)
            img = QLabel()
            img.setPixmap(QPixmap(path))
            look = ""
            if ln.site == "row52":  # Row52 links can't name a model; say what to look for in the list
                car = self._ps_cfg(parts_row=True).get("car") or {}
                look = f". Look for: {car.get('year', '')} {car.get('make', '')} {car.get('model', '')}".rstrip()
            cap = QLabel(f"{ln.label}{look}" + ("" if ln.verified else " (link unverified)"))
            cap.setWordWrap(True)
            cap.setStyleSheet("font-size: 16px; margin-bottom: 14px;")
            self.qr_lay.addWidget(img, i * 2, 0)
            self.qr_lay.addWidget(cap, i * 2 + 1, 0)

    def act_parts_sheet(self):
        if PS is None or not self.parts_shown:
            self._say(self.txt_parts, "Find a part first.", T.AMBER)
            return
        car = self._ps_cfg(parts_row=True)["car"]
        path = os.path.join(CFG["cache_dir"], "parts_sheet.html")
        os.makedirs(CFG["cache_dir"], exist_ok=True)
        with open(path, "w") as f:
            f.write(PSSHEET.render(self.parts_shown, f"{car['year']} {car['make']} {car['model']}",
                                   self.session.get("final_verdict")[:80]))
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        self._say(self.txt_parts, f"[✓] Sheet saved: {path}", T.OK)

    # ------------------------------------------------------------------ 6. TESTS (two-way)
    def _tests_view(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.b_testmode = QPushButton("TEST MODE: OFF")
        self.b_testmode.setCheckable(True)
        self.b_testmode.setToolTip("Two-way tests make parts of the car move. Off every time the deck starts.")
        self.b_testmode.clicked.connect(self.act_test_mode)
        self.lbl_testmode = QLabel("Reading only. Switch TEST MODE on to run tests or clear codes.")
        self.lbl_testmode.setStyleSheet(IDLE)
        self.lbl_testmode.setWordWrap(True)
        top.addWidget(self.b_testmode)
        top.addWidget(self.lbl_testmode, 1)
        lay.addLayout(top)

        sub = QGroupBox("SUBARU (SSM: KKL cable, or the EX if the KKL doesn't answer)")
        sl = QGridLayout(sub)
        how = QLabel("Join the green test-mode connectors under the dash with the key OFF, then key ON, engine OFF.")
        how.setWordWrap(True)
        sl.addWidget(how, 3, 0, 1, 2)
        self.b_t_connect = QPushButton("CONNECT TO SUBARU ECU")
        self.b_t_connect.clicked.connect(lambda: self.test_worker and self.test_worker.do("connect"))
        self.lst_tests = QListWidget()
        self.lst_tests.setToolTip("The actuator tests this ECU says it supports (from its own init reply)")
        self.b_t_start = QPushButton("RUN SELECTED TEST")
        self.b_t_start.clicked.connect(self.act_run_test)
        self.b_t_clearmem = QPushButton("CLEAR SUBARU ECU MEMORY")
        self.b_t_clearmem.clicked.connect(self.act_clear_memory)
        sl.addWidget(self.b_t_connect, 0, 0)
        sl.addWidget(self.b_t_clearmem, 0, 1)
        sl.addWidget(self.lst_tests, 1, 0, 1, 2)
        sl.addWidget(self.b_t_start, 2, 0, 1, 2)
        lay.addWidget(sub, 2)

        gen = QHBoxLayout()
        self.b_t_clearcodes = QPushButton("CLEAR CODES (OBD-II mode 04 on the EX; KKL fallback on a Subaru)")
        self.b_t_clearcodes.clicked.connect(self.act_clear_codes)
        gen.addWidget(self.b_t_clearcodes)
        lay.addLayout(gen)

        self.b_t_stop = QPushButton("■ STOP ALL ACTUATORS")
        self.b_t_stop.setMinimumHeight(64)
        self.b_t_stop.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT}; font-size: 20px; font-weight: bold;")
        self.b_t_stop.clicked.connect(self.act_stop_all)
        lay.addWidget(self.b_t_stop)

        self.txt_tests = QTextEdit()
        self.txt_tests.setReadOnly(True)
        lay.addWidget(self.txt_tests, 1)
        self.stack.addWidget(w)
        self._show_test_mode()

    def _show_test_mode(self, running=""):
        on = self.test_worker is not None
        self.b_testmode.setChecked(on)
        self.b_testmode.setText("TEST MODE: ON" if on else "TEST MODE: OFF")
        self.b_testmode.setStyleSheet(f"background-color: {T.RED}; color: {T.TEXT}; font-weight: bold;" if on else "")
        if running:
            self.lbl_testmode.setText(f"RUNNING: {running}")
            self.lbl_testmode.setStyleSheet(ERR)
        elif on:
            self.lbl_testmode.setText("TEST MODE on. Each action asks you first. The deck holds the cable.")
            self.lbl_testmode.setStyleSheet(BUSY)
        else:
            self.lbl_testmode.setText("Reading only. Switch TEST MODE on to run tests or clear codes.")
            self.lbl_testmode.setStyleSheet(IDLE)
        for b in (self.b_t_connect, self.b_t_clearmem, self.b_t_clearcodes, self.b_t_stop):
            b.setEnabled(on)
        self.b_t_start.setEnabled(on and self.lst_tests.count() > 0)
        if not on:
            self.lst_tests.clear()

    def _confirm(self, title, text):
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def act_test_mode(self):
        if self.test_worker is not None:   # switching off: stop everything, hand the cable back
            tw = self.test_worker
            if tw.isRunning():
                # Keep the reference until the thread really ends (dropping a running QThread aborts the app);
                # READ CAR comes back only once the port is closed.
                self.b_testmode.setEnabled(False)
                self._say(self.txt_tests, "[*] Switching TEST MODE off (finishing the current step)...", T.MUTED)
                tw.finished.connect(self._test_mode_off)
                tw.do("quit")
            else:
                self._test_mode_off()
            return
        if self.obd_worker is not None and self.obd_worker.isRunning() or self.tool_proc is not None:
            self._say(self.txt_tests, "[!] The cable is busy (READ CAR or another program). Try again when it's done.", T.AMBER)
            self._show_test_mode()
            return
        if not self._confirm("Turn TEST MODE on?",
                             "Two-way tests make parts of the car work: fuel pump, fans, solenoids, valves.\n\n"
                             "Key ON, engine OFF. Parking brake on, in Park or neutral. Hands and tools clear of "
                             "the fans and belts.\n\nTurn TEST MODE on?"):
            self._show_test_mode()
            return
        tw = TestWorker(CFG["obd_port"] if CFG.get("obd_adapter") == "kkl" else "")
        tw.msg.connect(lambda t, c: self._say(self.txt_tests, t, c))
        tw.tests.connect(self._show_tests)
        tw.running.connect(self._show_test_mode)
        self.test_worker = tw
        tw.start()
        self.b_obd.setEnabled(False)
        self._say(self.txt_tests, "[*] TEST MODE on. READ CAR and other programs wait until you switch it off.", T.AMBER)
        self._show_test_mode()

    def _test_mode_off(self):
        self.test_worker = None
        self.b_testmode.setEnabled(True)
        self._say(self.txt_tests, "[✓] TEST MODE off. The cable is back for READ CAR.", T.OK)
        self.b_obd.setEnabled(True)
        self._show_test_mode()

    def _show_tests(self, titles):
        self.lst_tests.clear()
        self.lst_tests.addItems(titles)
        self.b_t_start.setEnabled(self.test_worker is not None and bool(titles))

    def act_run_test(self):
        item = self.lst_tests.currentItem()
        if self.test_worker is None or item is None:
            self._say(self.txt_tests, "[!] Pick a test in the list first.", T.AMBER)
            return
        if self.worker is not None and self.worker.isRunning():  # a model crash mustn't end a test mid-run
            self._say(self.txt_tests, "[!] A model is still answering. Wait for it (or press STOP), then run the test.",
                      T.AMBER)
            return
        import ssm_tests
        if self._confirm(f"Run {item.text()}?",
                         f"{item.text()} will be switched on until you press STOP ALL "
                         f"(it stops by itself after {ssm_tests.MAX_TEST_S} s).\n\nKey ON, engine OFF, hands clear. Run it?"):
            self.test_worker.do("start", self.lst_tests.row(item))

    def act_stop_all(self):
        if self.test_worker is not None:  # no question, no checks, no queue: stopping is always allowed
            self.test_worker.emergency_stop()

    def act_clear_memory(self):
        if self.test_worker is not None and self._confirm(
                "Clear the Subaru ECU memory?",
                "This erases the stored trouble codes and the ECU's learned values (idle, fuel trims). "
                "Write the codes down first. The engine may idle rough until it relearns.\n\n"
                "Key ON, engine OFF. Clear it?"):
            self.test_worker.do("clear_memory")

    def act_clear_codes(self):
        if self.test_worker is not None and self._confirm(
                "Clear the trouble codes?",
                "OBD-II mode 04 clears the stored codes, freeze frame and the emissions readiness monitors "
                "(an inspection will fail until a drive cycle sets them again). Write the codes down first.\n\n"
                "It goes through the OBDLink EX. If the EX doesn't answer on a Subaru, it clears the ECU memory "
                "through the KKL cable instead, which also resets learned idle and fuel values.\n\n"
                "Key ON, engine OFF. Clear them?"):
            self.test_worker.do("clear_codes")

    def closeEvent(self, e):
        if self.drive_worker is not None:   # letting Qt destroy a running thread aborts the whole app
            self.drive_worker.stop_flag.set()
            if not self.drive_worker.wait(30000):
                self._say(self.txt_intake, "[!] Still finishing a reading; close again in a moment.", T.AMBER)
                e.ignore()
                return
        if self.tool_proc is not None:  # closing would kill it, maybe in the middle of a job on the car
            self._say(self.txt_intake, "[!] Close the other program first; it's still using the cable.", T.AMBER)
            e.ignore()
            return
        if self.test_worker is not None:  # actuators off before the deck lets go of the cable
            self.test_worker.do("quit")
            if not self.test_worker.wait(20000):  # a CONNECT can take ~16 s; never drop a running thread
                self._say(self.txt_tests, "[!] Still finishing a step on the cable; close again in a moment.",
                          T.AMBER)
                e.ignore()
                return
        self._stop_voice()
        # Freeing an RKLLM handle while rkllm_run is still using it on the worker thread can crash on exit.
        self.act_stop()
        if self.worker is not None:
            self.worker.wait(5000)
        if getattr(self, "chart_worker", None) is not None:
            self.chart_worker.stop_flag.set()
            self.chart_worker.wait(10000)
        if self.obd_worker is not None:
            self.obd_worker.wait(3000)
        for w in [getattr(self, "case_worker", None), getattr(self, "preload", None)] + getattr(self, "lookups", []) + \
                getattr(self, "warmers", []):
            if w is not None:
                w.wait(60000)
        self.mgr.release_all()
        super().closeEvent(e)


def install_home_icon():
    """Add "Cyberdeck Home" to the app list: this app with the home PC's models (the deck's own as fallback)."""
    here = os.path.dirname(os.path.abspath(__file__))
    launcher = f"{ROOT}/start_deck.sh"    # switches the earbuds to headset mode first, when it's there
    run = (f"env CYBERDECK_HOME=1 {launcher}" if os.access(launcher, os.X_OK) else
           f"env CYBERDECK_HOME=1 sh -c 'cd \"{here}\" && exec \"{sys.executable}\" cyberdeck_gui.py'")
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "cyberdeck-home.desktop")
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Cyberdeck Home\n"
                "Comment=The cyberdeck app with the big models on the home PC (over Tailscale)\n"
                f"Exec={run}\nIcon={here}/icon_home.svg\nTerminal=false\nCategories=Utility;\n")
    os.chmod(path, 0o755)
    print(f"added {path}")


def main():
    # a crash inside the NPU runtime closes the app with no Python error; this leaves the stack of every thread
    os.makedirs(f"{ROOT}/logs", exist_ok=True)
    crash_log = open(f"{ROOT}/logs/app_crash.log", "a")
    crash_log.write(f"\n=== app started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    crash_log.flush()
    faulthandler.enable(file=crash_log, all_threads=True)
    if "--home" in sys.argv[1:] or os.environ.get("CYBERDECK_HOME") == "1":
        CFG["home"] = True
    if "--install-home-icon" in sys.argv[1:]:
        install_home_icon()
        return
    app = QApplication(sys.argv)
    bus = ErrorBus()

    def hook(etype, value, tb):
        traceback.print_exception(etype, value, tb)
        bus.error.emit(f"{etype.__name__}: {value}")

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    win = CyberdeckApp(bus)
    win.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
