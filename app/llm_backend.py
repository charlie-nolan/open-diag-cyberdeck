"""
LLM backends for the cyberdeck GUI.

RKLLMModel  - Rockchip NPU via librkllmrt.so (Orange Pi 5 / RK3588). ctypes layout copied
              from rknn-llm v1.3.1 (examples/rkllm_server_demo/rkllm_server/flask_server.py).
              The .rkllm files must be exported with the same toolkit version as the runtime.
OllamaModel - Same interface over Ollama's HTTP API, so the GUI can be tested on the desktop.

Both expose: generate(prompt, on_token, keep_history=False, max_new_tokens=None) -> str,
             clear_history(), abort(), release().
Only one generation runs at a time (the NPU is shared), enforced by _RUN_LOCK.
"""
import ctypes
import json
import os
import threading

_RUN_LOCK = threading.Lock()


# --------------------------------------------------------------------------- RKLLM ctypes
class RKLLMExtendParam(ctypes.Structure):
    _fields_ = [("base_domain_id", ctypes.c_int32), ("embed_flash", ctypes.c_int8),
                ("enabled_cpus_num", ctypes.c_int8), ("enabled_cpus_mask", ctypes.c_uint32),
                ("n_batch", ctypes.c_uint8), ("use_cross_attn", ctypes.c_int8),
                ("reserved", ctypes.c_uint8 * 104)]


class RKLLMParam(ctypes.Structure):
    _fields_ = [("model_path", ctypes.c_char_p), ("max_context_len", ctypes.c_int32),
                ("max_new_tokens", ctypes.c_int32), ("top_k", ctypes.c_int32),
                ("n_keep", ctypes.c_int32), ("top_p", ctypes.c_float),
                ("temperature", ctypes.c_float), ("repeat_penalty", ctypes.c_float),
                ("frequency_penalty", ctypes.c_float), ("presence_penalty", ctypes.c_float),
                ("mirostat", ctypes.c_int32), ("mirostat_tau", ctypes.c_float),
                ("mirostat_eta", ctypes.c_float), ("skip_special_token", ctypes.c_bool),
                ("ignore_eos_token", ctypes.c_bool), ("is_async", ctypes.c_bool),
                ("extend_param", RKLLMExtendParam)]


class RKLLMEmbedInput(ctypes.Structure):
    _fields_ = [("embed", ctypes.POINTER(ctypes.c_float)), ("n_tokens", ctypes.c_size_t)]


class RKLLMTokenInput(ctypes.Structure):
    _fields_ = [("input_ids", ctypes.POINTER(ctypes.c_int32)), ("n_tokens", ctypes.c_size_t)]


class RKLLMImageInput(ctypes.Structure):
    _fields_ = [("image_embed", ctypes.POINTER(ctypes.c_float)), ("n_image_tokens", ctypes.c_size_t),
                ("n_image", ctypes.c_size_t), ("image_start", ctypes.c_char_p),
                ("image_end", ctypes.c_char_p), ("image_content", ctypes.c_char_p),
                ("image_width", ctypes.c_size_t), ("image_height", ctypes.c_size_t)]


class RKLLMVideoInput(ctypes.Structure):
    _fields_ = [("video_embed", ctypes.POINTER(ctypes.c_float)), ("n_frame_tokens", ctypes.c_size_t),
                ("n_frame_per_video", ctypes.c_size_t), ("n_video", ctypes.c_size_t),
                ("video_start", ctypes.c_char_p), ("video_end", ctypes.c_char_p),
                ("video_content", ctypes.c_char_p), ("frame_width", ctypes.c_size_t),
                ("frame_height", ctypes.c_size_t)]


class RKLLMAudioInput(ctypes.Structure):
    _fields_ = [("audio_embed", ctypes.POINTER(ctypes.c_float)), ("n_audio_tokens", ctypes.c_size_t),
                ("n_audio", ctypes.c_size_t), ("audio_start", ctypes.c_char_p),
                ("audio_end", ctypes.c_char_p), ("audio_content", ctypes.c_char_p)]


class RKLLMMultiModalInput(ctypes.Structure):
    _fields_ = [("prompt", ctypes.c_char_p), ("image", RKLLMImageInput),
                ("video", RKLLMVideoInput), ("audio", RKLLMAudioInput)]


class RKLLMInputUnion(ctypes.Union):
    _fields_ = [("prompt_input", ctypes.c_char_p), ("embed_input", RKLLMEmbedInput),
                ("token_input", RKLLMTokenInput), ("multimodal_input", RKLLMMultiModalInput)]


class RKLLMInput(ctypes.Structure):
    _anonymous_ = ("input_data",)
    _fields_ = [("role", ctypes.c_char_p), ("enable_thinking", ctypes.c_bool),
                ("input_type", ctypes.c_int), ("input_data", RKLLMInputUnion)]


class RKLLMLoraParam(ctypes.Structure):
    _fields_ = [("lora_adapter_name", ctypes.c_char_p)]


class RKLLMPromptCacheParam(ctypes.Structure):
    _fields_ = [("save_prompt_cache", ctypes.c_int), ("prompt_cache_path", ctypes.c_char_p)]


class RKLLMSamplingParam(ctypes.Structure):
    _fields_ = [("top_k", ctypes.c_int32), ("top_p", ctypes.c_float), ("temperature", ctypes.c_float),
                ("repeat_penalty", ctypes.c_float), ("frequency_penalty", ctypes.c_float),
                ("presence_penalty", ctypes.c_float), ("mirostat", ctypes.c_int32),
                ("mirostat_tau", ctypes.c_float), ("mirostat_eta", ctypes.c_float)]


class RKLLMInferParam(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("lora_params", ctypes.POINTER(RKLLMLoraParam)),
                ("prompt_cache_params", ctypes.POINTER(RKLLMPromptCacheParam)),
                ("sampling_params", ctypes.POINTER(RKLLMSamplingParam)),
                ("keep_history", ctypes.c_int), ("max_new_tokens", ctypes.c_int32)]


class RKLLMResultLastHiddenLayer(ctypes.Structure):
    _fields_ = [("hidden_states", ctypes.POINTER(ctypes.c_float)), ("embd_size", ctypes.c_int),
                ("num_tokens", ctypes.c_int)]


class RKLLMResultLogits(ctypes.Structure):
    _fields_ = [("logits", ctypes.POINTER(ctypes.c_float)), ("vocab_size", ctypes.c_int),
                ("num_tokens", ctypes.c_int)]


class RKLLMPerfStat(ctypes.Structure):
    _fields_ = [("prefill_time_ms", ctypes.c_float), ("prefill_tokens", ctypes.c_int),
                ("generate_time_ms", ctypes.c_float), ("generate_tokens", ctypes.c_int),
                ("memory_usage_mb", ctypes.c_float)]


class RKLLMResult(ctypes.Structure):
    _fields_ = [("text", ctypes.c_char_p), ("token_id", ctypes.c_int),
                ("last_hidden_layer", RKLLMResultLastHiddenLayer),
                ("logits", RKLLMResultLogits), ("perf", RKLLMPerfStat)]


_ResultCB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(RKLLMResult), ctypes.c_void_p, ctypes.c_int)
_TokenizerCB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                ctypes.POINTER(ctypes.c_int32), ctypes.c_int32)
_EmbedCB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int32),
                            ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64)


class RKLLMCallback(ctypes.Structure):
    _fields_ = [("result_callback", _ResultCB), ("result_userdata", ctypes.c_void_p),
                ("tokenizer_callback", _TokenizerCB), ("tokenizer_userdata", ctypes.c_void_p),
                ("embed_callback", _EmbedCB), ("embed_userdata", ctypes.c_void_p)]


RUN_NORMAL, RUN_WAITING, RUN_FINISH, RUN_ERROR = 0, 1, 2, 3
_lib = None


def _load_lib(lib_path):
    global _lib
    if _lib is None:
        _lib = ctypes.CDLL(lib_path)
        _lib.rkllm_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(RKLLMParam),
                                    ctypes.POINTER(RKLLMCallback)]
        _lib.rkllm_init.restype = ctypes.c_int
        _lib.rkllm_run.argtypes = [ctypes.c_void_p, ctypes.POINTER(RKLLMInput),
                                   ctypes.POINTER(RKLLMInferParam), ctypes.c_void_p]
        _lib.rkllm_run.restype = ctypes.c_int
        _lib.rkllm_set_chat_template.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                                 ctypes.c_char_p]
        _lib.rkllm_set_chat_template.restype = ctypes.c_int
        _lib.rkllm_clear_kv_cache.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        _lib.rkllm_clear_kv_cache.restype = ctypes.c_int
        _lib.rkllm_abort.argtypes = [ctypes.c_void_p]
        _lib.rkllm_destroy.argtypes = [ctypes.c_void_p]
    return _lib


class RKLLMModel:
    """One .rkllm model resident on the NPU. Text streams through on_token."""

    def __init__(self, model_path, lib_path, system_prompt="", max_context_len=4096,
                 max_new_tokens=1024, temperature=0.3, top_k=20, top_p=0.8,
                 repeat_penalty=1.05, enable_thinking=False, domain_id=0):
        if not os.path.isfile(model_path):
            raise FileNotFoundError(model_path)
        lib = _load_lib(lib_path)
        self.name = os.path.basename(model_path)
        self.enable_thinking = enable_thinking
        self._sink = None
        self._error = False
        # keep the CFUNCTYPE object alive for the lifetime of the handle
        self._cb_fn = _ResultCB(self._on_result)

        p = RKLLMParam()
        p.model_path = model_path.encode()
        p.max_context_len = max_context_len
        p.max_new_tokens = max_new_tokens
        p.top_k, p.top_p, p.temperature = top_k, top_p, temperature
        self._sampling = (top_k, top_p, repeat_penalty)
        p.repeat_penalty, p.frequency_penalty, p.presence_penalty = repeat_penalty, 0.0, 0.0
        p.n_keep = -1
        p.mirostat, p.mirostat_tau, p.mirostat_eta = 0, 5.0, 0.1
        p.skip_special_token = True
        p.ignore_eos_token = False
        p.is_async = False
        p.extend_param.base_domain_id = domain_id  # its own NPU memory domain when two models are loaded
        p.extend_param.embed_flash = 1
        p.extend_param.n_batch = 1
        p.extend_param.use_cross_attn = 0
        p.extend_param.enabled_cpus_num = 4
        p.extend_param.enabled_cpus_mask = (1 << 4) | (1 << 5) | (1 << 6) | (1 << 7)  # A76 big cores

        self._cb = RKLLMCallback()
        self._cb.result_callback = self._cb_fn
        self._cb.result_userdata = None
        self._cb.tokenizer_callback = _TokenizerCB()
        self._cb.embed_callback = _EmbedCB()

        self.handle = ctypes.c_void_p()
        ret = lib.rkllm_init(ctypes.byref(self.handle), ctypes.byref(p), ctypes.byref(self._cb))
        if ret != 0:
            raise RuntimeError(f"rkllm_init failed ({ret}) for {model_path} "
                               f"({os.path.getsize(model_path) / 1e9:.2f} GB on disk). Check the copy is "
                               "complete and was exported with the same rknn-llm version as librkllmrt.so.")
        if system_prompt:
            # Qwen chat format; an empty <think> block keeps Qwen3 hybrid models out of thinking mode.
            postfix = "<|im_end|>\n<|im_start|>assistant\n"
            # Think tags only for Qwen3 hybrid-thinking models: Qwen2.5 and the 2507 Instruct template have none
            low = self.name.lower()
            if "qwen3" in low and not ("instruct" in low and "2507" in low):
                postfix += "<think>\n" if enable_thinking else "<think>\n\n</think>\n\n"
            lib.rkllm_set_chat_template(self.handle,
                                        f"<|im_start|>system\n{system_prompt}<|im_end|>\n".encode(),
                                        b"<|im_start|>user\n", postfix.encode())

        self._infer = RKLLMInferParam()
        ctypes.memset(ctypes.byref(self._infer), 0, ctypes.sizeof(RKLLMInferParam))
        self._infer.mode = 0  # RKLLM_INFER_GENERATE

    def _on_result(self, result, userdata, state):
        if state == RUN_NORMAL and self._sink is not None and result and result.contents.text:
            self._sink(result.contents.text.decode("utf-8", errors="replace"))
        elif state == RUN_ERROR:
            self._error = True
        return 0

    def generate(self, prompt, on_token, keep_history=False, max_new_tokens=None, temperature=None):
        parts = []
        sampling = None
        if temperature is not None:  # per-call override (v1.3.0+), e.g. /wildcard
            top_k, top_p, rp = self._sampling
            sampling = RKLLMSamplingParam(top_k=top_k, top_p=top_p, temperature=temperature, repeat_penalty=rp,
                                          mirostat_tau=5.0, mirostat_eta=0.1)

        def sink(t):
            parts.append(t)
            on_token(t)

        inp = RKLLMInput()
        inp.role = b"user"
        inp.enable_thinking = self.enable_thinking
        inp.input_type = 0  # RKLLM_INPUT_PROMPT
        inp.prompt_input = prompt.encode("utf-8")
        with _RUN_LOCK:
            self._sink, self._error = sink, False
            self._infer.keep_history = 1 if keep_history else 0
            self._infer.max_new_tokens = max_new_tokens or 0
            self._infer.sampling_params = ctypes.pointer(sampling) if sampling else None
            ret = _lib.rkllm_run(self.handle, ctypes.byref(inp), ctypes.byref(self._infer), None)
            self._sink = None
        if ret != 0 or self._error:
            raise RuntimeError(f"rkllm_run failed (ret={ret})")
        return "".join(parts)

    def clear_history(self):
        _lib.rkllm_clear_kv_cache(self.handle, 1, None, None)

    def abort(self):
        _lib.rkllm_abort(self.handle)

    def release(self):
        if self.handle:
            _lib.rkllm_destroy(self.handle)
            self.handle = ctypes.c_void_p()


# --------------------------------------------------------------------------- Ollama (desktop)
class OllamaModel:
    """Same interface over Ollama's HTTP API: the desktop test setup, and the home PC's bigger models for the
    deck (Cyberdeck Home, over Tailscale). History kept client-side. Standard library only."""

    def __init__(self, model_name, system_prompt="", url="http://127.0.0.1:11434",
                 temperature=0.3, max_new_tokens=1024, enable_thinking=False, num_ctx=4096, options=None, **_):
        self.options = dict(options or {})      # extra Ollama sampling options (top_p, top_k, presence_penalty...)
        self.name, self.url = model_name, url.rstrip("/")
        self.system_prompt, self.temperature, self.max_new_tokens = system_prompt, temperature, max_new_tokens
        self.think = enable_thinking
        self.num_ctx = num_ctx
        self.history = []
        self._resp = None

    def generate(self, prompt, on_token, keep_history=False, max_new_tokens=None, temperature=None):
        import urllib.request
        msgs = [{"role": "system", "content": self.system_prompt}] if self.system_prompt else []
        msgs += self.history + [{"role": "user", "content": prompt}]
        payload = {"model": self.name, "messages": msgs, "stream": True, "think": self.think,
                   "options": {"temperature": self.temperature if temperature is None else temperature,
                               "num_predict": max_new_tokens or self.max_new_tokens,
                               # a fixed window instead of Ollama's default, which silently drops the
                               # oldest messages (the case) when a prompt is long
                               "num_ctx": self.num_ctx, **self.options}}
        req = urllib.request.Request(f"{self.url}/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        parts, in_think = [], False
        with _RUN_LOCK:
            self._resp = urllib.request.urlopen(req, timeout=600)
            try:
                for line in self._resp:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise RuntimeError(data["error"])
                    msg = data.get("message", {})
                    if msg.get("thinking"):  # same "<think>...</think>" shape the RKLLM stream has
                        t = ("<think>\n" if not in_think else "") + msg["thinking"]
                        in_think = True
                        parts.append(t)
                        on_token(t)
                    tok = msg.get("content", "")
                    if tok and in_think:
                        tok, in_think = "\n</think>\n\n" + tok, False
                    if tok:
                        parts.append(tok)
                        on_token(tok)
                    if data.get("done"):
                        break
            finally:
                self._resp.close()
                self._resp = None
        text = "".join(parts)
        if keep_history:
            self.history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": text}]
        return text

    def clear_history(self):
        self.history = []

    def abort(self):
        if self._resp is not None:
            self._resp.close()

    def release(self):
        pass


def ollama_models(url, timeout=3):
    """Names of the models an Ollama server has, or None when it can't be reached."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as r:
            return [m.get("name", "") for m in json.load(r).get("models", [])]
    except (OSError, ValueError):
        return None


def mem_available_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def mem_total_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


# --------------------------------------------------------------------------- out-of-process trial load
def probe_rkllm(model_path, lib_path, domain_id=0, timeout=600):
    """Load the model in a child process and generate a few tokens. -> (ok, reason, output).
    A model the NPU driver can't hold can kill the whole process instead of raising, which would take the GUI
    with it, so a big model is tried out of process before the app loads it itself."""
    import subprocess
    import sys
    cmd = [sys.executable, os.path.abspath(__file__), "--probe", model_path, lib_path, str(domain_id)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"took over {timeout} s to load", ""
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 0 and "PROBE OK" in r.stdout:
        return True, "", out
    if r.returncode < 0:
        reason = f"crashed the loader (signal {-r.returncode})"
    else:
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        reason = lines[-1][:120] if lines else f"loader exited {r.returncode}"
    return False, reason, out


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "--probe":
        m = RKLLMModel(sys.argv[2], sys.argv[3], domain_id=int(sys.argv[4]))
        m.generate("Reply with OK.", lambda t: None, max_new_tokens=4)
        m.release()
        print("PROBE OK", flush=True)
