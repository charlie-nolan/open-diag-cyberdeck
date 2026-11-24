"""Model manager: picks RKLLM, Ollama, OpenAI-compatible, Claude or CLI models for each role."""
import datetime
import json
import os
import platform
import threading
import time
from llm_backend import (
    ClaudeModel, CliModel, OllamaModel, OpenAICompatModel, RKLLMModel, mem_available_gb, mem_total_gb,
    ollama_models, probe_rkllm)
from ui.config import ROOT
from ui.prompts import DEEP_SYSTEM, FAST_SYSTEM


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
        self.on_pc = set()
        self.pc_checked = {}
        self.pc_why = ""
        self._lock = threading.RLock()
        self.deck_only = False
        if self.home and cfg.get("deck_only"):
            self.set_deck_only(True)

    def label(self, role):
        if self.home and role in self.on_pc:
            return f"PC {self.cfg[f'home_{role}']}"
        if self.home and self.pc_why:
            return f"deck {self._local_label(role)} ({self.pc_why})"
        return self._local_label(role)

    def _local_label(self, role):
        if self.backend == "cli":
            cmd = self.cfg.get(f"cli_cmd_{role}") or self.cfg.get("cli_cmd") or ["(cli_cmd not set)"]
            return f"{os.path.basename(cmd[0])} (signed-in CLI)"
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
        opts = ({"top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": self.cfg.get("think_presence_penalty", 1.5)}
                if role == "think" else {})
        return OllamaModel(name, system_prompt=system, url=self.cfg["home_url"], temperature=temp,
                           enable_thinking=role == "think", num_ctx=self.cfg["home_context_len"], options=opts)

    def set_deck_only(self, on):
        """DECK ONLY on: drop any PC models and answer with the deck's. Off: the PC is checked at the next question."""
        with self._lock:
            self.deck_only = bool(on)
            for role in list(self.on_pc):
                self.models.pop(role, None)
            self.on_pc.clear()
            self.pc_checked.clear()
            self.pc_why = "DECK ONLY" if on else ""

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
        if self.home and not self.deck_only and role not in self.on_pc and time.time() - self.pc_checked.get(role, 0) > 60:
            m = self._pc_model(role)
            if m is not None:
                old = self.models.pop(role, None)
                if old is not None:
                    old.release()
                self.models[role] = m
                self.on_pc.add(role)
                return m
            self.pc_checked[role] = time.time()
        if role in self.models:
            return self.models[role]
        try:
            need = os.path.getsize(self.cfg[f"{role}_model"]) / 2**30 * 1.5 + 1 if self.backend == "rkllm" else 0
        except OSError:
            need = 0
        domain = 0 if role == "fast" else 1
        if self.backend == "rkllm" and role != "fast" and self.cfg[f"{role}_model"] != self.cfg["fast_model"]:
            domain = self._trial(role)
            if domain is None:
                return self.get(role)
        for other in list(self.models):
            if not self.keep_both or domain == 0 and role != "fast" or {role, other} == {"deep", "think"}:
                self.models.pop(other).release()
        if need and self.models and mem_available_gb() < need:
            print(f"models: {mem_available_gb():.1f} GB free, {need:.1f} GB needed to load {role}: "
                  f"unloading {', '.join(self.models)} first")
            for other in list(self.models):
                self.models.pop(other).release()
        system = FAST_SYSTEM if role == "fast" else DEEP_SYSTEM
        temp = {"fast": 0.4, "deep": 0.1, "think": 0.6}[role]
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
                if self.models:
                    for other in list(self.models):
                        self.models.pop(other).release()
                    try:
                        m = load()
                    except RuntimeError as e2:
                        e = e2
                if m is None:
                    if self.cfg[f"{role}_model"] == self.cfg["fast_model"]:
                        raise
                    self.cfg.setdefault("stand_in", {})[role] = f"wouldn't load: {str(e).split('(')[0].strip()}"
                    self.cfg[f"{role}_model"] = self.cfg["fast_model"]
                    return self.get(role)
        elif self.backend == "claude":
            m = ClaudeModel(self.cfg[f"claude_{role}"], system_prompt=system,
                            effort=self.cfg["claude_effort"].get(role, "medium"), enable_thinking=think)
        elif self.backend == "cli":
            m = CliModel(self.cfg.get(f"cli_cmd_{role}") or self.cfg.get("cli_cmd"), system_prompt=system,
                         timeout=self.cfg.get("cli_timeout", 900))
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
