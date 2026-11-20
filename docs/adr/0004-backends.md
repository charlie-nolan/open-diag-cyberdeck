# ADR-0004: Model backends (app/llm_backend.py)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `app/llm_backend.py`

- `RKLLMModel.__init__`: Keep the CFUNCTYPE object alive for the lifetime of the handle.
- `RKLLMModel.__init__`: Qwen chat format; an empty <think> block keeps Qwen3 hybrid models out of thinking mode.
- `RKLLMModel.__init__`: Think tags only for Qwen3 hybrid-thinking models: Qwen2.5 and the 2507 Instruct template have none.
- `RKLLMModel.generate`: Last guard: a prompt built for a bigger context (Home mode falling back to the deck) is cut to fit instead of failing with "prompt is greater than max context". Keeps the start (case) and the end (question).
- `RKLLMModel.generate`: ~2.6 chars a token, conservative.
- `OllamaModel.generate`: A fixed window instead of Ollama's default, which silently drops the oldest messages (the case) when a prompt is long.
- `OpenAICompatModel.generate`: Reasoning models on llama.cpp vLLM DeepSeek-style APIs send their thinking separately.
- `ClaudeModel.__init__`: Max_tokens covers thinking as well as the answer, so it gets a generous floor.
