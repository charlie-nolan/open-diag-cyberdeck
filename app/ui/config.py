"""Settings: app/config.json over the defaults, config migrations, and the parts lookup import."""
import json
import os
import sys
import threading


APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(APP_DIR)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DEFAULT_CONFIG = {
    "vehicle": "",
    "backend": "auto",
    "cli_cmd": [], "cli_timeout": 900,
    "openai_url": os.environ.get("CYBERDECK_OPENAI_URL", "http://127.0.0.1:1234/v1"),
    "openai_key_env": "OPENAI_API_KEY",
    "openai_fast": "", "openai_deep": "", "openai_think": "",
    "claude_fast": "claude-opus-5-5", "claude_deep": "claude-opus-5-5", "claude_think": "claude-opus-5-5",
    "claude_effort": {"fast": "low", "deep": "high", "think": "high"},
    "rkllm_lib": [f"{ROOT}/rknn-llm/rkllm-runtime/Linux/librkllm_api/aarch64/librkllmrt.so", "/usr/lib/librkllmrt.so"],
    "fast_model": [f"{ROOT}/models/rkllm/Qwen3-1.7B_W8A8_RK3588.rkllm",
                   f"{ROOT}/models/rkllm/qwen2.5-3b-w8a8-rk3588.rkllm"],
    "deep_model": f"{ROOT}/models/rkllm/Qwen3-4B-Instruct-2507_W8A8_RK3588.rkllm",
    "think_model": f"{ROOT}/models/rkllm/Qwen3-4B-Thinking-2507_W8A8_RK3588.rkllm",
    "ollama_fast": "qwen3:1.7b",
    "ollama_deep": "qwen3:4b-instruct-2507-q4_K_M",
    "ollama_think": "qwen3:4b-thinking-2507-q4_K_M",
    "max_context_len": 4096,
    "home": False,
    "home_url": os.environ.get("CYBERDECK_HOME_URL", "http://127.0.0.1:11434"),
    "home_fast": "qwen3.6:35b-a3b-q4_K_M",
    "home_deep": "qwen3-next:80b-a3b-instruct-q4_K_M",
    "home_think": "qwen3-next:80b-a3b-thinking-q4_K_M",
    "home_context_len": 16384,
    "keep_both_loaded": "auto",
    "verdict_blind_first": True,
    "verdict_steps": False,
    "kb_dir": f"{ROOT}/kb",
    "vin_db": f"{ROOT}/kb/vin.db",
    "rom_db": f"{ROOT}/kb/romid.db",
    "kb_db": "",
    "make": "",
    "kb_model": None,
    "kb_year": None,
    "diag_db": f"{ROOT}/subaru_diag.db",
    "knowledge_db": f"{ROOT}/test_subaru_knowledge.db",
    "session_db": f"{ROOT}/session_active.db",
    "cache_dir": f"{ROOT}/cache",
    "pic_url": os.environ.get("CYBERDECK_PIC_URL", "http://127.0.0.1:8765"),
    "charm_pics_max": 8,
    "voice_enabled": True,
    "vosk_model": f"{ROOT}/models/voice/vosk-model-small-en-us-0.15",
    "whisper_bin": f"{ROOT}/models/voice/whisper.cpp/build/bin/whisper-cli",
    "whisper_model": f"{ROOT}/models/voice/ggml-base.en.bin",
    "whisper_model_fallback": f"{ROOT}/models/voice/ggml-tiny.en.bin",
    "whisper_threads": 4,
    "piper_bin": "",
    "piper_model": "",
    "mic_device": "",
    "tts_rate": 145,
    "obd_adapter": "obdlink",
    "ex_ssm_variant": "",
    "launchers": {},
    "obd_port": "",
    "embed_model": "",
    "embed_threads": 4,
    "parts_sourcing_dir": f"{ROOT}/parts_sourcing",
}

CONFIG_VERSION = 2


def _forget_old_home_defaults(data):
    """v1 -> v2: config.json files from before the 10-21 model change carry the old defaults as if chosen."""
    old = {"qwen3:8b", "qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3:30b-a3b-thinking-2507-q4_K_M"}
    for key in ("home_fast", "home_deep", "home_think"):
        if data.get(key) in old:
            del data[key]


CONFIG_MIGRATIONS = {1: _forget_old_home_defaults}


def migrate_config(data):
    """Bring a config.json dict up to CONFIG_VERSION, once. Returns True when it changed.
    A value the user sets after the migration is never touched again."""
    version = data.get("config_version", 1)
    if version >= CONFIG_VERSION:
        return False
    for v in range(version, CONFIG_VERSION):
        if v in CONFIG_MIGRATIONS:
            CONFIG_MIGRATIONS[v](data)
    data["config_version"] = CONFIG_VERSION
    return True


def _write_config(data):
    tmp = CONFIG_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            if migrate_config(data):
                _write_config(data)
            cfg.update(data)
        except (OSError, ValueError) as e:
            print(f"config.json unreadable ({e}); using defaults")
    cfg = {k: os.path.expanduser(v) if isinstance(v, str) and v.startswith("~") else v for k, v in cfg.items()}
    for key in ("rkllm_lib", "fast_model", "deep_model", "think_model"):
        cands = [cfg[key]] if isinstance(cfg[key], str) else list(cfg[key])
        default = DEFAULT_CONFIG[key]
        cands += [default] if isinstance(default, str) else default
        cands = [os.path.expanduser(c) for c in cands]
        cfg[key] = next((c for c in cands if os.path.isfile(c)), cands[0])
    cfg["stand_in"] = {}
    for key in ("deep_model", "think_model"):
        if not os.path.isfile(cfg[key]) and os.path.isfile(cfg["fast_model"]):
            cfg["stand_in"][key[:-6]] = "file not on the deck yet"
            cfg[key] = cfg["fast_model"]
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
    from parts_sourcing.interchange import is_part_number
except ImportError:
    PS = PSQR = PSSHEET = None

    def is_part_number(s):
        return False


_CONFIG_LOCK = threading.Lock()


def save_config_key(key, value):
    """Remember a choice (mic, cable) in config.json without touching the rest of it."""
    with _CONFIG_LOCK:
        data = {}
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
        except OSError:
            pass
        except ValueError:
            return
        data[key] = value
        _write_config(data)
