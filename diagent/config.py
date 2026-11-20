"""Settings: diagent defaults, then the main app's config.json (same model names and PC address), then
diagent/config.json. The model tags match app/cyberdeck_gui.py DEFAULT_CONFIG (checked 2026-10-25)."""
import json
import os

from . import HERE, ROOT

DEFAULTS = {
    "kb_dir": f"{ROOT}/kb",
    "home_url": os.environ.get("CYBERDECK_HOME_URL", "http://127.0.0.1:11434"),
    "local_url": "http://127.0.0.1:11434",
    "home_fast": "qwen3.6:35b-a3b-q4_K_M",
    "home_deep": "qwen3-next:80b-a3b-instruct-q4_K_M",
    "agent_role": "home_fast",
    "num_ctx": 16384,
    "temperature": 0.2,
    "think": False,
    "max_calls_per_turn": 8,
    "agent_wall_s": 1800,
    "case_db": f"{HERE}/data/cases.db",
    "session_db": f"{HERE}/data/session.db",
    "cache_dir": f"{HERE}/data/cache",
    "obd_port": "",
    "vin_db": f"{ROOT}/kb/vin.db",
    "rom_db": f"{ROOT}/kb/romid.db",
    "nhtsa_cache": f"{HERE}/data/vin_nhtsa.json",
    "ecu_vin_files": [f"{ROOT}/cache/ecu_vins.json", f"{HERE}/data/ecu_vins.json"],
    "app_session_db": f"{ROOT}/session_active.db",
    "image_dirs": [f"{ROOT}/kb/images", f"{ROOT}/test_batch_out", f"{ROOT}/extracted_images"],
}


def load():
    cfg = dict(DEFAULTS)
    for path in (f"{ROOT}/app/config.json", f"{HERE}/config.json"):
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
            except (OSError, ValueError) as e:
                print(f"{path} unreadable ({e}); using defaults")
    return cfg
