#!/usr/bin/env bash
# llama-server for job 1 (extract_fixes.py run): a small model fully on the 3070, 4 parallel slots.
# Stop marker / embedding jobs first: this wants most of the 8 GB.
# MODEL can be a local .gguf path or a Hugging Face repo:quant (downloaded once by llama-server).
MODEL="${MODEL:-unsloth/Qwen3-8B-GGUF:Q4_K_M}"
SLOTS="${SLOTS:-4}"          # keep equal to extract_fixes.py run --parallel
CTX="${CTX:-16384}"          # shared by all slots: 4096 tokens each, threads are cut to ~1500

if [[ "$MODEL" == *.gguf ]]; then SRC=(-m "$MODEL"); else SRC=(-hf "$MODEL"); fi
exec llama-server "${SRC[@]}" --jinja \
  -ngl 99 -c "$CTX" -np "$SLOTS" -fa on --cache-type-k q8_0 --cache-type-v q8_0 \
  --host 127.0.0.1 --port 8080
