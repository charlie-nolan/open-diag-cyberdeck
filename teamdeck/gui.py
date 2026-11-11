"""teamdeck: the main deck app, unchanged, with the team as its only brain (Charlie 2026-11-11: "everything exactly
the same except it's just for the team, no switching").

It runs app/cyberdeck_gui.py's own window. The only swap is the model manager: every model call the app makes
(chat, /debate, /wildcard, /common, Final Diagnosis, Think Harder, the repair guide, note summaries) goes through
TeamModel.generate to the PC's team service (team/service.py, stage "ask") instead of RKLLM or Ollama. The team
gets the app's own prompt (its case summary and manual text) plus the read-only DB tools; Claude writes the answer
in the format the app asked for, so every screen parses it as usual. Deep answers show TEAM NOTES and the deck's
lookup list under them; the [source check] also counts the pages the team looked up.

When the PC can't be reached, an answer says so ([LLM error: ...]); use the main app (RKLLM, or --home) then.
"""
import datetime
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
import types

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from . import HERE, client

import cyberdeck_gui as G      # app/ (teamdeck/__init__ puts it on sys.path)
import faithfulness as _faith

ICON = os.path.join(HERE, "icon.svg")
LABEL = "Team (Claude lead; Gemini, GPT-OSS, local Qwen)"


class Progress(QObject):
    line = pyqtSignal(str)


PROGRESS = None          # made on the UI thread in main(); TeamModel emits into it from GenWorker threads
EVIDENCE = {"text": ""}  # what the team looked up for the last answer, for the source check


def _case_for_team():
    """Car + codes for the team's DB scope, from the app's open case. No VIN, customer or mileage."""
    c = client.deck_case(G.CFG["session_db"])
    v = c.get("vehicle_info") or {}
    if not v.get("make") and G.CFG.get("make"):
        c["vehicle_info"] = dict(v, make=G.CFG["make"], model=G.CFG.get("kb_model") or v.get("model"),
                                 year=G.CFG.get("kb_year") or v.get("year"))
    return client.scrub({"vehicle_info": c.get("vehicle_info", {}), "dtcs": c.get("dtcs", "")})


class TeamModel:
    """The interface GenWorker uses (generate, abort), answered by the PC's team."""

    def __init__(self, role):
        self.role = role
        self.job = None
        self.stopped = False

    def _progress(self, text):
        if PROGRESS is not None:
            PROGRESS.line.emit(text)

    def generate(self, prompt, on_token, keep_history=False, max_new_tokens=None, temperature=None):
        self.stopped = False
        try:
            case = _case_for_team()
        except ValueError as e:
            return self._fail(on_token, str(e))
        if not case["vehicle_info"].get("make"):
            return self._fail(on_token, "pick the car's make on Intake first (the team's manual lookups need it)")
        r = client.ask(case, prompt, self.role)
        if r.get("error"):
            return self._fail(on_token, r["error"])
        self.job, at, misses = r["id"], 0, 0
        while not self.stopped:
            st = client.status(self.job, at)
            if st.get("error"):
                misses += 1
                if misses > 20:
                    return self._fail(on_token, st["error"])
            else:
                misses = 0
                for ln in st.get("progress", []):
                    self._progress("TEAM: " + ln.split(" ", 1)[-1])
                at = st.get("next", at)
                if st.get("state") not in ("queued", "running"):
                    break
            time.sleep(2)
        if self.stopped:
            return self._fail(on_token, "stopped")
        if st.get("state") != "done":
            return self._fail(on_token, f"team run {st.get('state')}")
        res = st.get("result", {})
        answer = res.get("answer", "")
        EVIDENCE["text"] = res.get("evidence", "")
        on_token(answer)
        notes = (res.get("notes") or "").strip()
        if self.role in ("deep", "think"):
            on_token("\n\n=== TEAM NOTES ===\n" + (notes or "(none)") + "\n\n" + (res.get("lookups") or ""))
        elif notes and notes.lower() not in ("none", "none."):
            on_token(f"\n[team] {notes.splitlines()[0][:300]}")
        return answer

    def _fail(self, on_token, why):
        text = f"\n[LLM error: team: {why}]"
        on_token(text)
        return text

    def abort(self):
        self.stopped = True
        if self.job:
            client.stop(self.job)


class TeamManager(G.ModelManager):
    """ModelManager's interface with one backend, the team. Home-mode budgets (the PC answers), no RKLLM preload,
    no switching."""

    def __init__(self, cfg):
        super().__init__(dict(cfg, backend="ollama", home=False))
        self.backend, self.home, self.pc_why = "team", True, ""
        self.models = {r: TeamModel(r) for r in ("fast", "deep", "think")}

    def label(self, role):
        return LABEL

    def get(self, role):
        return self.models.setdefault(role, TeamModel(role))

    def pc_failed(self, role):
        pass

    def release_all(self):
        for m in self.models.values():
            m.abort()


def _with_team_pages(prompt):
    return prompt + ("\n\n" + EVIDENCE["text"] if EVIDENCE["text"] else "")


# the app's source check, also counting the pages the team looked up for the answer (team ids like [M3])
faith = types.SimpleNamespace(**{k: getattr(_faith, k) for k in dir(_faith) if not k.startswith("__")})
faith.check = lambda answer, prompt: _faith.check(answer, _with_team_pages(prompt))
faith.report = lambda answer, prompt: _faith.report(answer, _with_team_pages(prompt))


def install_icon():
    """Add teamdeck to the app list (KDE on the PC, the deck's Ubuntu menu). It never autostarts."""
    apps = os.path.expanduser("~/.local/share/applications")
    os.makedirs(apps, exist_ok=True)
    path = os.path.join(apps, "teamdeck.desktop")
    root = os.path.dirname(HERE)
    run = f"sh -c 'cd \"{root}\" && exec \"{sys.executable}\" -m teamdeck'"
    with open(path, "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Teamdeck\nGenericName=Car diagnosis team\n"
                "Comment=The cyberdeck app with Claude, Gemini, GPT-OSS and local Qwen as its models (PC)\n"
                f"Exec={run}\nIcon={ICON}\nTerminal=false\nCategories=Utility;\nStartupWMClass=teamdeck\n")
    os.chmod(path, 0o755)
    print(f"added {path}")
    return 0


def main(argv):
    global PROGRESS
    if "--install-icon" in argv:
        return install_icon()
    os.makedirs(f"{G.ROOT}/logs", exist_ok=True)
    crash_log = open(f"{G.ROOT}/logs/app_crash.log", "a")
    crash_log.write(f"\n=== teamdeck started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}\n")
    crash_log.flush()
    faulthandler.enable(file=crash_log, all_threads=True)
    if not os.environ.get("TEAMDECK_URL"):   # the team service runs on the PC that Home mode uses
        import urllib.parse
        client.SERVICE = f"http://{urllib.parse.urlparse(G.CFG.get('home_url', '')).hostname or '127.0.0.1'}:8767"
    G.ModelManager = TeamManager       # the app builds its manager from this name
    G.faithfulness = faith
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("teamdeck")
    app.setDesktopFileName("teamdeck")
    bus = G.ErrorBus()

    def hook(etype, value, tb):
        traceback.print_exception(etype, value, tb)
        bus.error.emit(f"{etype.__name__}: {value}")

    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    PROGRESS = Progress()
    win = G.CyberdeckApp(bus)
    win.setWindowTitle("TEAMDECK // TEAM DIAGNOSIS (PC)")
    PROGRESS.line.connect(lambda t: (win.lbl_llm.setText(t[:160].upper()), win.lbl_llm.setStyleSheet(G.BUSY)))
    h = client.health()
    win.lbl_llm.setText("[team] PC team offline: answers will fail; use the main app" if h.get("error") else
                        f"[team] {LABEL} (idle)")
    if os.environ.get("TEAMDECK_NO_MAX"):
        win.show()
    else:
        win.showMaximized()
    return app.exec()
