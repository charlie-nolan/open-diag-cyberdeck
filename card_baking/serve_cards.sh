#!/usr/bin/env bash
# llama-server for job 2 (bake_cards.py): a mixture-of-experts model split between the 3070 and system RAM.
# Needs ~22 GB free RAM for a 35B-A3B at Q4. Don't run it alongside serve_extract.sh.
# MODEL can be a local .gguf path or a Hugging Face repo:quant. Check the exact repo name on Hugging Face
# before the first run (Qwen 3.6-35B-A3B suggested; Qwen3-30B-A3B or Gemma 4 26B MoE also work).
MODEL="${MODEL:-unsloth/Qwen3.6-35B-A3B-GGUF:Q4_K_M}"
CPU_MOE="${CPU_MOE:-99}"     # expert layers kept in RAM. 99 = all (always fits). Lower it step by step
                             # while nvidia-smi shows spare VRAM: every layer moved to the GPU is faster.
CTX="${CTX:-16384}"          # one card prompt is ~5k tokens

if [[ "$MODEL" == *.gguf ]]; then SRC=(-m "$MODEL"); else SRC=(-hf "$MODEL"); fi
exec llama-server "${SRC[@]}" --jinja \
  -ngl 99 --n-cpu-moe "$CPU_MOE" -c "$CTX" -np 1 -fa on -t 12 \
  --host 127.0.0.1 --port 8081
