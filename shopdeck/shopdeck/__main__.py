"""python3 -m shopdeck [--kb path/to/make.db] [--make subaru] [--tui] [--install-icon]"""
import argparse
import os
import sys

from .desk import Desk, load_config


def install_icon():
    """Put Shopdeck in the desktop's app list, started with this Python (the deck's venv)."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    icon = os.path.join(here, "shopdeck", "icon.svg")
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "shopdeck.desktop")
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Shopdeck\n"
                "Comment=Diagnose from the factory manual, quote and print the repair\n"
                f'Exec=sh -c \'cd "{here}" && exec "{sys.executable}" -m shopdeck\'\n'
                f"Icon={icon}\nTerminal=false\nCategories=Utility;\nStartupWMClass=shopdeck\n")
    os.chmod(path, 0o755)
    print(f"added {path}\nShopdeck is in the app list now (it can take a few seconds to show up).")


def main():
    ap = argparse.ArgumentParser(prog="shopdeck", description="Diagnose, quote and print repair instructions.")
    ap.add_argument("--kb", help="knowledge database (default: kb_dir/make.db from ~/.shopdeck/config.json)")
    ap.add_argument("--make", help="make to load, e.g. subaru (picks kb_dir/<make>.db)")
    ap.add_argument("--tui", action="store_true", help="run in the terminal instead of a window (for ssh)")
    ap.add_argument("--install-icon", action="store_true", help="add Shopdeck to the desktop's app list and exit")
    a = ap.parse_args()
    if a.install_icon:
        install_icon()
        return
    cfg = load_config()
    if a.make:
        cfg["make"] = a.make.lower()
    try:
        desk = Desk(cfg, kb_path=a.kb)
    except FileNotFoundError as e:
        sys.exit(f"shopdeck: {e}\nCopy the database there or pass --kb /path/to/subaru.db")
    window = not a.tui and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if window:
        try:
            from .gui import main as run
        except ImportError as e:              # no PyQt6 in this Python: the terminal version still works
            print(f"no window ({e}); running in the terminal")
            from .tui import main as run
    else:
        from .tui import main as run
    run(desk)


if __name__ == "__main__":
    main()
