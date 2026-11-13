"""
Tiny client for llama.cpp's llama-server (OpenAI-compatible /v1/chat/completions). Standard library only.

Every call asks for JSON that matches a schema; llama-server turns the schema into a grammar, so the
reply always parses. Qwen3-style thinking is switched off (start llama-server with --jinja).
"""
import json
import time
import urllib.error
import urllib.request


def conform(value, schema):
    """Make a reply match the schema even if the server ignored it: missing or wrong-typed fields get an
    empty default, unknown enum values become the last choice ('unknown'/'unresolved'/'no'), extra keys go.
    Raises ValueError only when the top level isn't an object."""
    t = schema.get("type")
    types = t if isinstance(t, list) else [t]
    if value is None and "null" in types:
        return None
    if "object" in types:
        if not isinstance(value, dict):
            raise ValueError(f"expected an object, got {type(value).__name__}")
        return {k: _field(value.get(k), s) for k, s in schema.get("properties", {}).items()}
    return _field(value, schema)


def _field(v, s):
    t = s.get("type")
    types = t if isinstance(t, list) else [t]
    if v is None and "null" in types:
        return None
    if "enum" in s:
        return v if v in s["enum"] else s["enum"][-1]
    if "object" in types:
        return conform(v if isinstance(v, dict) else {}, s)
    if "array" in types:
        items = v if isinstance(v, list) else []
        items = [_field(x, s.get("items", {})) for x in items][: s.get("maxItems", 10 ** 6)]
        return [x for x in items if x not in (None, "", {})]
    if "boolean" in types:
        return v if isinstance(v, bool) else str(v).lower() in ("true", "yes", "1")
    if "integer" in types:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None if "null" in types else 0
    if "string" in types:
        return v.strip() if isinstance(v, str) else (None if "null" in types else str(v or ""))
    return v


class LLM:
    def __init__(self, url="http://127.0.0.1:8080", temperature=0.2, max_tokens=1024, timeout=600, model=None,
                 num_ctx=16384):
        """model: set it to use an Ollama server (its native /api/chat: the model by name, context num_ctx, thinking
        off, JSON schema output). Unset: llama-server's OpenAI-compatible endpoint, as before."""
        self.url = url.rstrip("/") + "/v1/chat/completions"
        self.temperature, self.max_tokens, self.timeout, self.model = temperature, max_tokens, timeout, model
        self.num_ctx = num_ctx

    def ping(self):
        base = self.url.rsplit("/v1/", 1)[0]
        for path in ("/health", "/v1/models"):     # llama-server, then any OpenAI-compatible server (Ollama)
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    if r.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
        return False

    def json(self, system, user, schema, max_tokens=None, retries=3):
        """Returns (parsed_json, usage_dict)."""
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": schema}},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        url = self.url
        if self.model:   # Ollama: its /v1 keeps a 4k default context and thinks first; the native API sets both
            url = self.url.rsplit("/v1/", 1)[0] + "/api/chat"
            body = {"model": self.model, "messages": body["messages"], "stream": False, "think": False,
                    "format": schema, "options": {"temperature": self.temperature, "num_ctx": self.num_ctx,
                                                  "num_predict": max_tokens or self.max_tokens}}
        data = json.dumps(body).encode()
        err = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.load(r)
                if self.model:
                    text = out["message"]["content"]
                    usage = {"prompt_tokens": out.get("prompt_eval_count"), "completion_tokens": out.get("eval_count")}
                else:
                    text = out["choices"][0]["message"]["content"]
                    usage = out.get("usage", {})
                return conform(json.loads(text), schema), usage
            except (urllib.error.URLError, OSError, KeyError, TypeError, ValueError) as e:
                err = e
                time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"model server call failed after {retries} tries: {err}")
